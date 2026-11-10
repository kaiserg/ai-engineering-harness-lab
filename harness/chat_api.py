"""Chat autenticado; mensajes, runs y jobs se admiten en una sola transacción."""
from uuid import UUID
from typing import Literal
from fastapi import Request
from pydantic import BaseModel,ConfigDict,Field
from .chat import Chats
from .knowledge_api import service,Empty


class Create(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=False)
    request_key:UUID


class Conversation(Create):
    mode:Literal['documents','jira-demo','jira-cloud','github-demo','github-public']='documents'


class Send(Create):
    query:str=Field(min_length=1,max_length=2000,strict=True)
    pause_after_search:bool=Field(default=False,strict=True)


def install_routes(app):
    def chats(request):return Chats(service(request))

    @app.get('/api/chat/conversations')
    def recent(request:Request):return {'conversations':chats(request).recent()}

    @app.post('/api/chat/conversations',status_code=201)
    def create(body:Conversation,request:Request):return chats(request).create(body.request_key,body.mode)

    @app.get('/api/chat/conversations/{conversation_id}')
    def view(conversation_id:UUID,request:Request):return chats(request).view(str(conversation_id))

    @app.post('/api/chat/conversations/{conversation_id}/messages',status_code=202)
    def send(conversation_id:UUID,body:Send,request:Request):
        store=chats(request);identifier=store.send(str(conversation_id),body.request_key,body.query,body.pause_after_search)
        return {'run_id':identifier,'conversation_id':str(conversation_id)}

    @app.post('/api/chat/conversations/{conversation_id}/runs/{run_id}/resume',status_code=202)
    def resume(conversation_id:UUID,run_id:UUID,body:Empty,request:Request):
        store=chats(request);current=store.run(str(conversation_id),str(run_id))
        if current['status']!='COMPLETED':store.runs.requeue_job(str(run_id),store.identity())
        return store.run(str(conversation_id),str(run_id))
