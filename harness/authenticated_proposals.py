"""M3d3: dos credenciales locales del laboratorio; preparación y admisión para un worker de servicio."""
from uuid import UUID
from fastapi import Request
from .approval_web import ProposalRequest,DecisionRequest,EmptyRequest,DispatchRequest
from .adapters.mcp_jenkins_write import MCPJenkinsWrite
from .contracts import ContractError
from .core import authorize_run
from .fakes import ReadOnlyPolicy
from .operations import Operations
from .proposals import ProposalStore
from .write_queue import WriteQueue
from .resolutions import Resolutions


class AuthenticatedProposals:
    def __init__(self,runs,directory,provider_path):
        self.runs,self.directory,self.provider_path=runs,directory,provider_path
        self.proposals=ProposalStore(runs)

    def resolve(self,actor,workspace):
        try:return self.directory.resolve(actor,workspace)
        except ContractError as error:
            if error.code=='FORBIDDEN':raise
            raise ContractError('IDENTITY_UNAVAILABLE') from None
        except Exception:raise ContractError('IDENTITY_UNAVAILABLE') from None

    def operations(self,identity):
        return Operations(self.proposals,MCPJenkinsWrite(identity,self.provider_path),{identity.actor_id:identity},
            principal_resolver=self.resolve)

    def view(self,identifier,identity):
        proposal=self.proposals.get(str(identifier),identity)
        with self.runs._database() as db:
            operation=db.execute('''SELECT operation_id,status,version,outcome_code FROM operations
                WHERE proposal_id=%s''',(proposal['proposal_id'],)).fetchone()
            job=db.execute('''SELECT status,attempts,result_status,last_error FROM write_jobs WHERE operation_id=%s''',
                (operation['operation_id'],)).fetchone() if operation else None
            review=db.execute('SELECT * FROM operation_resolutions WHERE operation_id=%s',
                (operation['operation_id'],)).fetchone() if operation else None
        return {'proposal':proposal,'operation':dict(operation) if operation else None,
            'resolution':Resolutions._view(review,operation['version']) if review else None,
            'write_job':dict(job) if job else None,'dispatch_enabled':True}

    def inbox(self,identity):
        if not identity.roles & {'operator','approver'}:raise ContractError('FORBIDDEN')
        self.proposals._grant(identity)
        with self.runs._database() as db:
            rows=db.execute('''SELECT proposal_id FROM proposals WHERE workspace_id=%s AND (requested_by=%s OR %s)
                ORDER BY expires_at DESC,proposal_id DESC LIMIT 20''',
                (identity.workspace_id,identity.actor_id,'approver' in identity.roles)).fetchall()
        return {'items':[self.view(row['proposal_id'],identity) for row in rows]}

    def decide(self,identifier,identity,body):
        if self.resolve(identity.actor_id,identity.workspace_id)!=identity:raise ContractError('FORBIDDEN')
        if body.decision=='APPROVED':
            proposal=self.proposals.get(str(identifier),identity)
            owner=self.resolve(proposal['requested_by'],identity.workspace_id)
            if 'operator' not in owner.roles:raise ContractError('FORBIDDEN')
            self.proposals._grant(owner)
        self.proposals.decide(str(identifier),identity,**body.model_dump())
        return self.view(identifier,identity)


def install_routes(app):
    def service(request):return request.app.state.user_proposals

    @app.get('/api/proposals')
    def inbox(request:Request):return service(request).inbox(request.state.identity)

    @app.get('/api/runs/{run_id}/proposals')
    def for_run(run_id:UUID,request:Request):
        svc=service(request);identity=request.state.identity
        run=svc.runs.load(str(run_id),identity);authorize_run(run,identity,ReadOnlyPolicy())
        with svc.runs._database() as db:
            rows=db.execute('''SELECT proposal_id FROM proposals WHERE run_id=%s AND workspace_id=%s
                AND requested_by=%s ORDER BY expires_at DESC,proposal_id DESC LIMIT 20''',
                (str(run_id),identity.workspace_id,identity.actor_id)).fetchall()
        return {'items':[svc.view(row['proposal_id'],identity) for row in rows]}

    @app.post('/api/runs/{run_id}/proposals',status_code=201)
    def propose(run_id:UUID,body:ProposalRequest,request:Request):
        svc=service(request);identity=request.state.identity
        row=svc.proposals.create(str(run_id),identity,body.request_key)
        return svc.view(row['proposal_id'],identity)

    @app.get('/api/proposals/{proposal_id}')
    def get(proposal_id:UUID,request:Request):return service(request).view(proposal_id,request.state.identity)

    @app.post('/api/proposals/{proposal_id}/decision')
    def decide(proposal_id:UUID,body:DecisionRequest,request:Request):
        return service(request).decide(proposal_id,request.state.identity,body)

    @app.post('/api/proposals/{proposal_id}/operation')
    def prepare(proposal_id:UUID,body:EmptyRequest,request:Request):
        svc=service(request);identity=request.state.identity
        # Aunque un usuario tenga ambos roles, preparar solo se permite al dueño.
        proposal=svc.proposals.get(str(proposal_id),identity)
        if proposal['requested_by']!=identity.actor_id:raise ContractError('FORBIDDEN')
        svc.operations(identity).prepare(str(proposal_id),identity)
        return svc.view(proposal_id,identity)

    @app.get('/api/operations/{operation_id}')
    def operation(operation_id:UUID,request:Request):
        svc=service(request);ops=svc.operations(request.state.identity)
        return ops.get(str(operation_id),request.state.identity)|{'write_job':WriteQueue(ops).get(str(operation_id),request.state.identity)}

    @app.post('/api/operations/{operation_id}/dispatch',status_code=202)
    def dispatch(operation_id:UUID,body:DispatchRequest,request:Request):
        svc=service(request);identity=request.state.identity;ops=svc.operations(identity)
        with svc.runs._database() as db:
            op,proposal=ops._load(db,str(operation_id),identity)
            if op['status']=='PREPARED':ops._valid(db,proposal,identity)
        job=WriteQueue(ops).enqueue(str(operation_id),identity,interrupt=body.interrupt)
        return {'operation':ops.get(str(operation_id),identity),'write_job':job}
