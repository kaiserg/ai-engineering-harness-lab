"""Frontera HTTP de conocimiento: admisión/consulta; nunca ejecuta la ingestión."""
import asyncio
from uuid import UUID
from typing import Literal
from fastapi import Request
from pydantic import BaseModel,ConfigDict,Field,ValidationError
from .contracts import ContractError
from .knowledge import Knowledge,MAX_BYTES
from .knowledge_queue import KnowledgeQueue
from .embeddings import PROFILES

BODY_LIMIT=6*1024*1024


class Upload(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_key:UUID
    filename:str=Field(min_length=1,max_length=124)
    text:str=Field(min_length=1,max_length=MAX_BYTES)
    readers:list[str]=Field(default_factory=list,max_length=20)


class Enqueue(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_key:UUID
    embedding_profile:Literal['lexical','semantic']='lexical'


class Search(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    query:str=Field(min_length=1,max_length=2000)
    limit:int=Field(default=3,ge=1,le=5)


class Empty(BaseModel):
    model_config=ConfigDict(extra='forbid')


def service(request):
    directory=request.app.state.user_directory
    auth=request.headers.get('authorization');workspace=request.headers.get('x-harness-workspace')
    authenticate=lambda:directory.authenticate(auth,workspace)
    identity=authenticate()
    if not identity.roles & {'reader','operator'}:raise ContractError('FORBIDDEN')
    return Knowledge(request.app.state.run_store,authenticate,getattr(request.app.state,'knowledge_vectors',None))


def install_routes(app):
    @app.get('/api/knowledge/documents')
    def documents(request:Request):return {'documents':service(request).documents()}

    @app.post('/api/knowledge/documents',status_code=201)
    async def upload(request:Request):
        # Limita el stream antes de parsear JSON; Content-Length no es autoridad.
        raw=bytearray()
        async for chunk in request.stream():
            if len(raw)+len(chunk)>BODY_LIMIT:raise ContractError('PAYLOAD_TOO_LARGE')
            raw.extend(chunk)
        try:body=Upload.model_validate_json(raw)
        except ValidationError:raise ContractError('INVALID_DOCUMENT') from None
        def register():
            return service(request).register(body.filename,body.text,body.readers,request_key=str(body.request_key))
        return await asyncio.to_thread(register)

    @app.post('/api/knowledge/documents/{document_id}/retire')
    def retire(document_id:UUID,body:Empty,request:Request):return service(request).retire(str(document_id))

    @app.get('/api/knowledge/jobs')
    def jobs(request:Request):return {'jobs':KnowledgeQueue(service(request)).recent()}

    @app.post('/api/knowledge/jobs',status_code=202)
    def enqueue(body:Enqueue,request:Request):return KnowledgeQueue(service(request)).enqueue(str(body.request_key),configuration=PROFILES[body.embedding_profile])

    @app.get('/api/knowledge/jobs/{job_id}')
    def job(job_id:UUID,request:Request):return KnowledgeQueue(service(request)).get(str(job_id))

    @app.post('/api/knowledge/search')
    def search(body:Search,request:Request):return service(request).search(body.query,body.limit)
