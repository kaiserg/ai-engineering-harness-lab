"""Versionado/pin de proveedor en la cola: no necesita modelo local para esta regresión."""
import unittest
from unittest.mock import patch
from uuid import uuid4
from harness.contracts import ContractError
from harness.embeddings import SEMANTIC_CONFIG,LEXICAL_CONFIG,provider_for
from harness.knowledge import Knowledge
from harness.knowledge_queue import KnowledgeQueue
from harness.knowledge_worker import KnowledgeWorker
from integration_tests import test_knowledge as inventory_tests


class DenseVectors(inventory_tests.FakeVectors):
    def build(self,name,chunks,embed):
        embed.documents([c['content'] for c in chunks])
        super().build(name,chunks,embed)


class SemanticKnowledgeTests(unittest.TestCase):
    setUp=inventory_tests.KnowledgeIntegrationTests.setUp
    cleanup=inventory_tests.KnowledgeIntegrationTests.cleanup
    register=inventory_tests.KnowledgeIntegrationTests.register
    index=inventory_tests.KnowledgeIntegrationTests.index

    def test_same_key_cannot_silently_switch_embedding_configuration(self):
        self.register();queue=KnowledgeQueue(self.knowledge);key=str(uuid4())
        first=queue.enqueue(key)
        with self.assertRaisesRegex(ContractError,'CONFLICT'):queue.enqueue(key,configuration=SEMANTIC_CONFIG)
        self.assertEqual(queue.get(first['job_id'])['configuration'],LEXICAL_CONFIG)

    def test_default_worker_respects_persisted_semantic_configuration(self):
        self.register();queue=KnowledgeQueue(self.knowledge)
        job=queue.enqueue(str(uuid4()),configuration=SEMANTIC_CONFIG);seen=[]
        class SemanticFixture:
            def documents(self,texts):seen.append('semantic');return [[1.0,0.0] for _ in texts]
        def factory(configuration,check=None):
            return SemanticFixture() if configuration==SEMANTIC_CONFIG else provider_for(configuration,check)
        vectors=DenseVectors()
        with patch('harness.knowledge.provider_for',side_effect=factory),patch('harness.knowledge_queue.provider_for',side_effect=factory):
            result=KnowledgeWorker(queue,vectors_factory=lambda check:vectors).run_once()
        self.assertEqual(result['status'],'DONE');self.assertEqual(seen,['semantic'])
        self.assertEqual(queue.get(job['job_id'])['configuration'],SEMANTIC_CONFIG)
        with self.runs._database() as db:
            self.assertEqual(db.execute('SELECT configuration FROM knowledge_indexes').fetchone()['configuration'],SEMANTIC_CONFIG)

    def test_model_change_fails_candidate_without_replacing_previous_index(self):
        self.register();previous=self.index();queue=KnowledgeQueue(self.knowledge)
        job=queue.enqueue(str(uuid4()),configuration=SEMANTIC_CONFIG)
        class BrokenModel:
            def documents(self,texts):raise ContractError('EMBEDDING_MODEL_CHANGED')
        def factory(configuration,check=None):
            return BrokenModel() if configuration==SEMANTIC_CONFIG else provider_for(configuration,check)
        with patch('harness.knowledge.provider_for',side_effect=factory),patch('harness.knowledge_queue.provider_for',side_effect=factory):
            result=KnowledgeWorker(queue,vectors_factory=lambda check:DenseVectors()).run_once()
        self.assertEqual(result['error'],'EMBEDDING_MODEL_CHANGED');self.assertEqual(queue.get(job['job_id'])['status'],'FAILED')
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)

    def test_query_uses_active_index_configuration_and_preserves_acl_after_vectorization(self):
        self.register();self.index();seen=[]
        class QueryFixture:
            def query(self,text):seen.append('semantic');return [1.0,0.0]
        with self.runs._database() as db:db.execute('UPDATE knowledge_indexes SET configuration=%s',(SEMANTIC_CONFIG,))
        original=self.knowledge.vectors.query
        def revoked(name,vector,documents,limit):
            self.directory.revoke(self.credential['session_id'])
            return original(name,vector,documents,limit)
        self.knowledge.vectors.query=revoked
        with patch('harness.knowledge.provider_for',return_value=QueryFixture()):
            with self.assertRaisesRegex(ContractError,'UNAUTHENTICATED'):self.knowledge.search('pregunta')
        self.assertEqual(seen,['semantic'])
