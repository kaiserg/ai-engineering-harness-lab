"""Cierre autenticado por HTTP ASGI, workers y MCP reales; almacenamiento temporal."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sys
import tempfile
from threading import Lock
from time import monotonic,perf_counter
from unittest.mock import patch
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from scripts.evaluate_writes import isolated_store,expired_lease
from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
from harness.contracts import ContractError,Identity
from harness.core import Limits
from harness.jenkins_simulator import JenkinsSimulator
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json
from harness.user_identity import UserIdentityStore
from harness.user_write_worker import create_worker
from harness.web import create_app
from harness.worker import Worker

CASES=ROOT/'evals/authenticated_cases.json'
SCENARIOS={'approved','revoked-before-send','recovery-revoked','manual-closure'}
EXPECTED={'operation','job','effects','calls','events','review','code','last_actor','claims_range'}
COMMON={'scope_denied','self_approval_denied','read_recovery_preserved','source_preserved','limits_preserved',
    'same_intent','request_binding','reviewer_from_session','retry_safe','secret_free_report'}


def load_cases():
    cases=json.loads(CASES.read_text());seen=set();ids=set()
    if type(cases) is not list or len(cases)!=len(SCENARIOS):raise ValueError('Dataset incompleto')
    for case in cases:
        if (type(case) is not dict or set(case)!={'id','scenario','expected'} or case['scenario'] not in SCENARIOS
                or case['scenario'] in seen or case['id']!='AUTH-'+case['scenario'] or case['id'] in ids
                or type(case['expected']) is not dict or set(case['expected'])!=EXPECTED):raise ValueError('Dataset inválido')
        e=case['expected']
        if (type(e['calls']) is not dict or set(e['calls'])!={'revision','request','lookup','close'}
                or any(type(v) is not int or v<0 for v in e['calls'].values())
                or type(e['effects']) is not int or e['effects']<0
                or type(e['claims_range']) is not list or len(e['claims_range'])!=2
                or any(type(v) is not int for v in e['claims_range']) or not 1<=e['claims_range'][0]<=e['claims_range'][1]<=5
                or type(e['events']) is not list):raise ValueError('Dataset inválido')
        seen.add(case['scenario']);ids.add(case['id'])
    return cases


def grade(case,observed):
    checks={key:observed.get(key)==value for key,value in case['expected'].items() if key!='claims_range'}
    low,high=case['expected']['claims_range'];claims=observed.get('claims')
    checks['claims_bounded']=type(claims) is int and low<=claims<=high
    flags=COMMON.copy()
    if case['scenario']=='revoked-before-send':flags.add('revoked_actor_denied')
    if case['scenario']=='recovery-revoked':flags|={'revoked_actor_denied','interruption_observed'}
    if case['scenario']=='manual-closure':flags|={'interruption_observed','review_required','self_review_denied','late_request_rejected','atomic_job_finish'}
    checks.update({key:observed.get(key) is True for key in sorted(flags)})
    checks['read_calls']=observed.get('read_calls')==[4,3]
    return checks


async def recover(writer,identifier):
    deadline=monotonic()+30
    while monotonic()<deadline:
        result=await writer.run_once()
        if result is not None:
            if result.get('status')!='PENDING':return result
            if result.get('last_error')!='OPERATION_BUSY':raise ValueError('Recuperación inesperada')
        await asyncio.sleep(.1)
    raise TimeoutError('Recuperación incompleta')


async def evaluate(case):
    started=perf_counter();scenario=case['scenario'];flags={};calls=Counter({k:0 for k in ('revision','request','lookup','close')})
    bindings=[];probe=[False];probe_calls=[0];lock=Lock();original=MCPJenkinsWrite._call
    def measured(adapter,tool,args):
        action={'jenkins_current_revision':'revision','jenkins_request_tests':'request','jenkins_find_request':'lookup','jenkins_close_request':'close'}[tool]
        with lock:
            if probe[0]:probe_calls[0]+=1
            else:calls[action]+=1
            if 'operation_id' in args:bindings.append((args['operation_id'],args['payload_hash'],args.get('arguments')))
        return original(adapter,tool,args)
    with tempfile.TemporaryDirectory(prefix='harness-auth-eval-') as temporary,isolated_store() as db:
        path=Path(temporary)/'provider.sqlite3';directory=UserIdentityStore(db);directory.bootstrap()
        sessions={key:directory.issue(actor,workspace) for key,actor,workspace in (
            ('owner','reader-demo','workspace-alpha'),('reviewer','approver-demo','workspace-alpha'),('other','reader-beta','workspace-beta'))}
        identity=directory.resolve('reader-demo','workspace-alpha');scope=Identity(identity.actor_id,identity.workspace_id,frozenset(),frozenset())
        app=create_app(storage='postgres',pg_dsn=local_dsn(),pg_schema=db.schema,user_auth=True,user_directory=directory,approval_provider_path=path)
        with TestClient(app,base_url='http://127.0.0.1') as client,patch.object(MCPJenkinsWrite,'_call',measured):
            def http(url,body=None,who='owner'):
                headers={'X-Harness-Client':'local-ui'}
                if who:headers['Authorization']='Bearer '+sessions[who]['token']
                return client.request('GET' if body is None else 'POST','/api'+url,headers=headers,json=body)
            def api(url,body=None,who='owner',status=200):
                response=http(url,body,who)
                if response.status_code!=status:raise ValueError('Estado HTTP inesperado')
                return response.json()
            flags['scope_denied']=http('/runs',{},who=None).status_code==401
            run=api('/runs',{'scenario':'stale','pause_at':'jira'},status=202);rid=run['run_id']
            reader=Worker(db,identity=scope,identity_resolver=lambda:directory.resolve(scope.actor_id,scope.workspace_id))
            if (await reader.run_once())['event']!='paused':raise ValueError('Falta checkpoint')
            paused=api('/runs/'+rid)
            flags['scope_denied'] &= (http('/runs/'+rid,who='other').status_code==404
                and http('/runs/'+rid+'/resume',{},who='other').status_code==404
                and http('/runs',who='reviewer').status_code==403)
            directory.set_membership('approver-demo',identity.workspace_id,frozenset({'reader','approver'}))
            flags['scope_denied'] &= http('/runs/'+rid,who='reviewer').status_code==404
            directory.set_membership('approver-demo',identity.workspace_id,frozenset({'approver'}))
            api('/runs/'+rid+'/resume',{},status=202)
            if (await reader.run_once())['status']!='COMPLETED':raise ValueError('Lectura incompleta')
            source=db.load(rid,identity)
            flags['read_recovery_preserved']=(asdict(source.results[0])==paused['results'][0]
                and [paused['model_calls'],paused['tool_calls']]==[1,1])
            key=str(uuid4());item=api('/runs/'+rid+'/proposals',{'request_key':key},status=201)['proposal'];pid=item['proposal_id']
            flags['same_intent']=api('/runs/'+rid+'/proposals',{'request_key':key},status=201)['proposal']==item
            decision={'decision':'APPROVED','payload_hash':item['payload_hash'],'expected_version':0}
            directory.set_membership(identity.actor_id,identity.workspace_id,frozenset({'reader','operator','approver'}))
            flags['self_approval_denied']=http('/proposals/'+pid+'/decision',decision).status_code==403
            directory.set_membership(identity.actor_id,identity.workspace_id,identity.roles)
            reviewed=api('/proposals/'+pid,who='reviewer')['proposal']
            approved=api('/proposals/'+pid+'/decision',decision,'reviewer')['proposal']
            flags['reviewer_from_session']=(approved['decided_by']=='approver-demo'
                and reviewed['payload']==item['payload'] and reviewed['payload_hash']==item['payload_hash'])
            prepared=api('/proposals/'+pid+'/operation',{});oid=prepared['operation']['operation_id']
            flags['same_intent'] &= api('/proposals/'+pid+'/operation',{})==prepared
            interrupt={'recovery-revoked':'after-send','manual-closure':'before-send'}.get(scenario)
            admitted=api('/operations/'+oid+'/dispatch',{'interrupt':interrupt},status=202)
            flags['same_intent'] &= api('/operations/'+oid+'/dispatch',{'interrupt':interrupt},status=202)==admitted
            def writer():
                reopened=PostgresRunStore(local_dsn(),schema=db.schema)
                return create_worker(reopened,UserIdentityStore(reopened),identity.actor_id,identity.workspace_id,path)
            if scenario=='revoked-before-send':directory.set_membership('approver-demo',identity.workspace_id,frozenset(),enabled=False)
            first=writer();result=await first.run_once();review=None
            if interrupt:
                flags['interruption_observed']=result['status']=='PENDING' and first.ops.get(oid,first.identity)['status']=='DISPATCHING'
                if scenario=='recovery-revoked':
                    for who in ('owner','reviewer'):
                        actor=identity.actor_id if who=='owner' else 'approver-demo'
                        directory.set_membership(actor,identity.workspace_id,frozenset(),enabled=False)
                        directory.revoke(sessions[who]['session_id'])
                    flags['revoked_actor_denied']=http('/runs/'+rid).status_code==401
                await expired_lease(db,oid)
                second=writer();result=await recover(second,oid)
            if scenario=='revoked-before-send':flags['revoked_actor_denied']=http('/proposals/'+pid,who='reviewer').status_code==401
            if scenario=='manual-closure':
                flags['review_required']=result['status']=='NEEDS_REVIEW' and second.ops.get(oid,second.identity)['status']=='UNKNOWN'
                review=api('/operations/'+oid+'/resolution',{})['resolution']
                flags['review_required'] &= review['status']=='PENDING_REVIEW' and second.ops.get(oid,second.identity)['status']=='UNKNOWN'
                args={'payload_hash':review['payload_hash'],'expected_version':0}
                flags['self_review_denied']=http('/operations/'+oid+'/resolution/confirm',args).status_code==403
                directory.set_membership(identity.actor_id,identity.workspace_id,frozenset(),enabled=False)
                confirmed=api('/operations/'+oid+'/resolution/confirm',args,'reviewer')['resolution']
                flags['retry_safe']=api('/operations/'+oid+'/resolution/confirm',args,'reviewer')['resolution']==confirmed
                review=confirmed['status']
                probe[0]=True
                try:
                    second.ops.provider.client.request(second.ops.provider.client.identity,oid,item['payload_hash'],
                        item['payload']['arguments'],datetime.now(timezone.utc)+timedelta(seconds=30))
                except ContractError as error:flags['late_request_rejected']=error.code=='OPERATION_CLOSED'
                else:flags['late_request_rejected']=False
                finally:probe[0]=False
            final=writer();op=final.ops.get(oid,final.identity);job=final.queue.get(oid,final.identity)
            before=dict(calls)
            flags['retry_safe']=flags.get('retry_safe',True) and await final.run_once() is None and dict(calls)==before
            after=db.load(rid,identity)
            flags['source_preserved']=after==source
            flags['limits_preserved']=after.limits_snapshot==asdict(Limits()) and after.pending_time_seconds==0 and 0<=after.active_seconds<=Limits().active_timeout
            flags['request_binding']=all(identifier==oid and digest==item['payload_hash'] and (args is None or args==item['payload']['arguments']) for identifier,digest,args in bindings)
            with db._database() as connection:
                count=connection.execute('SELECT count(*) AS n FROM operations').fetchone()['n']
                jobs=connection.execute('SELECT count(*) AS n FROM write_jobs').fetchone()['n']
            flags['same_intent'] &= count==jobs==1
            if scenario=='manual-closure':flags['atomic_job_finish']=job['status']=='DONE' and job['result_status']==op['status']
            with JenkinsSimulator(path)._db() as provider:effects=provider.execute('SELECT count(*) AS n FROM requests').fetchone()['n']
            observed=dict(operation=op['status'],job=job['status'],effects=effects,calls=dict(calls),claims=job['attempts'],
                events=[e['status'] for e in op['events']],review=review,code=op['outcome_code'],last_actor=op['events'][-1]['actor_id'],
                read_calls=[after.model_calls,after.tool_calls],probe_calls=probe_calls[0],**flags)
            text=json.dumps(observed)
            observed['secret_free_report']=all(value not in text for session in sessions.values() for value in (session['token'],session['session_id']))
            checks=grade(case,observed)
            row=dict(id=case['id'],passed=all(checks.values()),checks=checks,observed=observed,duration_ms=round((perf_counter()-started)*1000,2))
    return row


async def run_suite():
    try:cases=load_cases()
    except Exception:
        return {'schema_version':1,'dataset':'authenticated-e2e-v1','passed':0,'total':1,
            'cases':[{'id':'dataset','passed':False,'checks':{'dataset_valid':False},'error':'EVALUATION_DATASET_ERROR'}]}
    rows=[]
    for case in cases:
        try:row=await evaluate(case)
        except Exception:row={'id':case['id'],'passed':False,'checks':{'execution':False},'error':'EVALUATION_EXECUTION_ERROR'}
        rows.append(row)
        failed=[key for key,value in row['checks'].items() if not value]
        print(('PASS' if row['passed'] else 'FAIL')+' '+row['id']+(' · '+', '.join(failed) if failed else ''),flush=True)
    return dict(schema_version=1,dataset='authenticated-e2e-v1',model='scripted',transport='http-asgi+mcp-stdio-python-go',
        passed=sum(r['passed'] for r in rows),total=len(rows),cases=rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/runtime/evals/authenticated-latest.json')
    args=parser.parse_args();report=asyncio.run(run_suite())
    args.output.parent.mkdir(parents=True,exist_ok=True);atomic_json(args.output,report)
    print(f"{report['passed']}/{report['total']} casos aprobados. Reporte: {args.output}")
    return 0 if report['total']>0 and report['passed']==report['total'] else 1


if __name__=='__main__':raise SystemExit(main())
