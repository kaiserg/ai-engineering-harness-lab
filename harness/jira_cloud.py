"""Conexiones Jira Cloud locales: destinos fijados, GET limitado y errores saneados."""
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
from uuid import UUID,uuid5,NAMESPACE_URL
from hashlib import sha256
import httpx
from .contracts import ContractError,ToolResult,ToolCall,issue_arguments,pull_request_arguments
from .fakes import ReadOnlyPolicy
from .postgres_config import RUNTIME
from .telemetry import span

DIRECTORY=RUNTIME/'jira-cloud'
PREFIX='ollama-qwen3-4b-jira-cloud-chat-v1:'


def identifier(value):
    try:
        if type(value) is not str or str(UUID(value))!=value:raise ValueError()
        return value
    except (ValueError,TypeError):raise ContractError('JIRA_CONFIGURATION') from None


def site_origin(value):
    url=urlsplit(value)
    if (url.scheme!='https' or not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.atlassian\.net',url.hostname or '')
            or url.username or url.password or url.port not in (None,443)):
        raise ContractError('JIRA_CONFIGURATION')
    return 'https://'+url.hostname


def binding_for(config):
    public={key:config[key] for key in ('site','cloud_id','projects','actor','workspace')}
    return str(uuid5(NAMESPACE_URL,json.dumps(public,sort_keys=True)))


def load(binding):
    path=DIRECTORY/(identifier(binding)+'.json')
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(descriptor) as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError()
            data=json.load(handle)
        if (set(data)!={'site','cloud_id','email','token','projects','actor','workspace'}
                or site_origin(data['site'])!=data['site'] or type(data['email']) is not str or not data['email']
                or type(data['token']) is not str or not data['token']
                or not isinstance(data['projects'],list) or not data['projects']
                or any(not re.fullmatch(r'[A-Z][A-Z0-9]{1,9}',p) for p in data['projects'])):raise ValueError()
        if data['cloud_id'] is not None:identifier(data['cloud_id'])
        if binding_for(data)!=binding:raise ValueError()
        return data
    except (OSError,ValueError,TypeError,KeyError):raise ContractError('JIRA_CONFIGURATION') from None


def current():
    try:value=(DIRECTORY/'current').read_text().strip()
    except OSError:raise ContractError('JIRA_CONFIGURATION') from None
    load(value);return identifier(value)


def check_scope(config,identity,key):
    ReadOnlyPolicy().authorize(identity,ToolCall('jira_get_issue',{'issue_key':key}))
    if (identity.actor_id!=config['actor'] or identity.workspace_id!=config['workspace']
            or key.split('-')[0] not in config['projects']):raise ContractError('FORBIDDEN')


def fetch(binding,identity,key,*,access_only=False):
    issue_arguments({'issue_key':key});config=load(binding);check_scope(config,identity,key)
    base=('https://api.atlassian.com/ex/jira/'+config['cloud_id']) if config['cloud_id'] else config['site']
    fields='updated' if access_only else 'summary,status,description,updated,project'
    try:
        with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
            with span('jira.cloud.access' if access_only else 'jira.cloud.get'):
                with client.stream('GET',base+'/rest/api/3/issue/'+key,params={'fields':fields},
                        auth=httpx.BasicAuth(config['email'],config['token']),headers={'Accept':'application/json'}) as response:
                    if response.status_code in (401,403):raise ContractError('FORBIDDEN')
                    if response.status_code==404:raise ContractError('NOT_FOUND')
                    if response.status_code!=200:raise ContractError('UPSTREAM_UNAVAILABLE')
                    raw=bytearray()
                    for part in response.iter_bytes():
                        if len(raw)+len(part)>131072:raise ContractError('INVALID_TOOL_OUTPUT')
                        raw.extend(part)
        data=json.loads(raw)
    except (httpx.HTTPError,ValueError):raise ContractError('UPSTREAM_UNAVAILABLE') from None
    if data.get('key')!=key or type(data.get('fields')) is not dict:raise ContractError('INVALID_TOOL_OUTPUT')
    return data


def plain_adf(node, links=None):
    """Texto ADF acotado; no ejecuta enlaces, adjuntos, HTML ni menciones."""
    output=[];stack=[(node,0)];count=0
    while stack:
        value,depth=stack.pop();count+=1
        if count>2000 or depth>30:raise ContractError('INVALID_TOOL_OUTPUT')
        if not isinstance(value,dict):continue
        if value.get('type')=='text' and type(value.get('text')) is str:
            output.append(value['text'])
            if links is not None:
                marks=value.get('marks',[])
                if not isinstance(marks,list):raise ContractError('INVALID_TOOL_OUTPUT')
                anchors=[mark for mark in marks if isinstance(mark,dict) and mark.get('type')=='link']
                if anchors:
                    for anchor in anchors:
                        attrs=anchor.get('attrs')
                        if isinstance(attrs,dict):links.append(attrs.get('href'))
                else:
                    links.extend(re.findall(r'https://[^\s<>\[\]"\']+',value['text']))
        if links is not None and value.get('type') in ('inlineCard','blockCard'):
            attrs=value.get('attrs')
            if isinstance(attrs,dict):links.append(attrs.get('url'))
        children=value.get('content',[])
        if not isinstance(children,list):raise ContractError('INVALID_TOOL_OUTPUT')
        stack.extend((child,depth+1) for child in reversed(children))
    return ' '.join(output)[:2400]


def github_pull_links(links):
    found = {}
    for url in links:
        if not isinstance(url,str) or len(url)>300:continue
        match=re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]{0,6})/?',url.rstrip('.,;!)'))
        if not match:continue
        repository,number=match[1].lower(),int(match[2])
        try:pull=pull_request_arguments({'repository':repository,'pull_number':number})
        except ContractError:continue
        found[repository,number]=pull
        if len(found)>16:raise ContractError('INVALID_TOOL_OUTPUT')
    return list(found.values())


class JiraCloud:
    def __init__(self,include_pr=False):
        self.include_pr=include_pr

    async def invoke(self,identity,call):
        import asyncio
        binding=identifier(os.environ['HARNESS_JIRA_BINDING'])
        data=await asyncio.to_thread(fetch,binding,identity,call.arguments['issue_key'])
        fields=data['fields'];key=data['key']
        try:
            if fields['project']['key']!=key.split('-')[0]:raise ValueError()
            summary,status,updated=fields['summary'],fields['status']['name'],fields['updated']
            if any(type(x) is not str or not x for x in (summary,status,updated)):raise ValueError()
        except (KeyError,TypeError,ValueError):raise ContractError('INVALID_TOOL_OUTPUT') from None
        links=[] if self.include_pr else None
        payload={'issue_key':key,'status':status[:100],'summary':summary[:200],
                 'description':plain_adf(fields.get('description'),links),'updated':updated[:64]}
        if self.include_pr:payload['pull_requests']=github_pull_links(links)
        source='jira-cloud:'+binding+':'+key+':'+sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:12]
        return ToolResult(call.call_id,payload,source,tool_name='jira_get_issue')
