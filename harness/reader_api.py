"""Admisión del lector LLM; el worker conserva ejecución, permisos y recuperación."""
from dataclasses import asdict
from uuid import UUID
from fastapi import Request
from pydantic import BaseModel,ConfigDict,Field
from .contracts import ContractError,Run
from .core import authorize_run
from .knowledge_api import service,Empty
from .llm_reader import PROFILE,LIMITS
from .rag_agent import KnowledgePolicy


class ReadingRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    query:str=Field(min_length=1,max_length=2000)
    pause_after_search:bool=False


def view(request,identifier):
    knowledge=service(request);identity=knowledge.identity()
    run,job=knowledge.runs.snapshot(str(identifier),identity)
    if run.execution_profile!=PROFILE:raise ContractError('NOT_FOUND')
    authorize_run(run,identity,KnowledgePolicy(knowledge))
    return {**asdict(run),'job':job}


def install_routes(app):
    @app.post('/api/knowledge/reader/runs',status_code=202)
    def start(body:ReadingRequest,request:Request):
        if not body.query.strip():raise ContractError('INVALID_QUERY')
        knowledge=service(request);identity=knowledge.identity()
        run=knowledge.runs.enqueue(Run(identity.workspace_id,identity.actor_id,execution_profile=PROFILE,
            read_request={'query':body.query},limits_snapshot=asdict(LIMITS)),identity,
            pause_at='jira' if body.pause_after_search else 'none',max_pending=16)
        return view(request,run.run_id)

    @app.get('/api/knowledge/reader/runs/{run_id}')
    def get(run_id:UUID,request:Request):return view(request,run_id)

    @app.post('/api/knowledge/reader/runs/{run_id}/resume',status_code=202)
    def resume(run_id:UUID,body:Empty,request:Request):
        current=view(request,run_id)
        if current['status']=='COMPLETED':return current
        knowledge=service(request)
        knowledge.runs.requeue_job(str(run_id),knowledge.identity())
        return view(request,run_id)
