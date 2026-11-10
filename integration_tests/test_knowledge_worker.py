"""Cola/worker RAG sobre PostgreSQL real: muerte, fencing, permisos y commit atómico."""
import unittest
from uuid import uuid4
from harness.contracts import ContractError,Identity
from harness.knowledge import Knowledge
from harness.knowledge_queue import KnowledgeQueue
from harness.knowledge_worker import KnowledgeWorker
from integration_tests import test_knowledge as inventory_tests


class CheckedVectors:
    def __init__(self,delegate,check):self.delegate,self.check=delegate,check
    def build(self,*args):self.check();self.delegate.build(*args);self.check()


class KnowledgeWorkerTests(unittest.TestCase):
    setUp=inventory_tests.KnowledgeIntegrationTests.setUp
    cleanup=inventory_tests.KnowledgeIntegrationTests.cleanup
    register=inventory_tests.KnowledgeIntegrationTests.register
    index=inventory_tests.KnowledgeIntegrationTests.index

    def queue(self):return KnowledgeQueue(self.knowledge)
    def enqueue(self,key=None):return self.queue().enqueue(key or str(uuid4()))
    def job(self,job):return self.queue().get(job['job_id'])
    def worker(self,**kwargs):
        return KnowledgeWorker(self.queue(),vectors_factory=lambda check:CheckedVectors(self.vectors,check),**kwargs)
    def expire(self,job):
        with self.runs._database() as db:db.execute("UPDATE knowledge_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=%s",(job['job_id'],))

    def test_enqueue_is_idempotent_and_completion_is_atomic(self):
        self.register();key=str(uuid4());job=self.enqueue(key)
        self.assertEqual(job,self.enqueue(key))
        self.assertEqual(job['status'],'PENDING')
        self.assertEqual(self.worker().run_once()['status'],'DONE')
        result=self.job(job)
        self.assertEqual(result['status'],'DONE');self.assertEqual(result['attempts'],1)
        self.assertEqual(result['index_id'],self.knowledge.search('Marta')['index_id'])
        repeated=self.enqueue(key)
        self.assertEqual(repeated['deadline_at'],job['deadline_at']);self.assertEqual(repeated['attempts'],1)
        self.assertIsNone(self.worker().run_once())

    def test_process_interruption_recovers_same_job_with_new_candidate(self):
        self.register();previous=self.index();job=self.enqueue()
        def interrupt():raise KeyboardInterrupt()
        self.vectors.before_build=interrupt
        with self.assertRaises(KeyboardInterrupt):self.worker().run_once()
        first=self.job(job)
        self.assertEqual(first['status'],'RUNNING')
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)
        self.assertIsNone(self.worker().run_once())
        self.expire(job);self.vectors.before_build=lambda:None
        self.assertEqual(self.worker().run_once()['status'],'DONE')
        recovered=self.job(job)
        self.assertEqual(recovered['job_id'],first['job_id'])
        self.assertNotEqual(recovered['index_id'],first['index_id'])
        self.assertEqual(recovered['attempts'],2)
        self.assertEqual(recovered['deadline_at'],first['deadline_at'])
        with self.runs._database() as db:
            old=db.execute('SELECT state FROM knowledge_indexes WHERE index_id=%s',(first['index_id'],)).fetchone()
        self.assertEqual(old['state'],'FAILED')

    def test_old_owner_cannot_activate_after_reclaim(self):
        self.register();previous=self.index();job=self.enqueue()
        old=self.queue().claim('old')
        def reclaim():
            self.expire(job)
            self.assertIsNotNone(self.queue().claim('new'))
        self.vectors.before_build=reclaim
        with self.assertRaisesRegex(ContractError,'LEASE_LOST'):
            self.knowledge.rebuild(guard=lambda db,**kw:self.queue().guard(db,old,**kw))
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)
        with self.runs._database() as db:row=db.execute('SELECT owner,fencing_token FROM knowledge_jobs').fetchone()
        self.assertEqual(row['owner'],'new');self.assertEqual(row['fencing_token'],old.fencing_token+1)
        self.assertFalse(self.queue().fail(old,'CHROMA_UNAVAILABLE'))

    def test_document_retired_during_build_prevents_activation(self):
        doc=self.register();previous=self.index();job=self.enqueue()
        self.vectors.before_build=lambda:self.knowledge.retire(doc)
        result=self.worker().run_once()
        self.assertEqual(result['error'],'STALE_INVENTORY')
        self.assertEqual(self.job(job)['status'],'FAILED')
        self.assertEqual(self.knowledge.search('Marta'),{'index_id':previous,'matches':[]})

    def test_inventory_changed_while_queued_requires_new_request(self):
        self.register();self.index();job=self.enqueue()
        self.knowledge.register('new.md','Otro documento Atlas')
        before=len(self.vectors.collections)
        self.assertEqual(self.worker().run_once()['error'],'STALE_INVENTORY')
        self.assertEqual(len(self.vectors.collections),before)
        self.assertEqual(self.job(job)['status'],'FAILED')

    def test_expired_deadline_is_not_reset_by_repeated_enqueue_or_claim(self):
        self.register();key=str(uuid4());job=self.enqueue(key)
        with self.runs._database() as db:db.execute("UPDATE knowledge_jobs SET deadline_at=clock_timestamp()-interval '1 second'")
        deadline=self.enqueue(key)['deadline_at']
        self.assertIsNone(self.worker().run_once())
        result=self.job(job)
        self.assertEqual(result['last_error'],'INGEST_DEADLINE')
        self.assertEqual(result['attempts'],0);self.assertEqual(result['deadline_at'],deadline)

    def test_deadline_expired_during_io_cannot_activate(self):
        self.register();previous=self.index();job=self.enqueue()
        def expire():
            with self.runs._database() as db:db.execute("UPDATE knowledge_jobs SET deadline_at=clock_timestamp()-interval '1 second'")
        self.vectors.before_build=expire
        self.assertEqual(self.worker().run_once()['error'],'INGEST_DEADLINE')
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)
        self.assertEqual(self.job(job)['status'],'FAILED')

    def test_transport_retries_are_bounded(self):
        self.register();previous=self.index();job=self.enqueue()
        def fail():raise ContractError('CHROMA_UNAVAILABLE')
        self.vectors.before_build=fail
        for attempt in range(1,4):
            self.worker().run_once()
            self.assertEqual(self.job(job)['attempts'],attempt)
        self.assertEqual(self.job(job)['status'],'FAILED')
        self.assertIsNone(self.worker().run_once())
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)

    def test_current_grants_are_required_after_session_expires(self):
        self.register();job=self.enqueue()
        # Worker usa resolución del directorio, no el bearer que admitió el job.
        service=Knowledge(self.runs,lambda:self.directory.resolve('reader-demo','workspace-alpha'),self.vectors)
        worker=KnowledgeWorker(KnowledgeQueue(service),vectors_factory=lambda check:CheckedVectors(self.vectors,check))
        self.directory.revoke(self.credential['session_id'])
        self.assertEqual(worker.run_once()['status'],'DONE')
        self.assertEqual(KnowledgeQueue(service).get(job['job_id'])['status'],'DONE')

    def test_revoked_operator_during_io_has_no_activation(self):
        self.register();previous=self.index();job=self.enqueue()
        def revoke():
            with self.runs._database() as db:db.execute("UPDATE user_memberships SET roles=ARRAY['reader'] WHERE actor_id='reader-demo'")
        self.vectors.before_build=revoke
        self.assertEqual(self.worker().run_once()['error'],'FORBIDDEN')
        self.assertEqual(self.job(job)['status'],'FAILED')
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)

    def test_cross_workspace_and_actor_cannot_inspect_job(self):
        self.register();job=self.enqueue()
        for actor,workspace in [('other','workspace-alpha'),('reader-demo','workspace-beta')]:
            other=Knowledge(self.runs,lambda:Identity(actor,workspace,frozenset({'operator'}),frozenset()),self.vectors)
            with self.assertRaisesRegex(ContractError,'NOT_FOUND'):KnowledgeQueue(other).get(job['job_id'])
            self.assertIsNone(KnowledgeQueue(other).claim('other-worker'))

    def test_queue_admission_is_bounded(self):
        self.register()
        for _ in range(16):self.enqueue()
        with self.assertRaisesRegex(ContractError,'QUEUE_FULL'):self.enqueue()

    def test_failure_at_completion_rolls_back_pointer_and_done_together(self):
        self.register();previous=self.index();job=self.enqueue()
        queue=self.queue();original=queue.guard
        def fail_at_commit(db,claim,**values):
            if values.get('complete'):raise RuntimeError('simulated commit boundary')
            return original(db,claim,**values)
        queue.guard=fail_at_commit
        worker=KnowledgeWorker(queue,vectors_factory=lambda check:CheckedVectors(self.vectors,check))
        self.assertEqual(worker.run_once()['error'],'INGEST_FAILED')
        self.assertEqual(self.knowledge.search('Marta')['index_id'],previous)
        self.assertEqual(self.job(job)['status'],'FAILED')
