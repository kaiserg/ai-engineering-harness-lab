"""Evals M4 contra PostgreSQL temporal y MCP stdio, nunca contra el schema de la app."""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict,replace
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import re
import sys
import tempfile
from time import monotonic,perf_counter
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from psycopg import sql
from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
from harness.composition import compose
from harness.contracts import ContractError,Run
from harness.core import Limits
from harness.development import fixture_identity
from harness.jenkins_simulator import JenkinsSimulator
from harness.operations import Operations
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.proposals import ProposalStore,digest
from harness.resolutions import Resolutions
from harness.security_lifecycle import atomic_json
from harness.write_queue import WriteQueue
from harness.write_worker import WriteWorker

CASES=ROOT/'evals/write_cases.json'
SCENARIOS={'approved','rejected','expired','stale','revoked','response-lost','after-send','manual-closure'}
EXPECTED={'proposal','effective_proposal','operation','job','claims','effects','calls','events','code','review'}
FLAGS={'source_preserved','limits_preserved','single_intent','request_binding','retry_safe'}


def load_cases():
    cases=json.loads(CASES.read_text())
    if type(cases) is not list or not cases:raise ValueError('Dataset inválido')
    ids=set();scenarios=set()
    for case in cases:
        if (type(case) is not dict or set(case)!={'id','scenario','expected'}
                or type(case['id']) is not str or not re.fullmatch(r'M4-[a-z-]+',case['id'])
                or case['id'] in ids or case['scenario'] not in SCENARIOS
                or type(case['expected']) is not dict or set(case['expected'])!=EXPECTED):
            raise ValueError('Dataset inválido')
        expected=case['expected']
        if (type(expected['calls']) is not dict or set(expected['calls'])!={'revision','request','lookup','close'}
                or any(type(v) is not int or v<0 for v in expected['calls'].values())
                or any(type(expected[k]) is not int or expected[k]<0 for k in ('claims','effects'))
                or type(expected['events']) is not list):raise ValueError('Dataset inválido')
        ids.add(case['id']);scenarios.add(case['scenario'])
    if scenarios!=SCENARIOS or len(cases)!=len(SCENARIOS):raise ValueError('Dataset incompleto')
    return cases


def grade(case,observed):
    # El oráculo es el dataset versionado, no el resultado devuelto por Operations.
    checks={key:observed.get(key)==value for key,value in case['expected'].items()}
    flags=FLAGS|({'interruption_observed'} if case['scenario'] in ('after-send','manual-closure') else set())
    if case['scenario']=='manual-closure':flags|={'review_required','self_review_denied','late_request_rejected'}
    checks.update({key:observed.get(key) is True for key in sorted(flags)})
    checks['exact_read_calls']=observed.get('read_calls')==[4,3]
    return checks


@contextmanager
def isolated_store():
    schema='harness_eval_'+uuid4().hex
    runs=None
    try:
        runs=PostgresRunStore(local_dsn(),schema=schema)
        yield runs
    finally:
        # El destino se genera aquí; no aceptamos --schema ni borramos harness_lab.
        if runs is not None:
            if not re.fullmatch(r'harness_eval_[0-9a-f]{32}',schema):raise ValueError('Schema inválido')
            with runs._database() as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


class ObservedProvider:
    """Cuenta invocaciones sin reemplazar transporte, validación ni proveedor."""
    def __init__(self,adapter,calls,requests):self.adapter,self.calls,self.requests=adapter,calls,requests
    def revision(self,*args):
        self.calls['revision']+=1
        return self.adapter.revision(*args)
    def request(self,identity,identifier,payload_hash,arguments,not_after):
        self.calls['request']+=1;self.requests.append((identifier,payload_hash,arguments.copy()))
        return self.adapter.request(identity,identifier,payload_hash,arguments,not_after)
    def lookup(self,*args):
        self.calls['lookup']+=1
        return self.adapter.lookup(*args)
    def close_request(self,*args):
        self.calls['close']+=1
        return self.adapter.close_request(*args)


def caught(action):
    try:action()
    except ContractError as error:return error.code
    return None  # Un rechazo que no ocurrió hará fallar el criterio esperado.


def expired_fixture(runs,proposal):
    """Fixture histórica aprobada y vencida; no modificar ni desproteger una fila existente."""
    identifier=str(uuid4())
    with runs._database() as db:
        row=db.execute('SELECT * FROM proposals WHERE proposal_id=%s',(proposal['proposal_id'],)).fetchone()
        past=db.execute("SELECT clock_timestamp()-interval '1 second' AS now").fetchone()['now']
        payload=json.loads(row['canonical_payload']);payload['expires_at']=past.isoformat()
        row.update(proposal_id=identifier,request_key=str(uuid4()),expires_at=past,
            canonical_payload=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False),payload_hash=digest(payload))
        db.execute(sql.SQL('INSERT INTO proposals ({}) VALUES ({})').format(sql.SQL(',').join(map(sql.Identifier,row)),
            sql.SQL(',').join(sql.Placeholder() for _ in row)),list(row.values()))
    return identifier


async def expired_lease(runs,identifier):
    until=monotonic()+30
    while monotonic()<until:
        with runs._database() as db:
            expired=db.execute('SELECT lease_until<=clock_timestamp() AS expired FROM operations WHERE operation_id=%s',
                (identifier,)).fetchone()['expired']
        if expired:return
        await asyncio.sleep(.1)
    raise TimeoutError('Lease no venció')


async def evaluate(case):
    started=perf_counter();scenario=case['scenario']
    with tempfile.TemporaryDirectory(prefix='harness-write-eval-') as temporary,isolated_store() as runs:
        path=Path(temporary)/'provider.sqlite3';simulator=JenkinsSimulator(path)
        operator,approver=fixture_identity('operator'),fixture_identity('approver')
        principals={operator.actor_id:operator,approver.actor_id:approver}
        calls=Counter({name:0 for name in ('revision','request','lookup','close')});requests=[]
        def runtime():
            reopened=PostgresRunStore(local_dsn(),schema=runs.schema)
            adapter=MCPJenkinsWrite(operator,path)
            if scenario=='response-lost':adapter.parameters.env['HARNESS_JENKINS_WRITE_FAULT']='exit-after-commit'
            return Operations(ProposalStore(reopened),ObservedProvider(adapter,calls,requests),principals)
        ops=runtime();store=ops.proposals;queue=WriteQueue(ops)
        profile='mcp-devops-ci-v3:stale:benign:ignore'
        run=runs.create(Run(operator.workspace_id,operator.actor_id,execution_profile=profile),operator)
        source=await compose(profile,operator,runs).harness.execute(run.run_id,operator)
        proposal=store.create(source.run_id,operator,str(uuid4()));pid=proposal['proposal_id']
        store.decide(pid,approver,payload_hash=proposal['payload_hash'],expected_version=0,
            decision='REJECTED' if scenario=='rejected' else 'APPROVED')
        if scenario=='expired':pid=expired_fixture(runs,proposal)
        flags={'single_intent':True};code=None;oid=None;review=None
        if scenario in ('rejected','expired'):
            code=caught(lambda:ops.prepare(pid,operator))
            flags['retry_safe']=code is not None and caught(lambda:ops.prepare(pid,operator))==code
        else:
            op=ops.prepare(pid,operator);oid=op['operation_id']
            flags['single_intent']=ops.prepare(pid,operator)==op
            interrupt={'after-send':'after-send','manual-closure':'before-send'}.get(scenario)
            admitted=queue.enqueue(oid,operator,interrupt=interrupt)
            flags['single_intent'] &= queue.enqueue(oid,operator,interrupt=interrupt)==admitted
            if scenario=='stale':simulator.set_revision(operator.workspace_id,'revision-B')
            if scenario=='revoked':principals[approver.actor_id]=replace(approver,jobs=frozenset())
            # Runtime nuevo: no depender del objeto que preparó/encoló la operación.
            ops=runtime();queue=WriteQueue(ops)
            await WriteWorker(ops,operator).run_once()
            if interrupt:
                with simulator._db() as db:effects=db.execute('SELECT count(*) AS n FROM requests').fetchone()['n']
                flags['interruption_observed']=(queue.get(oid,operator)['status']=='PENDING'
                    and ops.get(oid,operator)['status']=='DISPATCHING'
                    and effects==(1 if scenario=='after-send' else 0))
                await expired_lease(runs,oid)
                ops=runtime();queue=WriteQueue(ops)
                await WriteWorker(ops,operator).run_once()
            if scenario=='manual-closure':
                flags['review_required']=(ops.get(oid,operator)['status']=='UNKNOWN'
                    and queue.get(oid,operator)['status']=='NEEDS_REVIEW')
                resolutions=Resolutions(ops);pending=resolutions.prepare(oid,operator)
                args=dict(payload_hash=pending['payload_hash'],expected_version=pending['version'])
                flags['review_required'] &= ops.get(oid,operator)['status']=='UNKNOWN' and pending['status']=='PENDING_REVIEW'
                flags['self_review_denied']=caught(lambda:resolutions.confirm(oid,operator,operator,**args))=='FORBIDDEN'
                confirmed=resolutions.confirm(oid,operator,approver,**args)
                flags['retry_safe']=resolutions.confirm(oid,operator,approver,**args)==confirmed
                # Sonda adversarial separada: el cierre debe rechazar un envío tardío.
                flags['late_request_rejected']=caught(lambda:MCPJenkinsWrite(operator,path).request(operator,oid,
                    proposal['payload_hash'],proposal['payload']['arguments'],datetime.now(timezone.utc)+timedelta(seconds=30)))=='OPERATION_CLOSED'
                review=confirmed['status']
                await WriteWorker(runtime(),operator).run_once()  # Sincroniza DONE, sin reclamar otro envío.
            counts_before=dict(calls);before=ops.get(oid,operator)
            ops=runtime();queue=WriteQueue(ops)
            idle=await WriteWorker(ops,operator).run_once()
            retried=ops.dispatch(oid,operator)
            flags['retry_safe'] = flags.get('retry_safe',True) and (idle is None and retried['status']==before['status'] and dict(calls)==counts_before)
            code=before['outcome_code']
        final=ops.get(oid,operator) if oid else None
        job=queue.get(oid,operator) if oid else None
        final_proposal=store.get(pid,operator)
        with simulator._db() as db:effects=db.execute('SELECT count(*) AS n FROM requests').fetchone()['n']
        with runs._database() as db:
            operations=db.execute('SELECT count(*) AS n FROM operations').fetchone()['n']
            jobs=db.execute('SELECT count(*) AS n FROM write_jobs').fetchone()['n']
        flags['single_intent'] &= operations==jobs==(1 if oid else 0)
        flags['request_binding']=all((identifier,payload_hash,arguments)==
            (oid,proposal['payload_hash'],proposal['payload']['arguments']) for identifier,payload_hash,arguments in requests)
        after=runs.load(source.run_id,operator)
        flags['source_preserved']=after==source
        flags['limits_preserved']=(source.limits_snapshot==asdict(Limits()) and source.pending_time_seconds==0
            and 0<=source.active_seconds<=Limits().active_timeout)
        observed=dict(proposal=final_proposal['status'],effective_proposal=final_proposal['effective_status'],
            operation=final['status'] if final else None,job=job['status'] if job else None,
            claims=job['attempts'] if job else 0,effects=effects,calls=dict(calls),
            events=[event['status'] for event in final['events']] if final else [],code=code,review=review,
            read_calls=[after.model_calls,after.tool_calls],**flags)
        checks=grade(case,observed)
        row=dict(id=case['id'],passed=all(checks.values()),checks=checks,observed=observed,
            duration_ms=round((perf_counter()-started)*1000,2))
    # Solo devolver éxito después de haber limpiado el schema y el proveedor temporal.
    return row


async def run_suite():
    try:cases=load_cases()
    except Exception:
        return dict(schema_version=1,dataset='writes-deterministic-v1',model='scripted',transport='mcp-stdio-python-go',
            passed=0,total=1,cases=[dict(id='dataset',passed=False,checks={'dataset_valid':False},error='EVALUATION_DATASET_ERROR')])
    rows=[]
    for case in cases:
        try:row=await evaluate(case)
        except Exception:
            row=dict(id=case['id'],passed=False,checks={'execution':False},error='EVALUATION_EXECUTION_ERROR')
        rows.append(row)
        failed=[key for key,value in row['checks'].items() if not value]
        print(('PASS' if row['passed'] else 'FAIL')+' '+row['id']+(' · '+', '.join(failed) if failed else ''),flush=True)
    return dict(schema_version=1,dataset='writes-deterministic-v1',model='scripted',transport='mcp-stdio-python-go',
        passed=sum(row['passed'] for row in rows),total=len(rows),cases=rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/runtime/evals/writes-latest.json')
    args=parser.parse_args()
    report=asyncio.run(run_suite());args.output.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(args.output,report)
    print(f"{report['passed']}/{report['total']} casos aprobados. Reporte: {args.output}")
    return 0 if report['total']>0 and report['passed']==report['total'] else 1


if __name__=='__main__':raise SystemExit(main())
