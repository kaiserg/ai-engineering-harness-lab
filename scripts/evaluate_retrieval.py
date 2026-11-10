"""Evaluación aislada de retrieval real: baseline vs modelo semántico, sin respuestas generadas."""
import argparse
import json
import math
from pathlib import Path
import sys
import statistics
import time
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from psycopg import sql
from harness.chroma_vectors import ChromaVectors,BASE
from harness.contracts import ContractError
from harness.embeddings import PROFILES
from harness.knowledge import Knowledge,digest
from harness.postgres_config import local_dsn,RUNTIME
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json
from harness.user_identity import UserIdentityStore

DATASET=Path(__file__).resolve().parents[1]/'evals/retrieval_cases.json'
UNANSWERABLE=DATASET.with_name('unanswerable_cases.json')


def grade(ranked,relevant):
    relevant=set(relevant)
    rank=next((i for i,key in enumerate(ranked,1) if key in relevant),None)
    return {'top1':int(bool(ranked) and ranked[0] in relevant),
            'recall_at_3':len(set(ranked[:3])&relevant)/len(relevant),
            'reciprocal_rank_at_3':1/rank if rank and rank<=3 else 0}


def grade_unanswerable(matches):
    # Ground truth belongs to this synthetic fixture; this is NOT a runtime answerability detector.
    return {'expected_action':'abstain', 'candidate_count':len(matches),
            'has_candidates':bool(matches)}


def run():
    data=json.loads(DATASET.read_text())
    negatives=json.loads(UNANSWERABLE.read_text())
    schema='harness_retrieval_eval_'+uuid4().hex
    runs=PostgresRunStore(local_dsn(),schema=schema);vectors=ChromaVectors()
    knowledge=None;report={'dataset':data['version'],'dataset_sha256':digest(DATASET.read_text()),'profiles':{},'passed':False,
        'unanswerable_dataset':negatives['version'],'unanswerable_sha256':digest(UNANSWERABLE.read_text()),
        'answerability_evaluated':False,'runtime_abstention_implemented':False,
        'passed_scope':'retrieval_gates_citations_access_and_cleanup_only'}
    try:
        directory=UserIdentityStore(runs);directory.bootstrap()
        auth=lambda:directory.resolve('reader-demo','workspace-alpha')
        knowledge=Knowledge(runs,auth)
        mapping={};content={}
        for document in data['documents']:
            identifier=knowledge.register(document['filename'],document['text'])['document_id']
            mapping[identifier]=document['id'];content[identifier]=document
        for profile,configuration in PROFILES.items():
            engine=Knowledge(runs,auth,configuration=configuration)
            started=time.monotonic();index=engine.rebuild();build_ms=(time.monotonic()-started)*1000
            cases=[]
            for case in data['queries']:
                started=time.monotonic();result=engine.search(case['query'],3);elapsed=(time.monotonic()-started)*1000
                ranked=[]
                for match in result['matches']:
                    identifier=match['citation'].split(':')[1]
                    document=content[identifier]
                    assert match['citation'].startswith('['+index['index_id']+':')
                    assert match['text']==document['text'] and match['filename']==document['filename']
                    assert match['trust']=='untrusted_document'
                    ranked.append(mapping[identifier])
                cases.append({'id':case['id'],**grade(ranked,case['relevant']),'ranked':ranked,'latency_ms':round(elapsed,2)})
            latencies=sorted(c['latency_ms'] for c in cases)
            report['profiles'][profile]={'configuration':configuration,'index_id':index['index_id'],
                'build_ms':round(build_ms,2),'queries':len(cases),'cases':cases,
                **{metric:round(sum(c[metric] for c in cases)/len(cases),4)
                   for metric in ('top1','recall_at_3','reciprocal_rank_at_3')},
                'latency_p50_ms':round(statistics.median(latencies),2),
                'latency_p95_ms':latencies[math.ceil(.95*len(latencies))-1]}
            negative_cases=[]
            for case in negatives['cases']:
                result=engine.search(case['query'],3)
                # Verify citations even for irrelevant candidates; valid source != sufficient evidence.
                for match in result['matches']:
                    identifier=match['citation'].split(':')[1]
                    assert match['citation'].startswith('['+index['index_id']+':')
                    assert match['text']==content[identifier]['text']
                    assert match['filename']==content[identifier]['filename']
                    assert match['trust']=='untrusted_document'
                negative_cases.append({'id':case['id'],**grade_unanswerable(result['matches']),
                    'ranked':[mapping[m['citation'].split(':')[1]] for m in result['matches']]})
            report['profiles'][profile]['unanswerable']={
                'cases':negative_cases,'queries':len(negative_cases),
                'queries_with_candidates':sum(c['has_candidates'] for c in negative_cases),
                'interpretation':'Candidates are not answers; this measures retrieval behavior, not abstention quality.'}
        # El motor semántico no puede entregar contenido de otro workspace/actor ni de un retirado.
        for actor,workspace in [('reader-beta','workspace-beta'),('no-access-reader','workspace-alpha')]:
            if actor=='no-access-reader':
                with runs._database() as db:
                    db.execute('INSERT INTO user_accounts(actor_id) VALUES (%s)',(actor,))
                    db.execute("INSERT INTO user_memberships(actor_id,workspace_id,roles,projects,repositories,jobs) VALUES (%s,%s,ARRAY['reader'],ARRAY['DEV'],ARRAY['demo/atlas'],ARRAY['atlas-tests'])",(actor,workspace))
            other=Knowledge(runs,lambda:directory.resolve(actor,workspace))
            assert other.search('responsable Atlas')['matches']==[]
        retired=next(identifier for identifier,key in mapping.items() if key=='release')
        knowledge.retire(retired)
        assert all(retired not in m['citation'] for m in knowledge.search('responsable Atlas')['matches'])
        semantic=report['profiles']['semantic']
        report.update(citations_valid=True,scope_leaks=0,retirement_excluded=True,
            passed=semantic['top1']>=data['gates']['semantic_top1'] and semantic['recall_at_3']>=data['gates']['semantic_recall_at_3'])
    except Exception as error:
        report['error']=error.code if isinstance(error,ContractError) else 'RETRIEVAL_EVALUATION_FAILED'
    finally:
        try:
            if knowledge:
                with runs._database() as db:collections=db.execute('SELECT collection_name FROM knowledge_indexes').fetchall()
                for row in collections:vectors.request('DELETE',BASE+'/'+row['collection_name'])
            assert schema.startswith('harness_retrieval_eval_') and len(schema.rsplit('_',1)[1])==32
            with runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            report['cleanup']=True
        except Exception:report.update(cleanup=False,passed=False,error='RETRIEVAL_CLEANUP_FAILED')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=RUNTIME/'evals/retrieval-latest.json')
    args=parser.parse_args();report=run();args.output.parent.mkdir(parents=True,exist_ok=True);atomic_json(args.output,report)
    print(json.dumps({key:value for key,value in report.items() if key!='profiles'},ensure_ascii=False))
    for profile,result in report['profiles'].items():
        print(json.dumps({'profile':profile,'unanswerable_queries_with_candidates':result.get('unanswerable',{}).get('queries_with_candidates'),**{key:result[key] for key in ('top1','recall_at_3','reciprocal_rank_at_3','latency_p50_ms','latency_p95_ms')}}))
    return 0 if report['passed'] and report.get('cleanup') else 1


if __name__=='__main__':raise SystemExit(main())
