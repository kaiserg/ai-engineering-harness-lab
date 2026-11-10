"""Chat real local: continuidad, idempotencia, recuperación y retirada; datos sintéticos."""
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
from harness.knowledge import Knowledge
from harness.embeddings import SEMANTIC_CONFIG
from harness.chroma_vectors import ChromaVectors,BASE
from harness.contracts import ContractError
from harness.postgres_config import local_dsn,RUNTIME
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json
from harness.user_identity import UserIdentityStore
from harness.worker import Worker


async def evaluate():
    schema='harness_chat_eval_'+uuid4().hex;runs=PostgresRunStore(local_dsn(),schema=schema)
    knowledge=None;report={'version':'chat-continuity-v1','passed':False,'cases':{}}
    try:
        directory=UserIdentityStore(runs);directory.bootstrap()
        auth=lambda:directory.resolve('reader-demo','workspace-alpha')
        knowledge=Knowledge(runs,auth,configuration=SEMANTIC_CONFIG);identity=auth()
        document=knowledge.register('atlas.md','La responsable del release de Atlas es Marta. Ella coordina la entrega de cada nueva versión del producto.')['document_id']
        knowledge.rebuild();chats=Chats(knowledge)
        key=uuid4();conversation=str(chats.create(key)['conversation_id'])
        report['cases']['create_idempotent']=str(chats.create(key)['conversation_id'])==conversation
        worker=Worker(runs,identity=identity,identity_resolver=auth,
            composer=lambda profile,identity,store,**kwargs:compose(profile,identity,store,knowledge=knowledge,**kwargs))
        first_key=uuid4();first=chats.send(conversation,first_key,'¿Quién coordina la entrega de Atlas?')
        report['cases']['send_idempotent']=chats.send(conversation,first_key,'¿Quién coordina la entrega de Atlas?')==first
        await worker.run_once();saved=runs.load(first,identity)
        report['cases']['first_answer']=saved.status=='COMPLETED' and (saved.assessment or {}).get('answer_status')=='ANSWERED' and 'marta' in (saved.answer or '').lower()
        follow=chats.send(conversation,uuid4(),'¿Y cuál es su correo electrónico?')
        await worker.run_once();saved=runs.load(follow,identity)
        report['cases']['followup_abstention']=saved.status=='COMPLETED' and (saved.assessment or {}).get('answer_status')=='ABSTAINED'
        report['followup_search_query']=saved.results[0].data['query'] if saved.results else None
        report['cases']['reference_resolved']=bool(saved.results and 'marta' in saved.results[0].data['query'].lower())
        third=chats.send(conversation,uuid4(),'¿Qué función tiene ella en Atlas?',True)
        outcome=await worker.run_once();before=runs.load(third,identity)
        report['cases']['pause']=outcome.get('event')=='paused' and before.tool_calls==1
        runs.requeue_job(third,identity);await worker.run_once();after=runs.load(third,identity)
        report['cases']['recovery']=after.status=='COMPLETED' and after.results==before.results and after.read_request==before.read_request and after.tool_calls==1 and after.model_calls==2
        report['cases']['followup_answer']=(after.assessment or {}).get('answer_status')=='ANSWERED'
        fresh=Chats(Knowledge(PostgresRunStore(local_dsn(),schema=schema),auth))
        report['cases']['history_restored']=len(fresh.view(conversation)['turns'])==3
        knowledge.retire(document)
        view=fresh.view(conversation)
        report['cases']['revoked_history_redacted']=all(t['unavailable'] for t in view['turns']) and 'Marta' not in json.dumps(view,default=str)
        report['passed']=all(report['cases'].values())
    except Exception as error:report['error']=error.code if isinstance(error,ContractError) else 'CHAT_EVALUATION_FAILED'
    finally:
        try:
            if knowledge:
                with runs._database() as db:collections=db.execute('SELECT collection_name FROM knowledge_indexes').fetchall()
                for row in collections:ChromaVectors().request('DELETE',BASE+'/'+row['collection_name'])
            assert schema.startswith('harness_chat_eval_') and len(schema.rsplit('_',1)[1])==32
            with runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            report['cleanup']=True
        except Exception:report.update(cleanup=False,passed=False,error='CHAT_CLEANUP_FAILED')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=RUNTIME/'evals/chat-latest.json')
    args=parser.parse_args();report=asyncio.run(evaluate());args.output.parent.mkdir(parents=True,exist_ok=True);atomic_json(args.output,report)
    print(json.dumps(report,ensure_ascii=False));return 0 if report['passed'] and report.get('cleanup') else 1


if __name__=='__main__':raise SystemExit(main())
