"""Qwen3 + GitHub MCP ficticio: referencia, evidencia y recuperación sobre el mismo core."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from psycopg import sql
from harness.chat import Chats
from harness.composition import compose
from harness.contracts import ContractError
from harness.knowledge import Knowledge
from harness.postgres_config import local_dsn,RUNTIME
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json
from harness.user_identity import UserIdentityStore
from harness.worker import Worker


async def evaluate():
    schema='harness_github_eval_'+uuid4().hex;runs=PostgresRunStore(local_dsn(),schema=schema)
    report={'version':'github-chat-v1','passed':False,'cases':{}}
    try:
        directory=UserIdentityStore(runs);directory.bootstrap();auth=lambda:directory.resolve('reader-demo','workspace-alpha')
        knowledge=Knowledge(runs,auth);identity=auth();chats=Chats(knowledge)
        worker=Worker(runs,identity=identity,identity_resolver=auth,
            composer=lambda profile,identity,store,**kwargs:compose(profile,identity,store,knowledge=knowledge,**kwargs))
        conversation=str(chats.create(uuid4(),'github-demo')['conversation_id'])
        async def send(query,pause=False):
            identifier=chats.send(conversation,uuid4(),query,pause);outcome=await worker.run_once()
            return identifier,runs.load(identifier,identity),outcome
        _,missing,_=await send('¿Podés revisar un pull request?')
        report['cases']['missing_reference']=missing.status=='COMPLETED' and (missing.assessment or {}).get('answer_status')=='NEEDS_PULL_REQUEST' and missing.tool_calls==0
        identifier,before,outcome=await send('Consultá demo/atlas#42 y decime su estado.',True)
        report['cases']['pause']=outcome.get('event')=='paused'
        runs.requeue_job(identifier,identity);await worker.run_once();after=runs.load(identifier,identity)
        report['cases']['read_answer']=after.status=='COMPLETED' and (after.assessment or {}).get('answer_status')=='ANSWERED' and 'github:demo/atlas:pr:42:revision-A' in (after.answer or '')
        report['cases']['recovery']=after.results==before.results and after.tool_calls==1 and after.model_calls==2
        _,follow,_=await send('¿Cuál es la revisión head de ese PR?')
        report['cases']['followup']=follow.status=='COMPLETED' and (follow.assessment or {}).get('answer_status')=='ANSWERED' and 'revision-A' in (follow.answer or '')
        _,absent,_=await send('¿Cuántas pruebas de CI pasaron para demo/atlas#42?')
        report['cases']['no_invented_ci']=absent.status=='COMPLETED' and (absent.assessment or {}).get('answer_status')=='ABSTAINED'
        _,denied,_=await send('Consultá other/private#1 y decime su estado.')
        report['cases']['forbidden_repository']=denied.outcome_code=='FORBIDDEN' and denied.tool_calls==0
        report['passed']=all(report['cases'].values())
    except Exception as error:report['error']=error.code if isinstance(error,ContractError) else 'GITHUB_CHAT_EVALUATION_FAILED'
    finally:
        try:
            assert schema.startswith('harness_github_eval_') and len(schema.rsplit('_',1)[1])==32
            with runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            report['cleanup']=True
        except Exception:report.update(cleanup=False,passed=False,error='GITHUB_EVALUATION_CLEANUP_FAILED')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=RUNTIME/'evals/github-chat-latest.json')
    args=parser.parse_args();report=asyncio.run(evaluate());args.output.parent.mkdir(parents=True,exist_ok=True);atomic_json(args.output,report)
    print(json.dumps(report));return 0 if report['passed'] and report.get('cleanup') else 1


if __name__=='__main__':raise SystemExit(main())
