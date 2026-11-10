"""API autenticada y worker con permisos vivos, conservando evidencia y fencing."""
import asyncio
import unittest
from uuid import uuid4
from psycopg import sql
from fastapi.testclient import TestClient
from harness.composition import compose
from harness.contracts import Identity
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore
from harness.web import create_app
from harness.worker import Worker


class AuthenticatedRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='harness_auth_runs_'+uuid4().hex
        self.runs=PostgresRunStore(local_dsn(),schema=self.schema)
        self.addCleanup(self.cleanup)
        self.directory=UserIdentityStore(self.runs);self.directory.bootstrap()
        self.alpha=self.directory.issue('reader-demo','workspace-alpha')
        self.beta=self.directory.issue('reader-beta','workspace-beta')
        self.approver=self.directory.issue('approver-demo','workspace-alpha')
        app=create_app(storage='postgres',pg_dsn=local_dsn(),pg_schema=self.schema,user_auth=True,user_directory=self.directory)
        self.client=TestClient(app,base_url='http://127.0.0.1');self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)

    def cleanup(self):
        self.assertRegex(self.schema,r'^harness_auth_runs_[0-9a-f]{32}$')
        with self.runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def headers(self,credential=None):return {'Authorization':'Bearer '+(credential or self.alpha)['token'],'X-Harness-Client':'local-ui'}

    def start(self,credential=None,**values):
        response=self.client.post('/api/runs',headers=self.headers(credential),json={'scenario':'matching',**values})
        self.assertEqual(response.status_code,202,response.text)
        return response.json()

    def worker(self,actor='reader-demo',workspace='workspace-alpha',**kwargs):
        scope=Identity(actor,workspace,frozenset(),frozenset())
        return Worker(self.runs,identity=scope,identity_resolver=lambda:self.directory.resolve(actor,workspace),**kwargs)

    async def test_both_users_create_isolated_jobs_and_cannot_read_or_resume_each_other(self):
        alpha=self.start();beta=self.start(self.beta)
        self.assertEqual(alpha['requested_by'],'reader-demo');self.assertEqual(beta['workspace_id'],'workspace-beta')
        for credential,own,other in ((self.alpha,alpha,beta),(self.beta,beta,alpha)):
            rows=self.client.get('/api/runs',headers=self.headers(credential)).json()['runs']
            self.assertEqual([r['run_id'] for r in rows],[own['run_id']])
            self.assertEqual(self.client.get('/api/runs/'+other['run_id'],headers=self.headers(credential)).status_code,404)
            self.assertEqual(self.client.post('/api/runs/'+other['run_id']+'/resume',headers=self.headers(credential),json={}).status_code,404)
        # El worker anterior del schema original no puede ver este schema nuevo.
        other_schema='harness_legacy_it_'+uuid4().hex
        legacy=PostgresRunStore(local_dsn(),schema=other_schema)
        try:self.assertIsNone(await Worker(legacy).run_once())
        finally:
            with legacy._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(other_schema)))
        self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
        self.assertEqual((await self.worker('reader-beta','workspace-beta').run_once())['status'],'FAILED')
        beta_run=self.client.get('/api/runs/'+beta['run_id'],headers=self.headers(self.beta)).json()
        self.assertEqual(beta_run['outcome_code'],'NOT_FOUND')  # Los backends conservan sus fixtures alpha.

    async def test_another_reader_in_same_workspace_cannot_see_owner_history(self):
        with self.runs._database() as db:
            db.execute("INSERT INTO user_accounts(actor_id) VALUES ('reader-other')")
            db.execute("""INSERT INTO user_memberships(actor_id,workspace_id,roles,projects,repositories,jobs)
                SELECT 'reader-other',workspace_id,roles,projects,repositories,jobs FROM user_memberships
                WHERE actor_id='reader-demo' AND workspace_id='workspace-alpha'""")
        other=self.directory.issue('reader-other','workspace-alpha');run=self.start()
        self.assertEqual(self.client.get('/api/runs',headers=self.headers(other)).json()['runs'],[])
        self.assertEqual(self.client.get('/api/runs/'+run['run_id'],headers=self.headers(other)).status_code,404)

    async def test_recovery_keeps_confirmed_evidence_and_original_budgets(self):
        created=self.start(pause_at='jira');identifier=created['run_id']
        self.assertEqual((await self.worker().run_once())['event'],'paused')
        paused=self.client.get('/api/runs/'+identifier,headers=self.headers()).json()
        self.assertEqual((paused['model_calls'],paused['tool_calls']),(1,1))
        self.assertEqual(self.client.post('/api/runs/'+identifier+'/resume',headers=self.headers(),json={}).status_code,202)
        self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
        after=self.client.get('/api/runs/'+identifier,headers=self.headers()).json()
        self.assertEqual(after['results'][0],paused['results'][0])
        self.assertEqual(after['limits_snapshot'],paused['limits_snapshot'])
        self.assertEqual((after['model_calls'],after['tool_calls']),(4,3))
        self.assertNotIn(self.alpha['token'],str(after))
        self.assertNotIn(self.alpha['session_id'],str(after))

    async def test_role_revoked_after_enqueue_prevents_any_model_or_tool_io(self):
        created=self.start();self.directory.set_membership('reader-demo','workspace-alpha',frozenset({'approver'}))
        def forbidden_composer(*args,**kwargs):raise AssertionError('must not compose')
        result=await self.worker(composer=forbidden_composer).run_once()
        self.assertEqual(result['error'],'FORBIDDEN')
        run=self.runs.load(created['run_id'],self.directory.resolve('reader-demo','workspace-alpha'))
        self.assertEqual((run.model_calls,run.tool_calls),(0,0))
        self.assertEqual(run.status,'FAILED')

    async def test_revocation_at_tool_checkpoint_prevents_actual_dispatch(self):
        created=self.start();adapters={}
        def composer(*args,**kwargs):
            execution=compose(*args,**kwargs);adapters.update(execution.adapters);return execution
        def revoke(point,run):
            if point=='before-tool':self.directory.set_membership('reader-demo','workspace-alpha',frozenset(),enabled=False)
        result=await self.worker(composer=composer,checkpoint_hook=revoke).run_once()
        self.assertEqual(result['status'],'FAILED')
        self.assertEqual(adapters['jira'].invocations,0)
        scoped=Identity('reader-demo','workspace-alpha',frozenset(),frozenset())
        run=self.runs.load(created['run_id'],scoped)
        self.assertEqual(run.outcome_code,'FORBIDDEN')
        self.assertEqual(run.tool_calls,1)  # Reserva conservada; ausencia de dispatch no reinicia contadores.

    async def test_session_revocation_blocks_http_but_not_already_authorized_durable_intent(self):
        created=self.start();self.directory.revoke(self.alpha['session_id'])
        self.assertEqual(self.client.get('/api/runs',headers=self.headers()).status_code,401)
        result=await self.worker().run_once()
        self.assertEqual(result['status'],'COMPLETED')

    async def test_missing_credentials_role_scope_and_spoofed_actor_cannot_enqueue(self):
        self.assertEqual(self.client.post('/api/runs',json={}).status_code,401)
        self.assertEqual(self.client.get('/api/runs',headers=self.headers(self.approver)).status_code,403)
        headers=self.headers();headers['X-Harness-Workspace']='workspace-beta'
        self.assertEqual(self.client.post('/api/runs',headers=headers,json={}).status_code,403)
        headers=self.headers();headers.update({'X-Actor-ID':'approver-demo','X-Roles':'approver'})
        self.assertEqual(self.client.post('/api/runs',headers=headers,json={'actor_id':'approver-demo'}).status_code,422)
        result=self.client.post('/api/runs',headers=headers,json={})
        self.assertEqual(result.json()['requested_by'],'reader-demo')
        self.assertEqual(self.client.get('/api/operations/'+str(uuid4()),headers=headers).status_code,404)

    async def test_configuration_cannot_mix_fixture_m4_and_authenticated_routes(self):
        for args in ({'storage':'sqlite'},{'storage':'postgres','approvals_demo':True},{'storage':'postgres','pg_schema':'harness_lab'}):
            with self.assertRaises(ValueError):create_app(user_auth=True,**args)
