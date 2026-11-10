import json
from uuid import uuid4
import unittest
from unittest.mock import patch
from harness.chat import Chats
from harness.contracts import ContractError,ToolCall,FinalAnswer
from harness.worker import Worker
from harness.composition import compose
from harness.knowledge import Knowledge
from integration_tests import test_knowledge as inventory
from integration_tests import test_knowledge_api as api_tests
from harness.jira_chat import PROFILE


async def decision(model,run,allowed):
    if not run.results:return ToolCall('jira_get_issue',{'issue_key':'DEV-123'})
    return FinalAnswer(json.dumps({'status':'answered','answer':'En revisión','evidence':[
        {'citation':run.results[0].source_id,'quote':'status: IN_REVIEW'}]}))


class JiraChatIntegrationTests(unittest.IsolatedAsyncioTestCase):
    cleanup=inventory.KnowledgeIntegrationTests.cleanup
    async def asyncSetUp(self):
        inventory.KnowledgeIntegrationTests.setUp(self)
        self.knowledge=Knowledge(self.runs,lambda:self.directory.resolve('reader-demo','workspace-alpha'),self.vectors)
        self.identity=self.knowledge.identity();self.chats=Chats(self.knowledge);self.key=uuid4()
        self.id=str(self.chats.create(self.key,'jira-demo')['conversation_id'])
        p=patch('harness.jira_chat.JiraReader.decide',decision);p.start();self.addCleanup(p.stop)
    def worker(self):return Worker(self.runs,identity=self.identity,identity_resolver=self.knowledge.identity,
        composer=lambda profile,identity,store,**kwargs:compose(profile,identity,store,knowledge=self.knowledge,**kwargs))

    async def test_real_mcp_jira_and_recovery_use_saved_result(self):
        identifier=self.chats.send(self.id,uuid4(),'Consultá DEV-123',True)
        self.assertEqual((await self.worker().run_once())['event'],'paused')
        before=self.runs.load(identifier,self.identity);self.assertEqual(before.execution_profile,PROFILE)
        self.runs.requeue_job(identifier,self.identity)
        with patch('harness.adapters.mcp_jira.MCPJira.invoke',side_effect=AssertionError('repeated tool')):
            self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
        after=self.runs.load(identifier,self.identity)
        self.assertEqual(after.results,before.results);self.assertEqual((after.model_calls,after.tool_calls),(2,1))
        self.assertEqual(self.chats.view(self.id)['turns'][0]['assessment']['answer_status'],'ANSWERED')

    async def test_conversation_mode_is_frozen_and_idempotent(self):
        self.assertEqual(str(self.chats.create(self.key,'jira-demo')['conversation_id']),self.id)
        with self.assertRaisesRegex(ContractError,'CONFLICT'):self.chats.create(self.key,'documents')
        identifier=self.chats.send(self.id,uuid4(),'DEV-123');await self.worker().run_once()
        self.assertEqual(self.runs.load(identifier,self.identity).execution_profile,PROFILE)

    async def test_project_rejection_and_write_rejection_have_no_tool_effect(self):
        for call,code in [(ToolCall('jira_get_issue',{'issue_key':'PROD-1'}),'FORBIDDEN'),(ToolCall('jira_delete_issue',{'issue_key':'DEV-123'}),'UNKNOWN_TOOL')]:
            conversation=str(self.chats.create(uuid4(),'jira-demo')['conversation_id'])
            identifier=self.chats.send(conversation,uuid4(),'PROD-1')
            async def attack(*args):return call
            with patch('harness.jira_chat.JiraReader.decide',attack),patch('harness.adapters.mcp_jira.MCPJira.invoke',side_effect=AssertionError('must not connect')):
                await self.worker().run_once()
            result=self.runs.load(identifier,self.identity);self.assertEqual(result.outcome_code,code);self.assertEqual(result.tool_calls,0)

    async def test_cloud_history_revocation_hides_saved_ticket_and_keeps_binding(self):
        from harness.jira_cloud import binding_for,PREFIX
        from harness.contracts import ToolResult
        from hashlib import sha256
        config={'site':'https://example.atlassian.net','cloud_id':None,'projects':['DEV'],
            'actor':'reader-demo','workspace':'workspace-alpha','email':'demo@example.test','token':'synthetic-test-value'}
        binding=binding_for(config);key=uuid4()
        data={'issue_key':'DEV-123','status':'IN_REVIEW','summary':'Demo','description':'','updated':'2026-01-01T00:00:00Z'}
        source='jira-cloud:'+binding+':DEV-123:'+sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:12]
        async def read(adapter,identity,call):return ToolResult(call.call_id,data,source,tool_name='jira_get_issue')
        with patch('harness.jira_cloud.current',return_value=binding),patch('harness.jira_chat.load',return_value=config),patch('harness.jira_chat.fetch',return_value={'key':'DEV-123'}):
            conversation=str(self.chats.create(key,'jira-cloud')['conversation_id'])
            identifier=self.chats.send(conversation,uuid4(),'DEV-123')
            with patch('harness.adapters.mcp_jira_cloud.MCPJiraCloud.invoke',read):
                self.assertEqual((await self.worker().run_once())['status'],'COMPLETED')
            self.assertEqual(self.runs.load(identifier,self.identity).execution_profile,PREFIX+binding)
            with patch('harness.jira_cloud.current',side_effect=AssertionError('must keep original connection')):
                self.assertEqual(str(self.chats.create(key,'jira-cloud')['conversation_id']),conversation)
            with patch('harness.jira_chat.fetch',side_effect=ContractError('FORBIDDEN')):
                self.assertTrue(self.chats.view(conversation)['turns'][0]['unavailable'])

    async def test_current_project_revocation_blocks_history(self):
        identifier=self.chats.send(self.id,uuid4(),'DEV-123');await self.worker().run_once()
        with self.runs._database() as db:db.execute("UPDATE user_memberships SET projects=ARRAY[]::text[] WHERE actor_id='reader-demo'")
        with self.assertRaisesRegex(ContractError,'FORBIDDEN'):self.chats.view(self.id)


class JiraChatAPITests(unittest.TestCase):
    cleanup=api_tests.KnowledgeAPITests.cleanup
    setUp=api_tests.KnowledgeAPITests.setUp
    headers=api_tests.KnowledgeAPITests.headers
    def test_mode_is_a_preset_not_client_selected_endpoint_or_profile(self):
        base='/api/chat/conversations'
        response=self.client.post(base,headers=self.headers(),json={'request_key':str(uuid4()),'mode':'jira-demo'})
        self.assertEqual(response.status_code,201,response.text);identifier=response.json()['conversation_id']
        sent=self.client.post(base+'/'+identifier+'/messages',headers=self.headers(),json={'request_key':str(uuid4()),'query':'DEV-123'})
        self.assertEqual(sent.status_code,202,sent.text)
        self.assertEqual(self.client.get('/api/runs/'+sent.json()['run_id'],headers=self.headers()).status_code,404)
        for values in ({'mode':'https://evil.example'},{'mode':'jira-demo','endpoint':'https://evil.example'}):
            self.assertEqual(self.client.post(base,headers=self.headers(),json={'request_key':str(uuid4()),**values}).status_code,422)
