"""Evalúa tres recorridos de RAG con Qwen3, Chroma y un esquema PostgreSQL temporal."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from psycopg import sql
from harness.chat import Chats
from harness.chroma_vectors import ChromaVectors, BASE
from harness.composition import compose
from harness.embeddings import SEMANTIC_CONFIG
from harness.knowledge import Knowledge
from harness.postgres_config import RUNTIME, local_dsn
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json
from harness.user_identity import UserIdentityStore
from harness.worker import Worker

DOCUMENTS = {
    'atlas-entrega.md':'La responsable de la entrega del producto Atlas es Marta Salas.',
    'atlas-ficha.md':'Ficha de guardia del producto Atlas. La entrada del directorio que contiene el canal exacto de Atlas tiene el código ZEFR-924.',
    'atlas-oncall.md':'El canal de guardia de Atlas figura en el directorio bajo el identificador de su ficha. Este documento describe el proceso de guardia.',
    'atlas-soporte.md':'La consulta por el canal de guardia del producto Atlas comienza en el inventario de productos y continúa en el directorio.',
    'directorio.md':'ZEFR-924 — canal de guardia: #torre-violeta.',
}
CASES = {
    'direct': ('¿Quién es responsable de la entrega de Atlas?', 'ANSWERED', 'marta', 1),
    'reformulate': ('¿Cuál es el nombre exacto del canal de guardia del producto Atlas? Si solo encontrás una referencia al directorio, seguí esa referencia.', 'ANSWERED', '#torre-violeta', 2),
    'abstain': ('¿Cuál fue el presupuesto aprobado para Atlas en 2022?', 'ABSTAINED', '', None),
}


async def evaluate():
    schema = 'harness_agentic_eval_'+uuid4().hex
    runs = PostgresRunStore(local_dsn(),schema=schema)
    report = {'version':'agentic-rag-v1','cases':{},'passed':False}
    try:
        directory = UserIdentityStore(runs)
        directory.bootstrap()
        def auth():
            return directory.resolve('reader-demo','workspace-alpha')
        identity = auth()
        knowledge = Knowledge(runs,auth,configuration=SEMANTIC_CONFIG)
        for filename, text in DOCUMENTS.items():
            knowledge.register(filename,text)
        knowledge.rebuild()
        chats = Chats(knowledge)
        worker = Worker(runs,identity=identity,identity_resolver=auth,
            composer=lambda profile,actor,store,**options:compose(profile,actor,store,knowledge=knowledge,**options))
        for name, (question,status,expected,count) in CASES.items():
            conversation = str(chats.create(uuid4(),'documents-agentic')['conversation_id'])
            identifier = chats.send(conversation,uuid4(),question,pause=name=='reformulate')
            outcome = await worker.run_once()
            before = runs.load(identifier,identity)
            if name == 'reformulate' and outcome.get('event')=='paused':
                runs.requeue_job(identifier,identity)
                await worker.run_once()
            saved = runs.load(identifier,identity)
            assessment = saved.assessment or {}
            searches = len(saved.results)
            recovered = name!='reformulate' or (outcome.get('event')=='paused'
                and len(before.results)==1 and saved.results[:1]==before.results and saved.tool_calls==2
                and expected not in ' '.join(m['text'] for m in before.results[0].data['matches']))
            passed = (saved.status=='COMPLETED' and assessment.get('answer_status')==status
                and expected in (saved.answer or '').lower() and (count is None or count==searches)
                and searches<=2 and recovered)
            report['cases'][name] = {'passed':passed,'status':saved.status,'outcome':saved.outcome_code,
                'answer_status':assessment.get('answer_status'),'searches':searches,
                'queries':[r.data['query'] for r in saved.results],
                'retrieved':[[m['filename'] for m in r.data['matches']] for r in saved.results],
                'recovered':recovered,'answer':saved.answer}
        report['passed'] = all(case['passed'] for case in report['cases'].values())
    finally:
        try:
            with runs._database() as db:
                table = db.execute("SELECT to_regclass('knowledge_indexes') AS name").fetchone()['name']
                collections = db.execute('SELECT collection_name FROM knowledge_indexes').fetchall() if table else []
            for row in collections:
                ChromaVectors().request('DELETE',BASE+'/'+row['collection_name'])
            report['cleanup'] = True
        finally:
            with runs._database() as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=RUNTIME/'evals/agentic-rag-latest.json')
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(args.output,report)
    print(json.dumps(report,ensure_ascii=False))
    raise SystemExit(0 if report['passed'] and report['cleanup'] else 1)
