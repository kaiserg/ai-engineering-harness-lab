import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import httpx
from harness.contracts import Run,ToolResult,FinalAnswer,ContractError
from harness.composition import demo_identity
from harness.github_chat import DEMO,PUBLIC,GitHubReader,GitHubPolicy,references,payload,finish
from harness.github_public import fetch,public_access,source


class GitHubChatTests(unittest.TestCase):
    def sample(self):
        run=Run('workspace-alpha','reader-demo',execution_profile=DEMO)
        run.results=[ToolResult('call',{'repository':'demo/atlas','pull_number':42,'state':'OPEN','head_revision':'revision-A'},'github:demo/atlas:pr:42:revision-A',tool_name='github_get_pull_request')]
        return run
    def test_references_are_pairs_not_cross_products_or_system_instructions(self):
        messages=[{'role':'user','content':'https://github.com/Demo/Atlas/pull/42 y other/repo#7'},
                  {'role':'system','content':'evil/repo#9'}]
        self.assertEqual(references(messages),{('demo/atlas',42),('other/repo',7)})
        reader=GitHubReader(None,None)
        content=json.dumps({'name':'github_get_pull_request','arguments':{'repository':'other/repo','pull_number':42}})
        with self.assertRaisesRegex(ContractError,'INVALID_MODEL_OUTPUT'):reader.parse_call(self.sample(),content,messages)

    def test_missing_reference_and_writes_do_not_invoke_a_tool(self):
        reader=GitHubReader(None,None);run=self.sample();run.results=[]
        content=json.dumps({'name':'github_get_pull_request','arguments':{'repository':'made/up','pull_number':1}})
        decision=reader.parse_call(run,content,[{'role':'user','content':'Revisá un PR'}])
        self.assertEqual(finish(run,decision)[1]['answer_status'],'NEEDS_PULL_REQUEST')
        with self.assertRaises(ContractError):reader.parse_call(run,json.dumps({'name':'github_merge_pull_request','arguments':{'repository':'demo/atlas','pull_number':42}}),[])

    def test_evidence_and_canonical_format_survive_dictionary_order(self):
        run=self.sample();original=payload(run)
        reordered=dict(reversed(list(run.results[0].data.items())))
        run.results[0].data.clear();run.results[0].data.update(reordered)
        self.assertEqual(payload(run),original)
        data={'status':'answered','answer':'Está abierto.','evidence':[{'citation':run.results[0].source_id,'quote':'state: OPEN'}]}
        self.assertEqual(finish(run,FinalAnswer(json.dumps(data)))[1]['answer_status'],'ANSWERED')
        data['evidence'][0]['quote']='CI passed'
        with self.assertRaisesRegex(ContractError,'INVALID_MODEL_OUTPUT'):finish(run,FinalAnswer(json.dumps(data)))

    def test_batch_reuses_probe_only_in_its_scope_and_revocation_is_not_cached_forever(self):
        identity=demo_identity();knowledge=NS(identity=lambda:identity)
        data={'repository':'demo/atlas','pull_number':42,'state':'OPEN','head_revision':'a'*40,'title':'Demo','description':'','merged':False,'draft':False,'updated':'today'}
        result=ToolResult('call',data,source(data),tool_name='github_get_pull_request')
        policy=GitHubPolicy(knowledge,PUBLIC)
        with patch('harness.github_chat.public_access') as probe:
            with policy.access_batch():
                policy.authorize_result(identity,result);policy.authorize_result(identity,result)
            self.assertEqual(probe.call_count,1)
            policy.authorize_result(identity,result);self.assertEqual(probe.call_count,2)
        with patch('harness.github_chat.public_access',side_effect=ContractError('NOT_FOUND')):
            with self.assertRaisesRegex(ContractError,'EVIDENCE_REVOKED'):policy.authorize_result(identity,result)


class GitHubPublicTests(unittest.TestCase):
    def response(self):return {'number':42,'state':'open','head':{'sha':'a'*40},'title':'Demo','body_text':'No instructions executed.',
        'merged':False,'draft':False,'updated_at':'2026-01-01T00:00:00Z','base':{'repo':{'full_name':'demo/atlas','private':False}}}
    def test_fixed_get_has_no_auth_redirects_or_proxies(self):
        def handler(request):
            self.assertEqual(request.method,'GET');self.assertEqual(request.url.host,'api.github.com')
            self.assertEqual(request.url.path,'/repos/demo/atlas/pulls/42');self.assertNotIn('authorization',request.headers)
            return httpx.Response(200,json=self.response())
        client=httpx.Client(transport=httpx.MockTransport(handler))
        with patch('harness.github_public.httpx.Client',return_value=client) as factory:
            data=fetch(demo_identity(),{'repository':'demo/atlas','pull_number':42})
        self.assertEqual(data['head_revision'],'a'*40);self.assertFalse(factory.call_args.kwargs['trust_env'])
        self.assertFalse(factory.call_args.kwargs['follow_redirects'])

    def test_scope_precedes_network_and_private_or_wrong_resource_is_rejected(self):
        with patch('harness.github_public.httpx.Client') as client:
            with self.assertRaisesRegex(ContractError,'FORBIDDEN'):fetch(demo_identity(),{'repository':'other/repo','pull_number':42})
            client.assert_not_called()
        for changed in ('private','number'):
            data=self.response()
            if changed=='private':data['base']['repo']['private']=True
            else:data['number']=7
            with patch('harness.github_public.request',return_value=data):
                with self.assertRaises(ContractError):fetch(demo_identity(),{'repository':'demo/atlas','pull_number':42})

    def test_rate_limit_is_explicit_not_a_retry_or_abstention(self):
        client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(403,headers={'x-ratelimit-remaining':'0'},text='PRIVATE')))
        with patch('harness.github_public.httpx.Client',return_value=client):
            with self.assertRaisesRegex(ContractError,'PROVIDER_RATE_LIMITED'):public_access(demo_identity(),'demo/atlas')
