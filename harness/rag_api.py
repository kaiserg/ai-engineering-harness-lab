"""API opt-in del perfil CI+RAG: la admisión no ejecuta herramientas."""
from dataclasses import asdict
from uuid import UUID
from fastapi import Request
from .contracts import ContractError,Run
from .core import Limits,authorize_run
from .rag_agent import PROFILE,KnowledgePolicy
from .knowledge_api import Empty,service

RAG_LIMITS=Limits(model_calls=5)


def view(request,identifier):
    knowledge=service(request);identity=knowledge.identity()
    run,job=knowledge.runs.snapshot(str(identifier),identity)
    if run.execution_profile!=PROFILE:raise ContractError('NOT_FOUND')
    authorize_run(run,identity,KnowledgePolicy(knowledge))
    return {**asdict(run),'job':job}


def install_routes(app):
    @app.post('/api/knowledge/agent/runs',status_code=202)
    def start(body:Empty,request:Request):
        knowledge=service(request);identity=knowledge.identity()
        run=knowledge.runs.enqueue(Run(identity.workspace_id,identity.actor_id,execution_profile=PROFILE,
            limits_snapshot=asdict(RAG_LIMITS)),identity,max_pending=16)
        return view(request,run.run_id)

    @app.get('/api/knowledge/agent/runs/{run_id}')
    def get(run_id:UUID,request:Request):return view(request,run_id)

    @app.post('/api/knowledge/agent/runs/{run_id}/resume',status_code=202)
    def resume(run_id:UUID,body:Empty,request:Request):
        current=view(request,run_id)
        if current['status']=='COMPLETED':return current
        knowledge=service(request)
        knowledge.runs.requeue_job(str(run_id),knowledge.identity())
        return view(request,run_id)
