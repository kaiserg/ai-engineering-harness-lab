"""Perfil didáctico: CI + recuperación textual; el modelo guionado no concede autoridad."""
import asyncio
import math
from copy import deepcopy
import re
from .contracts import ContractError,ToolCall,ToolResult,FinalAnswer,knowledge_arguments,result_call
from .fakes import ReadOnlyPolicy,CIScriptedLLM
from .knowledge import digest
from .evidence import finalize_ci

PROFILE='mcp-devops-rag-v1'
QUERY='responsable release Atlas'


def source(data):return 'knowledge:'+str(data['index_id'])+':'+digest(data['query'])[:16]


def validate_result(result):
    data=result.data
    if (result.tool_name!='knowledge_search' or result.schema_version!=1 or type(data) is not dict
            or set(data)!={'query','limit','index_id','matches'}):raise ContractError('INVALID_TOOL_OUTPUT')
    knowledge_arguments({'query':data['query'],'limit':data['limit']})
    index=data['index_id']
    if index is not None and (type(index) is not str or not re.fullmatch('[0-9a-f]{32}',index)):
        raise ContractError('INVALID_TOOL_OUTPUT')
    if type(data['matches']) is not list or len(data['matches'])>data['limit'] or index is None and data['matches']:
        raise ContractError('INVALID_TOOL_OUTPUT')
    seen=set()
    for match in data['matches']:
        if (type(match) is not dict or set(match)!={'citation','filename','text','distance','trust'}
                or type(match['distance']) not in (int,float) or not math.isfinite(match['distance'])
                or match['trust']!='untrusted_document' or type(match['text']) is not str
                or not 1<=len(match['text'])<=800 or type(match['filename']) is not str
                or not 1<=len(match['filename'])<=124 or type(match['citation']) is not str
                or not re.fullmatch(r'\['+str(index)+r':[0-9a-f-]{36}:[0-9]{1,4}\]',match['citation'])
                or match['citation'] in seen):raise ContractError('INVALID_TOOL_OUTPUT')
        seen.add(match['citation'])
    if result.source_id!=source(data):raise ContractError('INVALID_TOOL_OUTPUT')
    return data


class KnowledgePolicy(ReadOnlyPolicy):
    def __init__(self,knowledge):self.knowledge=knowledge
    def visible_tools(self,identity):
        previous=super().visible_tools(identity)
        return previous+(('knowledge_search',) if identity.roles & {'reader','operator'} else ())
    def authorize(self,identity,call):
        current=self.knowledge.identity()
        if current!=identity:raise ContractError('FORBIDDEN')
        if call.name!='knowledge_search':return super().authorize(identity,call)
        knowledge_arguments(call.arguments)
    def authorize_result(self,identity,result):
        if result.tool_name!='knowledge_search':
            self.authorize(identity,result_call(result));return
        data=validate_result(result)
        self.authorize(identity,ToolCall(result.tool_name,{'query':data['query'],'limit':data['limit']}))
        with self.knowledge.runs._database() as db:
            for match in data['matches']:
                chunk_id=match['citation'][34:-1]  # '[' + 32 hex + ':'; chunk_id incluye doc UUID y ordinal.
                row=db.execute("""SELECT c.content,c.content_hash,d.filename FROM knowledge_chunks c
                    JOIN knowledge_documents d USING(document_id) JOIN knowledge_indexes k USING(index_id)
                    WHERE c.index_id=%s AND c.chunk_id=%s AND k.state='READY' AND k.workspace_id=%s
                    AND d.workspace_id=%s AND d.state='ACTIVE' AND (d.owner=%s OR %s=ANY(d.readers))""",
                    (data['index_id'],chunk_id,identity.workspace_id,identity.workspace_id,identity.actor_id,identity.actor_id)).fetchone()
                if not row:raise ContractError('EVIDENCE_REVOKED')
                if (row['content']!=match['text'] or row['filename']!=match['filename']
                        or digest(row['content'])!=row['content_hash']):raise ContractError('INVALID_TOOL_OUTPUT')


class KnowledgeTool:
    def __init__(self,knowledge):self.knowledge=knowledge;self.invocations=0
    async def invoke(self,identity,call):
        KnowledgePolicy(self.knowledge).authorize(identity,call)
        arguments=knowledge_arguments(call.arguments);self.invocations+=1
        found=await self.knowledge.asearch(**arguments)
        data={**arguments,**found}
        result=ToolResult(call.call_id,data,source(data),tool_name='knowledge_search')
        await asyncio.to_thread(KnowledgePolicy(self.knowledge).authorize_result,identity,result)
        return result


class RAGScriptedLLM(CIScriptedLLM):
    """Elige la búsqueda para el caso didáctico; no ejecuta instrucciones del documento."""
    async def decide(self,run,allowed_tools):
        if len(run.results)<3:return await super().decide(run,allowed_tools)
        if len(run.results)==3:return ToolCall('knowledge_search',{'query':QUERY,'limit':3})
        return FinalAnswer('Cerrar con evidencia CI y contexto documental citado.')


def finalize_rag(run):
    if [r.tool_name for r in run.results]!=['jira_get_issue','github_get_pull_request','jenkins_get_build','knowledge_search']:
        raise ContractError('INCOMPLETE_EVIDENCE')
    data=validate_result(run.results[-1])
    ci=deepcopy(run);ci.results=ci.results[:3]
    answer,assessment=finalize_ci(ci)
    context='\nContexto documental recuperado (contenido no confiable):'
    if not data['matches']:context+=' Sin fragmentos accesibles; no hay evidencia documental para responder.'
    for match in data['matches']:
        context+='\n'+match['filename']+': '+repr(match['text'][:240])+' '+match['citation']
    assessment={**assessment,'knowledge_status':'RETRIEVED' if data['matches'] else 'NO_MATCHES',
                'knowledge_citations':[m['citation'] for m in data['matches']]}
    return FinalAnswer(answer.text+context),assessment
