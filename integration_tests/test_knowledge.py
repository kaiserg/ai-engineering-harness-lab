"""Inventario real PostgreSQL; fallos/control de carreras inyectados en vectores."""
import unittest
from uuid import uuid4
from psycopg import sql
from harness.contracts import ContractError,Identity
from harness.knowledge import Knowledge
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore


class FakeVectors:
    def __init__(self):self.collections={};self.before_build=lambda:None;self.before_query=lambda:None
    def build(self,name,chunks,embed):
        self.collections[name]=list(chunks);self.before_build()
    def query(self,name,vector,documents,limit):
        self.before_query()
        return [(c['chunk_id'],0.1) for c in self.collections[name] if c['document_id'] in documents][:limit]


class KnowledgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.schema='harness_rag_it_'+uuid4().hex
        self.runs=PostgresRunStore(local_dsn(),schema=self.schema)
        self.addCleanup(self.cleanup)
        self.directory=UserIdentityStore(self.runs);self.directory.bootstrap()
        self.credential=self.directory.issue('reader-demo','workspace-alpha')
        self.vectors=FakeVectors()
        self.knowledge=Knowledge(self.runs,lambda:self.directory.authenticate('Bearer '+self.credential['token']),self.vectors)

    def cleanup(self):
        self.assertRegex(self.schema,r'^harness_rag_it_[0-9a-f]{32}$')
        with self.runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def register(self):return self.knowledge.register('atlas.md','Marta es responsable del release de Atlas.')['document_id']
    def index(self):return self.knowledge.rebuild()['index_id']

    def test_index_citation_and_retirement_without_deleting_vectors(self):
        document=self.register();index=self.index()
        result=self.knowledge.search('responsable Atlas')
        self.assertEqual(result['index_id'],index);self.assertIn('Marta',result['matches'][0]['text'])
        self.assertEqual(result['matches'][0]['trust'],'untrusted_document')
        self.knowledge.retire(document)
        self.assertEqual(self.knowledge.search('Atlas')['matches'],[])
        self.assertEqual(len(self.vectors.collections),1)

    def test_failed_candidate_keeps_previous_active_index(self):
        self.register();previous=self.index()
        def fail():raise ContractError('CHROMA_UNAVAILABLE')
        self.vectors.before_build=fail
        with self.assertRaisesRegex(ContractError,'CHROMA_UNAVAILABLE'):self.index()
        self.assertEqual(self.knowledge.search('Atlas')['index_id'],previous)
        with self.runs._database() as db:
            self.assertEqual(db.execute("SELECT count(*) n FROM knowledge_indexes WHERE state='FAILED'").fetchone()['n'],1)

    def test_inventory_change_during_build_cannot_activate(self):
        document=self.register();previous=self.index()
        self.vectors.before_build=lambda:self.knowledge.retire(document)
        with self.assertRaisesRegex(ContractError,'STALE_INDEX'):self.index()
        self.assertEqual(self.knowledge.search('Atlas'),{'index_id':previous,'matches':[]})
        with self.runs._database() as db:
            self.assertEqual(db.execute("SELECT count(*) n FROM knowledge_indexes WHERE state='STALE'").fetchone()['n'],1)

    def test_withdrawn_grants_before_activation_preserve_old_pointer(self):
        self.register();previous=self.index()
        def revoke():self.directory.revoke(self.credential['session_id'])
        self.vectors.before_build=revoke
        with self.assertRaisesRegex(ContractError,'UNAUTHENTICATED'):self.index()
        with self.runs._database() as db:
            self.assertEqual(db.execute('SELECT active_index FROM knowledge_inventory').fetchone()['active_index'],previous)

    def test_workspace_and_same_workspace_acl_plus_forged_ids(self):
        doc=self.register();self.index()
        beta=Knowledge(self.runs,lambda:Identity('reader-demo','workspace-beta',frozenset({'reader','operator'}),frozenset()),self.vectors)
        self.assertEqual(beta.documents(),[]);self.assertEqual(beta.search('Marta')['matches'],[])
        other=Knowledge(self.runs,lambda:Identity('other','workspace-alpha',frozenset({'reader'}),frozenset()),self.vectors)
        self.assertEqual(other.documents(),[])
        self.vectors.query=lambda *args:[(doc+':0',0.0)]
        self.assertEqual(other.search('Marta')['matches'],[])  # No confiar en el filtro del vector store.
        with self.assertRaisesRegex(ContractError,'FORBIDDEN'):other.register('x.md','text')
        with self.assertRaisesRegex(ContractError,'NOT_FOUND'):beta.retire(doc)

    def test_retirement_during_query_is_rechecked(self):
        document=self.register();self.index()
        self.vectors.before_query=lambda:self.knowledge.retire(document)
        self.assertEqual(self.knowledge.search('Marta')['matches'],[])

    def test_revocation_during_query_prevents_text_delivery(self):
        self.register();self.index()
        self.vectors.before_query=lambda:self.directory.revoke(self.credential['session_id'])
        with self.assertRaisesRegex(ContractError,'UNAUTHENTICATED'):self.knowledge.search('Marta')

    def test_acl_grants_are_explicit_and_limited_to_workspace(self):
        self.knowledge.register('shared.md','Marta release',['other'])
        self.index()
        other=Knowledge(self.runs,lambda:Identity('other','workspace-alpha',frozenset({'reader'}),frozenset()),self.vectors)
        self.assertEqual(len(other.search('Marta')['matches']),1)
        other_workspace=Knowledge(self.runs,lambda:Identity('other','workspace-beta',frozenset({'reader'}),frozenset()),self.vectors)
        self.assertEqual(other_workspace.search('Marta')['matches'],[])

    def test_quota_and_interruption_leave_existing_active_version(self):
        self.register();previous=self.index()
        def interrupt():raise KeyboardInterrupt()
        self.vectors.before_build=interrupt
        with self.assertRaises(KeyboardInterrupt):self.index()
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)
        with self.runs._database() as db:
            self.assertEqual(db.execute("SELECT count(*) n FROM knowledge_indexes WHERE state='BUILDING'").fetchone()['n'],1)
        with self.assertRaisesRegex(ContractError,'KNOWLEDGE_QUOTA'):
            self.knowledge.register('large.md','text '*150000)

    def test_pointer_changed_during_query_requires_retry(self):
        self.register();self.index()
        self.vectors.before_query=lambda:self.index()
        with self.assertRaisesRegex(ContractError,'STALE_INDEX'):self.knowledge.search('Marta')

    def test_corrupt_fragment_is_not_delivered(self):
        self.register();self.index()
        with self.runs._database() as db:db.execute("UPDATE knowledge_chunks SET content='tampered'")
        with self.assertRaisesRegex(ContractError,'CORRUPT_STATE'):self.knowledge.search('Marta')
