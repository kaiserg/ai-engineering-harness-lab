"""Lectura GitHub pública: GET fijo y anónimo, sin credenciales ni redirects."""
import asyncio
import json
import re
from hashlib import sha256
import httpx
from .contracts import ContractError,ToolCall,ToolResult,pull_request_arguments
from .fakes import ReadOnlyPolicy
from .telemetry import span

FIELDS={'repository','pull_number','state','head_revision','title','description','merged','draft','updated'}


def source(data):return 'github-public:'+sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:32]


def request(path,*,metadata=False):
    try:
        with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
            with span('github.public.access' if metadata else 'github.public.get'):
                with client.stream('GET','https://api.github.com'+path,headers={
                    'Accept':'application/vnd.github.text+json','X-GitHub-Api-Version':'2026-03-10',
                    'User-Agent':'ai-engineering-harness-lab'}) as response:
                    if response.status_code==429 or response.status_code==403 and (response.headers.get('x-ratelimit-remaining')=='0' or 'retry-after' in response.headers):
                        raise ContractError('PROVIDER_RATE_LIMITED')
                    if response.status_code in (401,403):raise ContractError('FORBIDDEN')
                    if response.status_code==404:raise ContractError('NOT_FOUND')
                    if response.status_code!=200:raise ContractError('UPSTREAM_UNAVAILABLE')
                    raw=bytearray()
                    for part in response.iter_bytes():
                        if len(raw)+len(part)>262144:raise ContractError('INVALID_TOOL_OUTPUT')
                        raw.extend(part)
        data=json.loads(raw)
    except (httpx.HTTPError,ValueError):raise ContractError('UPSTREAM_UNAVAILABLE') from None
    if type(data) is not dict:raise ContractError('INVALID_TOOL_OUTPUT')
    return data


def public_access(identity,repository):
    arguments=pull_request_arguments({'repository':repository,'pull_number':1})
    ReadOnlyPolicy().authorize(identity,ToolCall('github_get_pull_request',arguments))
    data=request('/repos/'+repository,metadata=True)
    if data.get('private') is not False or str(data.get('full_name','')).lower()!=repository.lower():raise ContractError('FORBIDDEN')


def validate_data(data):
    if type(data) is not dict or set(data)!=FIELDS:raise ContractError('INVALID_TOOL_OUTPUT')
    pull_request_arguments({key:data[key] for key in ('repository','pull_number')})
    if (data['state'] not in ('OPEN','CLOSED') or type(data['head_revision']) is not str
            or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',data['head_revision'])
            or type(data['merged']) is not bool or type(data['draft']) is not bool):raise ContractError('INVALID_TOOL_OUTPUT')
    for field,limit in [('title',200),('description',2400),('updated',64)]:
        if type(data[field]) is not str or len(data[field])>limit:raise ContractError('INVALID_TOOL_OUTPUT')
    return data


def fetch(identity,arguments):
    arguments=pull_request_arguments(arguments);ReadOnlyPolicy().authorize(identity,ToolCall('github_get_pull_request',arguments))
    repository,number=arguments['repository'],arguments['pull_number']
    data=request('/repos/'+repository+'/pulls/'+str(number))
    try:
        if (type(data['number']) is not int or data['number']!=number or data['base']['repo']['private'] is not False
                or data['base']['repo']['full_name'].lower()!=repository.lower()):raise ValueError()
        normalized={'repository':repository,'pull_number':number,'state':data['state'].upper(),
            'head_revision':data['head']['sha'],'title':data['title'][:200],
            'description':(data.get('body_text') or data.get('body') or '')[:2400],
            'merged':data['merged'],'draft':data['draft'],'updated':data['updated_at']}
    except (KeyError,TypeError,ValueError,AttributeError):raise ContractError('INVALID_TOOL_OUTPUT') from None
    return validate_data(normalized)


class PublicGitHub:
    async def invoke(self,identity,call):
        data=await asyncio.to_thread(fetch,identity,call.arguments)
        return ToolResult(call.call_id,data,source(data),tool_name='github_get_pull_request')
