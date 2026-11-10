"""Cierre revisado por dos usuarios; capacidad de servicio sin reenviar UNKNOWN."""
from uuid import UUID
from fastapi import Request
from .approval_web import EmptyRequest,ResolutionConfirmRequest
from .contracts import ContractError,Identity
from .operations import Operations
from .proposals import ProposalStore,uuid_text
from .resolutions import Resolutions
from .user_write_worker import ServiceWriteOperations,ServiceWriteProvider


class ClosureProvider(ServiceWriteProvider):
    def request(self,*args):raise ContractError('FORBIDDEN')
    def lookup(self,*args):raise ContractError('FORBIDDEN')


class ManualResolutionOperations(ServiceWriteOperations):
    def dispatch(self,*args,**kwargs):raise ContractError('FORBIDDEN')
    def reconcile(self,*args,**kwargs):raise ContractError('FORBIDDEN')

    @staticmethod
    def _event(db,row,actor):Operations._event(db,row,actor)


class AuthenticatedResolutions:
    def __init__(self,proposals):self.service=proposals;self.runs=proposals.runs

    def context(self,identifier,identity,*,action='read'):
        # Encontrar el dueño no concede acceso: primero aplicar la vista autorizada de la propuesta.
        with self.runs._database() as db:
            row=db.execute('''SELECT o.proposal_id FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
                WHERE o.operation_id=%s AND p.workspace_id=%s''',(uuid_text(identifier),identity.workspace_id)).fetchone()
        if row is None:raise ContractError('NOT_FOUND')
        proposal=self.service.proposals.get(row['proposal_id'],identity)
        owner=proposal['requested_by']
        role='operator' if action=='prepare' else 'approver' if action=='confirm' else None
        def authorize():
            current=self.service.resolve(identity.actor_id,identity.workspace_id)
            if current!=identity:raise ContractError('FORBIDDEN')
            ProposalStore._grant(current)
            if role and role not in current.roles:raise ContractError('FORBIDDEN')
            if action=='prepare' and current.actor_id!=owner:raise ContractError('FORBIDDEN')
            if action=='confirm' and current.actor_id==owner:raise ContractError('FORBIDDEN')
        authorize()
        scope=Identity(owner,identity.workspace_id,frozenset(),frozenset())
        def guard(db):
            # Mismo orden de locks que el worker: job antes de operación. Nunca tomar un lease activo.
            job=db.execute('SELECT status FROM write_jobs WHERE operation_id=%s FOR UPDATE',(identifier,)).fetchone()
            if job is None or job['status'] not in ('NEEDS_REVIEW','DONE'):raise ContractError('OPERATION_BUSY')
        provider=ClosureProvider(scope,self.service.provider_path,closure_authorizer=authorize if role else None)
        ops=ManualResolutionOperations(self.service.proposals,provider,scope,self.service.directory,guard=guard)
        def finish(db,op,status):
            row=db.execute('''UPDATE write_jobs SET status='DONE',result_status=%s,last_error='MANUAL_PROVIDER_CLOSURE',
                lease_until=NULL,fencing_token=fencing_token+1,updated_at=clock_timestamp()
                WHERE operation_id=%s AND status='NEEDS_REVIEW' RETURNING job_id''',(status,op['operation_id'])).fetchone()
            if row is None:raise ContractError('CONFLICT')
        return Resolutions(ops,reviewer_resolver=self.service.resolve,on_confirm=finish),scope


def install_routes(app):
    def service(request):return AuthenticatedResolutions(request.app.state.user_proposals)

    @app.get('/api/operations/{operation_id}/resolution')
    def get(operation_id:UUID,request:Request):
        resolutions,scope=service(request).context(str(operation_id),request.state.identity)
        return {'resolution':resolutions.get(str(operation_id),scope)}

    @app.post('/api/operations/{operation_id}/resolution')
    def prepare(operation_id:UUID,body:EmptyRequest,request:Request):
        resolutions,scope=service(request).context(str(operation_id),request.state.identity,action='prepare')
        with resolutions.runs._database() as db:
            op,_=resolutions.ops._load(db,str(operation_id),scope)
            if op['status']=='DISPATCHING':
                now=db.execute('SELECT clock_timestamp() AS now').fetchone()['now']
                if op['lease_until']>now:raise ContractError('OPERATION_BUSY')
                # Reclamos agotados pueden dejar DISPATCHING aparcado: investigar sin reenviar.
                resolutions.ops._transition(db,op,'UNKNOWN',scope,code='UNKNOWN_EFFECT')
        return {'resolution':resolutions.prepare(str(operation_id),scope)}

    @app.post('/api/operations/{operation_id}/resolution/confirm')
    def confirm(operation_id:UUID,body:ResolutionConfirmRequest,request:Request):
        resolutions,scope=service(request).context(str(operation_id),request.state.identity,action='confirm')
        return {'resolution':resolutions.confirm(str(operation_id),scope,request.state.identity,**body.model_dump())}
