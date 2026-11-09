"""Rutas M4 opt-in de laboratorio. Identidades fijas, no autenticación de usuarios."""
from typing import Literal
from uuid import UUID
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field

from .adapters.mcp_jenkins_write import MCPJenkinsWrite
from .core import authorize_run
from .development import fixture_identity
from .fakes import ReadOnlyPolicy
from .operations import Operations,OperationInterrupted
from .proposals import ProposalStore


class ProposalRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_key:str=Field(pattern=r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


class DecisionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    payload_hash:str=Field(pattern=r'^[0-9a-f]{64}$')
    expected_version:int=Field(ge=0)
    decision:Literal['APPROVED','REJECTED']


class EmptyRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class DispatchRequest(EmptyRequest):
    interrupt:Literal['after-send','before-send']|None=None


class ResolutionConfirmRequest(EmptyRequest):
    payload_hash:str=Field(pattern=r'^[0-9a-f]{64}$')
    expected_version:int=Field(ge=0)


class ApprovalDemo:
    def __init__(self,runs,provider_path,*,write_worker=False):
        self.operator=fixture_identity('operator');self.approver=fixture_identity('approver')
        self.proposals=ProposalStore(runs)
        self.operations=Operations(self.proposals,MCPJenkinsWrite(self.operator,provider_path),
            {self.operator.actor_id:self.operator,self.approver.actor_id:self.approver})
        from .resolutions import Resolutions
        self.resolutions=Resolutions(self.operations)
        from .write_queue import WriteQueue
        self.write_queue=WriteQueue(self.operations)
        self.write_worker=write_worker
        from .write_observability import start
        start(9103,lambda:self.write_queue.snapshot(self.operator))
        self.runs=runs

    def view(self,identifier):
        proposal=self.proposals.get(str(identifier),self.operator)
        with self.runs._database() as db:
            row=db.execute('SELECT operation_id FROM operations WHERE proposal_id=%s',(proposal['proposal_id'],)).fetchone()
        return {'proposal':proposal,'operation':self.operations.get(row['operation_id'],self.operator) if row else None,
            'resolution':self.resolutions.get(row['operation_id'],self.operator) if row else None,
            'write_job':self.write_queue.get(row['operation_id'],self.operator) if row and self.write_worker else None}

    def list_for_run(self,identifier):
        run=self.runs.load(str(identifier),self.operator)
        authorize_run(run,self.operator,ReadOnlyPolicy())
        with self.runs._database() as db:
            rows=db.execute('''SELECT proposal_id FROM proposals WHERE run_id=%s AND workspace_id=%s
                AND requested_by=%s ORDER BY expires_at DESC,proposal_id DESC LIMIT 20''',
                (run.run_id,self.operator.workspace_id,self.operator.actor_id)).fetchall()
        return {'items':[self.view(row['proposal_id']) for row in rows]}


def install_routes(app):
    def service(request):return request.app.state.approval_demo

    @app.get('/api/runs/{run_id}/proposals')
    def proposals(run_id:UUID,request:Request):
        return service(request).list_for_run(run_id)

    @app.post('/api/runs/{run_id}/proposals',status_code=201)
    def propose(run_id:UUID,body:ProposalRequest,request:Request):
        demo=service(request)
        row=demo.proposals.create(str(run_id),demo.operator,body.request_key)
        return demo.view(row['proposal_id'])

    @app.get('/api/proposals/{proposal_id}')
    def proposal(proposal_id:UUID,request:Request):return service(request).view(proposal_id)

    @app.post('/api/proposals/{proposal_id}/decision')
    def decide(proposal_id:UUID,body:DecisionRequest,request:Request):
        demo=service(request)
        demo.view(proposal_id)  # El modo local solo opera propuestas visibles al operador fijo.
        demo.proposals.decide(str(proposal_id),demo.approver,**body.model_dump())
        return demo.view(proposal_id)

    @app.post('/api/proposals/{proposal_id}/operation')
    def prepare(proposal_id:UUID,body:EmptyRequest,request:Request):
        demo=service(request)
        demo.operations.prepare(str(proposal_id),demo.operator)
        return demo.view(proposal_id)

    @app.get('/api/operations/{operation_id}')
    def operation(operation_id:UUID,request:Request):
        demo=service(request)
        return demo.operations.get(str(operation_id),demo.operator)

    @app.post('/api/operations/{operation_id}/dispatch')
    def dispatch(operation_id:UUID,body:DispatchRequest,request:Request):
        demo=service(request)
        if demo.write_worker:
            job=demo.write_queue.enqueue(str(operation_id),demo.operator,interrupt=body.interrupt)
            return JSONResponse({'operation':demo.operations.get(str(operation_id),demo.operator),'write_job':job},status_code=202)
        try:return demo.operations.dispatch(str(operation_id),demo.operator,interrupt=body.interrupt)
        except OperationInterrupted:
            # Punto de fallo de laboratorio: omitir confirmación del ledger, mantener API disponible.
            return demo.operations.get(str(operation_id),demo.operator)

    @app.post('/api/operations/{operation_id}/reconcile')
    def reconcile(operation_id:UUID,body:EmptyRequest,request:Request):
        demo=service(request)
        return demo.operations.reconcile(str(operation_id),demo.operator)

    @app.get('/api/operations/{operation_id}/resolution')
    def resolution(operation_id:UUID,request:Request):
        demo=service(request)
        return demo.resolutions.get(str(operation_id),demo.operator)

    @app.post('/api/operations/{operation_id}/resolution')
    def prepare_resolution(operation_id:UUID,body:EmptyRequest,request:Request):
        demo=service(request)
        return demo.resolutions.prepare(str(operation_id),demo.operator)

    @app.post('/api/operations/{operation_id}/resolution/confirm')
    def confirm_resolution(operation_id:UUID,body:ResolutionConfirmRequest,request:Request):
        demo=service(request)
        return demo.resolutions.confirm(str(operation_id),demo.operator,demo.approver,**body.model_dump())
