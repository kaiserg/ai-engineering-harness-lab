"""Prueba que el enlace de un ticket solo habilita el PR configurado."""
from dataclasses import asdict
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from harness.ci_jira import LIMITS, JiraCIPlanner, JiraCIPolicy, finish
from harness.ci_live import Binding
from harness.composition import demo_identity
from harness.contracts import Run
from harness.core import Harness
from harness.fakes import MemoryRunStore
from harness.jira_cloud import JiraCloud
from harness.profiles import JIRA_CI_PREFIX


class JiraCIGuards(IsolatedAsyncioTestCase):
    async def test_untrusted_adf_cannot_select_another_pr_or_start_jenkins(self):
        identity = demo_identity()
        binding = Binding('http://127.0.0.1:18080','reader',identity.workspace_id,
                          identity.actor_id,'demo/atlas',42,'atlas-tests')
        jira_id = str(uuid4())
        profile = JIRA_CI_PREFIX+jira_id+':'+binding.profile.split(':')[1]
        config = {'actor':identity.actor_id,'workspace':identity.workspace_id,'projects':['DEV']}
        allowed_url = 'https://github.com/demo/atlas/pull/42'
        for nodes, status in (
            ([{'type':'text','text':'https://github.com.invalid/demo/atlas/pull/42'},
              {'type':'text','text':allowed_url,'marks':[{'type':'link','attrs':{
                  'href':'https://github.com.invalid/demo/atlas/pull/42'}}]}], 'MISSING_PR'),
            ([{'type':'text','text':'otro PR','marks':[{'type':'link','attrs':{
                'href':'https://github.com/other/repo/pull/99'}}]}], 'PR_NOT_ALLOWED'),
            ([{'type':'inlineCard','attrs':{'url':allowed_url}},
              {'type':'text','text':'https://github.com/demo/atlas/pull/43'}], 'AMBIGUOUS_PR'),
        ):
            with self.subTest(status=status):
                store = MemoryRunStore()
                run = Run(identity.workspace_id,identity.actor_id,execution_profile=profile,
                          limits_snapshot=asdict(LIMITS),read_request={'conversation_id':str(uuid4()),
                          'query':'¿Pasó el build 1 de DEV-123?','history_run_ids':[]})
                store.create(run,identity)
                outside = SimpleNamespace(invoke=AsyncMock(side_effect=AssertionError('unexpected downstream call')))
                harness = Harness(JiraCIPlanner(binding),JiraCIPolicy(SimpleNamespace(identity=lambda:identity),binding,jira_id),
                                  {'jira_get_issue':JiraCloud(include_pr=True),
                                   'github_get_pull_request':outside,'jenkins_get_build':outside},
                                  store,limits=LIMITS,execution_profile=profile,finalizer=lambda saved:finish(saved,binding))
                response = {'key':'DEV-123','fields':{'project':{'key':'DEV'},'summary':'Ticket','status':{'name':'Open'},
                            'updated':'now','description':{'type':'doc','content':nodes}}}
                with patch.dict('os.environ',HARNESS_JIRA_BINDING=jira_id),patch('harness.jira_cloud.fetch',return_value=response),patch('harness.jira_cloud.load',return_value=config):
                    result = await harness.resume(run.run_id,identity)
                self.assertEqual(result.assessment['status'],status)
                self.assertEqual(result.tool_calls,1)
                outside.invoke.assert_not_awaited()
