import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4
import httpx
from harness.contracts import Run,ToolResult,FinalAnswer,ContractError,ToolCall
from harness.composition import demo_identity
from harness.jira_chat import JiraReader,PROFILE,finish,payload
from harness.jira_cloud import binding_for,load,fetch,site_origin,plain_adf


class JiraChatTests(unittest.TestCase):
    def run_case(self):return Run('workspace-alpha','reader-demo',execution_profile=PROFILE,
        read_request={'query':'Consultá DEV-123','conversation_id':str(uuid4()),'history_run_ids':[]})
    def test_tool_key_must_come_from_user_or_history(self):
        reader=JiraReader(None,None);run=self.run_case()
        content=json.dumps({'name':'jira_get_issue','arguments':{'issue_key':'DEV-999'}})
        with self.assertRaisesRegex(ContractError,'INVALID_MODEL_OUTPUT'):reader.parse_call(run,content,[{'role':'user','content':'DEV-123'}])
        content=json.dumps({'name':'jira_get_issue','arguments':{'issue_key':'DEV-123'}})
        self.assertEqual(reader.parse_call(run,content,[{'role':'user','content':'DEV-123'}]).name,'jira_get_issue')
        with self.assertRaises(ContractError):reader.parse_call(run,json.dumps({'name':'jira_delete_issue','arguments':{'issue_key':'DEV-123'}}),[])

    def test_ticket_format_does_not_change_after_jsonb_reorders_fields(self):
        run=self.run_case();data={'issue_key':'DEV-123','status':'IN_REVIEW','description':'Demo'}
        run.results=[ToolResult('call',data,'jira:DEV-123:v1')]
        original=payload(run)
        run.results[0].data.clear();run.results[0].data.update({'description':'Demo','status':'IN_REVIEW','issue_key':'DEV-123'})
        self.assertEqual(payload(run),original)

    def test_missing_key_is_a_clarification_without_tool(self):
        run=self.run_case();reader=JiraReader(None,None)
        decision=reader.parse_call(run,json.dumps({'name':'need_issue_key','arguments':{'issue_key':''}}),[])
        answer,assessment=finish(run,decision)
        self.assertEqual(assessment['answer_status'],'NEEDS_ISSUE_KEY');self.assertIn('clave',answer.text)
        invented=reader.parse_call(run,json.dumps({'name':'jira_get_issue','arguments':{'issue_key':'DEV-999'}}),[{'role':'user','content':'Revisá un ticket'}])
        self.assertIsInstance(invented,FinalAnswer)

    def test_response_requires_exact_ticket_source_and_quote(self):
        run=self.run_case();run.results=[ToolResult('call',{'issue_key':'DEV-123','status':'IN_REVIEW'},'jira:DEV-123:v1')]
        output={'status':'answered','answer':'En revisión','evidence':[{'citation':'jira:DEV-123:v1','quote':'status: IN_REVIEW'}]}
        self.assertIn('jira:DEV-123:v1',finish(run,FinalAnswer(json.dumps(output)))[0].text)
        for field,value in [('citation','jira:OTHER-9:v1'),('quote','Estado inventado')]:
            invalid=json.loads(json.dumps(output));invalid['evidence'][0][field]=value
            with self.assertRaisesRegex(ContractError,'INVALID_MODEL_OUTPUT'):finish(run,FinalAnswer(json.dumps(invalid)))


class JiraCloudTests(unittest.TestCase):
    def config(self):return {'site':'https://example.atlassian.net','cloud_id':None,'email':'demo@example.test',
        'token':'synthetic-test-value','projects':['DEV'],'actor':'reader-demo','workspace':'workspace-alpha'}
    def test_configuration_is_private_and_pins_destination_not_token(self):
        with tempfile.TemporaryDirectory() as temp,patch('harness.jira_cloud.DIRECTORY',Path(temp)):
            data=self.config();binding=binding_for(data);path=Path(temp)/(binding+'.json');path.write_text(json.dumps(data));path.chmod(0o600)
            self.assertEqual(load(binding)['projects'],['DEV'])
            data['token']='rotated-synthetic-value';path.write_text(json.dumps(data));self.assertEqual(load(binding)['token'],data['token'])
            data['site']='https://different.atlassian.net';path.write_text(json.dumps(data))
            with self.assertRaises(ContractError):load(binding)
            path.chmod(0o644)
            with self.assertRaises(ContractError):load(binding)

    def test_only_fixed_https_get_and_whitelisted_fields_no_redirects_or_proxies(self):
        data=self.config();binding=binding_for(data);requests=[]
        def handler(request):
            requests.append(request);self.assertEqual(request.method,'GET');self.assertEqual(request.url.host,'example.atlassian.net')
            self.assertEqual(request.url.path,'/rest/api/3/issue/DEV-123');self.assertIn('Authorization',request.headers)
            self.assertEqual(request.url.params['fields'],'summary,status,description,updated,project')
            return httpx.Response(200,json={'key':'DEV-123','fields':{}})
        client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch('harness.jira_cloud.load',return_value=data),patch('harness.jira_cloud.httpx.Client',return_value=client) as factory:
            fetch(binding,demo_identity(),'DEV-123')
        self.assertFalse(factory.call_args.kwargs['follow_redirects']);self.assertFalse(factory.call_args.kwargs['trust_env'])
        self.assertEqual(len(requests),1)

    def test_gateway_for_scoped_token_and_access_probe_is_minimal(self):
        data=self.config();data['cloud_id']=str(uuid4())
        def handler(request):
            self.assertEqual(request.url.host,'api.atlassian.com');self.assertEqual(request.url.params['fields'],'updated')
            self.assertTrue(request.url.path.startswith('/ex/jira/'+data['cloud_id']+'/'))
            return httpx.Response(200,json={'key':'DEV-123','fields':{'updated':'today'}})
        client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch('harness.jira_cloud.load',return_value=data),patch('harness.jira_cloud.httpx.Client',return_value=client):
            fetch(binding_for(data),demo_identity(),'DEV-123',access_only=True)

    def test_unauthorized_project_never_opens_http_and_errors_are_sanitized(self):
        data=self.config()
        with patch('harness.jira_cloud.load',return_value=data),patch('harness.jira_cloud.httpx.Client') as client:
            with self.assertRaisesRegex(ContractError,'FORBIDDEN'):fetch(binding_for(data),demo_identity(),'PROD-1')
            client.assert_not_called()
        for status,code in [(401,'FORBIDDEN'),(403,'FORBIDDEN'),(404,'NOT_FOUND'),(429,'UPSTREAM_UNAVAILABLE'),(302,'UPSTREAM_UNAVAILABLE')]:
            client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(status,text='PRIVATE-SENTINEL')))
            with patch('harness.jira_cloud.load',return_value=data),patch('harness.jira_cloud.httpx.Client',return_value=client):
                with self.assertRaisesRegex(ContractError,code) as error:fetch(binding_for(data),demo_identity(),'DEV-123')
                self.assertNotIn('PRIVATE',str(error.exception))

    def test_adf_is_bounded_and_does_not_follow_embedded_links(self):
        node={'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':'Hola','marks':[{'type':'link','attrs':{'href':'https://evil.example'}}]}]}]}
        self.assertEqual(plain_adf(node),'Hola')
        for _ in range(35):node={'content':[node]}
        with self.assertRaises(ContractError):plain_adf(node)
        for url in ['http://example.atlassian.net','https://example.atlassian.net.evil.test','https://name:secret@example.atlassian.net','https://127.0.0.1']:
            with self.assertRaises(ContractError):site_origin(url)
