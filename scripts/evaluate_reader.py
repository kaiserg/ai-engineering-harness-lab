"""Eval real de Qwen3 + Chroma + PostgreSQL, corpus sintético y recursos aislados."""
import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from psycopg import sql
from harness.chroma_vectors import ChromaVectors,BASE
from harness.composition import compose
from harness.contracts import Run,ContractError
from harness.embeddings import SEMANTIC_CONFIG
from harness.knowledge import Knowledge,digest
from harness.llm_reader import PROFILE,MODEL,MODEL_DIGEST,LIMITS,ABSTENTION
from harness.postgres_config import local_dsn,RUNTIME
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json
from harness.user_identity import UserIdentityStore
from harness.checkpoints import RunInterrupted

ROOT=Path(__file__).resolve().parents[1]
DATASET=ROOT/'evals/reader_cases.json'


def grade_case(case,run,documents):
    assessment=run.assessment or {};status=assessment.get('answer_status')
    if case['expected_status']=='ABSTAINED':
        passed=run.status=='COMPLETED' and status=='ABSTAINED' and run.answer==ABSTENTION
    else:
        cited={documents.get(e['citation'].split(':')[1]) for e in assessment.get('evidence',[])}
        passed=(run.status=='COMPLETED' and status=='ANSWERED' and bool(set(case['relevant'])&cited)
            and any(term in (run.answer or '').casefold() for term in case['expected_any']))
    return {'id':case['id'],'passed':bool(passed),'status':str(run.status),'answer_status':status,
        'outcome_code':run.outcome_code,'model_calls':run.model_calls,'tool_calls':run.tool_calls,
        'answer':run.answer,'assessment':assessment,
        'search_query':run.results[0].data['query'] if run.results else None,
        'retrieved_documents':[documents.get(m['citation'].split(':')[1]) for r in run.results for m in r.data.get('matches',[])]}


async def evaluate():
    data=json.loads(DATASET.read_text());corpus_path=ROOT/'evals/retrieval_cases.json'
    corpus=json.loads(corpus_path.read_text());schema='harness_reader_eval_'+uuid4().hex
    runs=PostgresRunStore(local_dsn(),schema=schema);vectors=ChromaVectors();knowledge=None
    report={'dataset':data['version'],'dataset_sha256':digest(DATASET.read_text()),
        'corpus_sha256':digest(corpus_path.read_text()),'profile':PROFILE,'model':MODEL,'model_digest':MODEL_DIGEST,
        'gates':data['gates'],'cases':[],'passed':False,'grading':'fixture assertions, not a universal factuality judge'}
    try:
        directory=UserIdentityStore(runs);directory.bootstrap()
        auth=lambda:directory.resolve('reader-demo','workspace-alpha')
        knowledge=Knowledge(runs,auth,configuration=SEMANTIC_CONFIG);identity=auth();documents={}
        for document in corpus['documents']:
            identifier=knowledge.register(document['filename'],document['text'])['document_id'];documents[identifier]=document['id']
        knowledge.rebuild()
        def execution(**kwargs):return compose(PROFILE,identity,runs,knowledge=knowledge,limits=LIMITS,**kwargs)
        def create(query):return runs.create(Run(identity.workspace_id,identity.actor_id,execution_profile=PROFILE,
            read_request={'query':query},limits_snapshot=asdict(LIMITS)),identity)
        for case in data['cases']:
            run=create(case['query']);result=await execution().harness.execute(run.run_id,identity)
            report['cases'].append(grade_case(case,result,documents))
            print(json.dumps({'case':case['id'],'passed':report['cases'][-1]['passed'],'status':result.status}),flush=True)
        # Recuperación real tras confirmar búsqueda: la misma pregunta, resultados y contadores.
        run=create('¿Quién coordina la entrega de Atlas?')
        def pause(point,snapshot):
            if point=='after-result':raise RunInterrupted()
        try:await execution(checkpoint_hook=pause).harness.execute(run.run_id,identity)
        except RunInterrupted:pass
        before=runs.load(run.run_id,identity)
        recovered=await execution().harness.resume(run.run_id,identity)
        report['recovery']=(before.status=='RUNNING' and recovered.status=='COMPLETED'
            and before.results==recovered.results and before.read_request==recovered.read_request
            and recovered.tool_calls==1 and recovered.model_calls==2
            and recovered.active_seconds>=before.active_seconds)
        positives=[r for c,r in zip(data['cases'],report['cases']) if c['expected_status']=='ANSWERED']
        negatives=[r for c,r in zip(data['cases'],report['cases']) if c['expected_status']=='ABSTAINED']
        valid=sum(r['status']=='COMPLETED' and r['answer_status'] in ('ANSWERED','ABSTAINED') for r in report['cases'])
        report.update(answer_accuracy=sum(r['passed'] for r in positives)/len(positives),
            abstention_recall=sum(r['passed'] for r in negatives)/len(negatives),
            false_abstentions=sum(r['answer_status']=='ABSTAINED' for r in positives),
            supported_citations=valid/len(report['cases']))
        report['passed']=all(report[key]>=value for key,value in data['gates'].items()) and report['recovery']
    except Exception as error:report['error']=error.code if isinstance(error,ContractError) else 'READER_EVALUATION_FAILED'
    finally:
        try:
            if knowledge:
                with runs._database() as db:collections=db.execute('SELECT collection_name FROM knowledge_indexes').fetchall()
                for row in collections:vectors.request('DELETE',BASE+'/'+row['collection_name'])
            assert schema.startswith('harness_reader_eval_') and len(schema.rsplit('_',1)[1])==32
            with runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            report['cleanup']=True
        except Exception:report.update(cleanup=False,passed=False,error='READER_CLEANUP_FAILED')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=RUNTIME/'evals/reader-latest.json')
    args=parser.parse_args();report=asyncio.run(evaluate());args.output.parent.mkdir(parents=True,exist_ok=True);atomic_json(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k!='cases'},ensure_ascii=False))
    return 0 if report['passed'] and report.get('cleanup') else 1


if __name__=='__main__':raise SystemExit(main())
