"""Lecturas de la consola común; las acciones reutilizan las fronteras existentes."""
from dataclasses import asdict
from fastapi import Request
from .contracts import ContractError
from .core import Limits
from .knowledge import CONFIG
from .rag_agent import PROFILE
from .rag_api import RAG_LIMITS
from .telemetry import carrier


def principal(request,*,read=False):
    identity=request.app.state.user_directory.authenticate(request.headers.get('authorization'),request.headers.get('x-harness-workspace'))
    allowed={'reader','operator'} if read else {'reader','operator','approver'}
    if not identity.roles & allowed:raise ContractError('FORBIDDEN')
    return identity


def install_routes(app,scenarios):
    @app.get('/api/console/runs')
    def runs(request:Request):
        identity=principal(request,read=True)
        with request.app.state.run_store._database() as db:
            rows=db.execute('''SELECT r.run_id,r.created_at,r.payload->>'status' AS status,
                r.payload->>'execution_profile' AS profile,(r.payload->>'model_calls')::integer AS model_calls,
                (r.payload->>'tool_calls')::integer AS tool_calls,j.status AS job_status,
                COALESCE(j.lease_until>clock_timestamp(),false) AS lease_active,j.previous_traceparent
                FROM runs r LEFT JOIN jobs j USING(run_id)
                WHERE r.workspace_id=%s AND r.requested_by=%s ORDER BY r.created_at DESC,r.run_id LIMIT 30''',
                (identity.workspace_id,identity.actor_id)).fetchall()
        for row in rows:
            saved=row.pop('previous_traceparent')
            parent=carrier(saved) if saved else None
            row['trace_id']=parent.split('-')[1] if parent else None
            row['kind']='rag' if row['profile']==PROFILE else 'ci'
        return {'runs':rows}

    @app.get('/api/console/config')
    def config(request:Request):
        identity=principal(request)
        return {'workspace':identity.workspace_id,'identity_mode':'local-user-credential',
                'model':'scripted','embeddings':CONFIG,'limits':{'ci':asdict(Limits()),'rag':asdict(RAG_LIMITS)},
                'ingestion':{'max_file_bytes':5*1024*1024,'max_documents':20,'max_chunks':1000,
                    'max_attempts':3,'deadline_seconds':300},
                'scenarios':[{'id':key,'title':value[0]} for key,value in scenarios.items()],
                'monitoring':'shared-local-lab','editable':False}
