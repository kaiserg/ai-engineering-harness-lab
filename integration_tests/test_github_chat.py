import json
from uuid import uuid4
import unittest
from unittest.mock import patch
from harness.chat import Chats
from harness.contracts import ToolCall,FinalAnswer,ContractError
from harness.composition import compose
from harness.knowledge import Knowledge
from harness.worker import Worker
from harness.github_chat import DEMO,PUBLIC
from integration_tests import test_knowledge as inventory
from integration_tests import test_knowledge_api as api_tests


async def decision(model,run,allowed):
    if not run.results:return ToolCall('github_get_pull_request',{'repository':'demo/atlas','pull_number':42})
    return FinalAnswer(json.dumps({'status':'answered','answer':'Está abierto.','evidence':[{'citation':run.results[0].source_id,'quote':'state: OPEN'}]}))


class GitHubChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    cleanup=inventory.KnowledgeIntegrationTests.cleanup
    async def asyncSetUp(self):
        inventory.KnowledgeIntegrationTests.setUp(self)
        self.knowledge=Knowledge(self.runs,lambda:self.directory.resolve('reader-demo','workspace-alpha'),self.vectors)
        self.identity=self.knowledge.identity();self.chats=Chats(self.knowledge);self.key=uuid4()
        self.id=str(self.chats.create(self.key,'github-demo')['conversation_id'])
        p=patch('harness.github_chat.GitHubReader.decide',decision);p.start();self.addCleanup(p.stop)
    def worker(self):return Worker(self.runs,identity=self.identity,identity_resolver=self.knowledge.identity,
        composer=lambda profile,identity,store,**kwargs:compose(profile,identity,store,knowledge=self.knowledge,**kwargs))

    async def test_mcp_and_recovery_keep_saved_revision_and_one_tool(self):
        identifier=self.chats.send(self.id,uuid4(),'demo/atlas#42',True)
        self.assertEqual((await self.worker().run_once())['event'],'paused')
        before=self.runs.load(identifier,self.identity);self.runs.requeue_job(identifier,self.identity)
        with patch('harness.adapters.mcp_github.MCPGitHub.invoke',side_effect=AssertionError('repeated tool')):
            self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
        after=self.runs.load(identifier,self.identity)
        self.assertEqual(after.results,before.results);self.assertEqual((after.model_calls,after.tool_calls),(2,1))
        self.assertIn('revision-A',after.results[0].source_id)

    async def test_frozen_mode_and_cross_scope_or_write_rejected(self):
        self.assertEqual(str(self.chats.create(self.key,'github-demo')['conversation_id']),self.id)
        with self.assertRaises(ContractError):self.chats.create(self.key,'documents')
        for call,code in [(ToolCall('github_get_pull_request',{'repository':'other/repo','pull_number':1}),'FORBIDDEN'),(ToolCall('github_merge_pull_request',{}),'UNKNOWN_TOOL')]:
            identifier=self.chats.send(self.id,uuid4(),'demo/atlas#42')
            async def attack(*args):return call
            with patch('harness.github_chat.GitHubReader.decide',attack),patch('harness.adapters.mcp_github.MCPGitHub.invoke',side_effect=AssertionError('must not connect')):
                await self.worker().run_once()
            saved=self.runs.load(identifier,self.identity);self.assertEqual(saved.outcome_code,code);self.assertEqual(saved.tool_calls,0)

    async def test_public_history_batches_visibility_probes_and_revocation_redacts_all_turns(self):
        from harness.github_public import source
        from harness.contracts import ToolResult
        data={'repository':'demo/atlas','pull_number':42,'state':'OPEN','head_revision':'a'*40,
            'title':'Demo','description':'','merged':False,'draft':False,'updated':'today'}
        async def read(adapter,identity,call):return ToolResult(call.call_id,data,source(data),tool_name='github_get_pull_request')
        conversation=str(self.chats.create(uuid4(),'github-public')['conversation_id'])
        with patch('harness.github_chat.public_access') as probe,patch('harness.adapters.mcp_github_public.MCPGitHubPublic.invoke',read):
            for _ in range(3):
                self.chats.send(conversation,uuid4(),'demo/atlas#42')
                self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
            probe.reset_mock()
            self.assertEqual(len(self.chats.view(conversation)['turns']),3)
            self.assertEqual(probe.call_count,1)
            probe.reset_mock();probe.side_effect=ContractError('NOT_FOUND')
            self.assertTrue(all(t['unavailable'] for t in self.chats.view(conversation)['turns']))
            self.assertEqual(probe.call_count,1)

    async def test_repository_revocation_blocks_saved_history(self):
        identifier=self.chats.send(self.id,uuid4(),'demo/atlas#42');await self.worker().run_once()
        with self.runs._database() as db:db.execute("UPDATE user_memberships SET repositories=ARRAY[]::text[] WHERE actor_id='reader-demo'")
        with self.assertRaisesRegex(ContractError,'FORBIDDEN'):self.chats.view(self.id)


class GitHubChatAPITests(unittest.TestCase):
    cleanup=api_tests.KnowledgeAPITests.cleanup
    setUp=api_tests.KnowledgeAPITests.setUp
    headers=api_tests.KnowledgeAPITests.headers
    def test_admission_is_not_inline_execution_and_ci_endpoint_cannot_deliver_it(self):
        base='/api/chat/conversations';created=self.client.post(base,headers=self.headers(),json={'request_key':str(uuid4()),'mode':'github-demo'})
        self.assertEqual(created.status_code,201,created.text);identifier=created.json()['conversation_id']
        with patch('harness.github_chat.GitHubReader.decide',side_effect=AssertionError('inline model')):
            sent=self.client.post(base+'/'+identifier+'/messages',headers=self.headers(),json={'request_key':str(uuid4()),'query':'demo/atlas#42'})
        self.assertEqual(sent.status_code,202,sent.text)
        self.assertEqual(self.client.get('/api/runs/'+sent.json()['run_id'],headers=self.headers()).status_code,404)
        self.assertEqual(self.client.get(base+'/'+identifier,headers=self.headers(self.beta)).status_code,404)
