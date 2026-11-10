"""Opt-in: requiere Chroma local levantado con scripts/knowledge.py up."""
import os
import unittest
from uuid import uuid4
from psycopg import sql
from harness.chroma_vectors import ChromaVectors,BASE
from harness.knowledge import Knowledge
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore


@unittest.skipUnless(os.environ.get('HARNESS_CHROMA_TESTS')=='1','Chroma opt-in')
class ChromaKnowledgeTests(unittest.TestCase):
    def test_real_inventory_index_query_without_storing_raw_text_in_chroma(self):
        schema='harness_chroma_it_'+uuid4().hex
        runs=PostgresRunStore(local_dsn(),schema=schema)
        vectors=ChromaVectors()
        try:
            directory=UserIdentityStore(runs);directory.bootstrap()
            credential=directory.issue('reader-demo','workspace-alpha')
            knowledge=Knowledge(runs,lambda:directory.authenticate('Bearer '+credential['token']),vectors)
            knowledge.register('atlas.md','La responsable del release Atlas es Marta.')
            index=knowledge.rebuild()
            result=knowledge.search('responsable release Atlas')
            self.assertEqual(result['index_id'],index['index_id'])
            self.assertIn('Marta',result['matches'][0]['text'])
            name='harness_rag_'+index['index_id']
            raw=vectors.request('POST',BASE+'/'+vectors.collection(name)+'/get',{'include':['documents']})
            self.assertEqual(raw['documents'],[None])
        finally:
            with runs._database() as db:
                rows=db.execute('SELECT collection_name FROM knowledge_indexes').fetchall()
            for row in rows:vectors.request('DELETE',BASE+'/'+row['collection_name'])
            self.assertRegex(schema,r'^harness_chroma_it_[0-9a-f]{32}$')
            with runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

    def test_real_process_exit_after_vector_write_recovers_without_activating_partial_candidate(self):
        import subprocess
        import sys
        import time
        from harness.knowledge_queue import KnowledgeQueue
        from harness.knowledge_worker import KnowledgeWorker
        schema='harness_chroma_it_'+uuid4().hex
        runs=PostgresRunStore(local_dsn(),schema=schema)
        vectors=ChromaVectors()
        try:
            directory=UserIdentityStore(runs);directory.bootstrap()
            knowledge=Knowledge(runs,lambda:directory.resolve('reader-demo','workspace-alpha'))
            knowledge.register('atlas.md','Marta es responsable del release Atlas.')
            previous=knowledge.rebuild()['index_id']
            queue=KnowledgeQueue(knowledge);job=queue.enqueue(str(uuid4()))
            program=f'''
import os
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore
from harness.knowledge import Knowledge
from harness.knowledge_queue import KnowledgeQueue
from harness.knowledge_worker import KnowledgeWorker
from harness.chroma_vectors import ChromaVectors
runs=PostgresRunStore(local_dsn(),schema={schema!r})
directory=UserIdentityStore(runs)
knowledge=Knowledge(runs,lambda:directory.resolve('reader-demo','workspace-alpha'))
class ExitAfterWrite(ChromaVectors):
    def build(self,*args):
        super().build(*args)
        os._exit(23)
KnowledgeWorker(KnowledgeQueue(knowledge),lease_seconds=2,vectors_factory=lambda check:ExitAfterWrite(check)).run_once()
'''
            child=subprocess.run([sys.executable,'-c',program],capture_output=True,timeout=30)
            self.assertEqual(child.returncode,23)
            first=queue.get(job['job_id'])
            self.assertEqual(first['status'],'RUNNING')
            self.assertEqual(knowledge.search('Marta')['index_id'],previous)
            time.sleep(2.1)  # Lease real de dos segundos; no modifica el reloj PostgreSQL.
            self.assertEqual(KnowledgeWorker(queue).run_once()['status'],'DONE')
            recovered=queue.get(job['job_id'])
            self.assertEqual(recovered['attempts'],2)
            self.assertNotEqual(recovered['index_id'],first['index_id'])
            self.assertEqual(recovered['deadline_at'],first['deadline_at'])
            self.assertEqual(knowledge.search('Marta')['index_id'],recovered['index_id'])
        finally:
            with runs._database() as db:rows=db.execute('SELECT collection_name FROM knowledge_indexes').fetchall()
            for row in rows:
                # Todos los candidatos llegaron a crear colección en esta prueba.
                vectors.request('DELETE',BASE+'/'+row['collection_name'])
            self.assertRegex(schema,r'^harness_chroma_it_[0-9a-f]{32}$')
            with runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
