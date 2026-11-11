"""Ejecuta los adaptadores MCP reales con fixtures y un store en memoria."""
from dataclasses import asdict, replace
from types import SimpleNamespace
import unittest
from uuid import uuid4

from harness.chat import profile_for_mode
from harness.ci_chat import CI_LIMITS
from harness.checkpoints import RunInterrupted, pause_hook
from harness.composition import compose, demo_identity
from harness.contracts import Run
from harness.fakes import MemoryRunStore


class CIChatTests(unittest.IsolatedAsyncioTestCase):
    def setup_run(self, scenario, query='¿El build de DEV-123 pasó?', **options):
        self.identity = options.pop('identity', demo_identity())
        self.store = MemoryRunStore()
        profile = profile_for_mode(f'ci-{scenario}')
        run = Run(self.identity.workspace_id, self.identity.actor_id, execution_profile=profile,
                  limits_snapshot=asdict(CI_LIMITS), read_request={
                      'conversation_id': str(uuid4()), 'query': query, 'history_run_ids': []})
        self.store.create(run, self.identity)
        knowledge = SimpleNamespace(identity=lambda: self.identity)
        self.execution = compose(profile, self.identity, self.store, limits=CI_LIMITS,
                                 knowledge=knowledge, **options)
        self.run_id = run.run_id

    async def execute_run(self):
        return await self.execution.harness.resume(self.run_id, self.identity)

    async def test_four_scenarios_publish_distinct_assessments(self):
        for scenario, status, same_revision, success in (
            ('matching', 'VERIFIED', True, True), ('stale', 'STALE_BUILD', False, True),
            ('failed', 'BUILD_FAILED', True, False), ('unavailable', 'INCOMPLETE', None, None),
        ):
            with self.subTest(scenario=scenario):
                self.setup_run(scenario)
                result = await self.execute_run()
                self.assertEqual(result.status, 'COMPLETED')
                self.assertEqual(result.assessment['status'], status)
                self.assertIs(result.assessment['same_revision'], same_revision)
                self.assertIs(result.assessment['build_successful'], success)
                self.assertEqual((result.model_calls, result.tool_calls), (4, 3))
                self.assertEqual(len(result.assessment['sources']), 2 if scenario == 'unavailable' else 3)
                self.assertEqual('El build pasó para la revisión' in result.answer, scenario == 'matching')

    async def test_missing_or_ambiguous_ticket_does_not_invoke_mcp(self):
        for query in ('¿Cómo está CI?', '¿Y DEV-999?', '¿DEV-123 o DEV-999?'):
            with self.subTest(query=query):
                self.setup_run('matching', query)
                result = await self.execute_run()
                self.assertEqual(result.assessment['status'], 'NEEDS_TICKET')
                self.assertEqual(result.tool_calls, 0)
                self.assertEqual(self.execution.counters(), (0, 0))

    async def test_policy_blocks_jenkins_when_job_is_not_allowed(self):
        self.setup_run('matching', identity=replace(demo_identity(), jobs=frozenset()))
        result = await self.execute_run()
        self.assertEqual((result.status, result.outcome_code), ('FAILED', 'FORBIDDEN'))
        self.assertIsNone(result.answer)
        self.assertEqual(self.execution.adapters['jenkins'].connections, 0)

    async def test_resume_keeps_confirmed_jira_and_does_not_read_it_again(self):
        self.setup_run('stale', checkpoint_hook=pause_hook('jira'))
        with self.assertRaises(RunInterrupted):
            await self.execute_run()
        before = self.store.load(self.run_id, self.identity)
        result = await self.execute_run()
        self.assertEqual(result.assessment['status'], 'STALE_BUILD')
        self.assertEqual(result.results[0], before.results[0])
        self.assertEqual(self.execution.adapters['jira'].invocations, 1)
