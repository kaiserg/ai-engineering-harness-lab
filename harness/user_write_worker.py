"""M3d4: servicio acotado por dueño/workspace; envío autorizado, recuperación independiente de sesión."""
import argparse
import asyncio
import json
from pathlib import Path
import signal
from .adapters.mcp_jenkins_write import MCPJenkinsWrite
from .contracts import ContractError,Identity
from .operations import Operations
from .proposals import ProposalStore
from .user_identity import name
from .write_worker import WriteWorker


def resolve(directory,actor,workspace):
    try:return directory.resolve(actor,workspace)
    except ContractError as error:
        if error.code=='FORBIDDEN':raise
        raise ContractError('IDENTITY_UNAVAILABLE') from None
    except Exception:raise ContractError('IDENTITY_UNAVAILABLE') from None


class ServiceWriteProvider:
    """El sobre del simulador representa el scope del servicio, no una sesión humana."""
    def __init__(self,scope,path,*,closure_authorizer=None):
        self.scope=scope;self.closure_authorizer=closure_authorizer
        subject=Identity(scope.actor_id,scope.workspace_id,frozenset({'operator'}),
            frozenset({'DEV'}),frozenset({'demo/atlas'}),frozenset({'atlas-tests'}))
        self.client=MCPJenkinsWrite(subject,path)

    def _scope(self,identity):
        if identity!=self.scope:raise ContractError('FORBIDDEN')

    def request(self,identity,*args):
        self._scope(identity)
        return self.client.request(self.client.identity,*args)

    def lookup(self,identity,*args):
        self._scope(identity)
        return self.client.lookup(self.client.identity,*args)

    def close_request(self,identity,*args):
        self._scope(identity)
        if self.closure_authorizer is None:raise ContractError('FORBIDDEN')
        self.closure_authorizer()
        proof=self.client.close_request(self.client.identity,*args)
        self.closure_authorizer()
        return proof


class ServiceWriteOperations(Operations):
    service_execution=True

    def __init__(self,proposals,provider,scope,directory,*,guard=None):
        super().__init__(proposals,provider,{},guard=guard)
        self.scope,self.directory=scope,directory

    def with_guard(self,guard):
        return ServiceWriteOperations(self.proposals,self.provider,self.scope,self.directory,guard=guard)

    def _operator(self,identity):
        # Capacidad de contabilidad de servicio: no concede permiso para request.
        if identity!=self.scope:raise ContractError('FORBIDDEN')

    def _load(self,db,identifier,identity):
        from .proposals import uuid_text
        self._check(db);self._operator(identity)
        row=db.execute('''SELECT o.* FROM operations o JOIN proposals p ON p.proposal_id=o.proposal_id
            WHERE o.operation_id=%s AND p.workspace_id=%s AND p.requested_by=%s FOR UPDATE OF o''',
            (uuid_text(identifier),self.scope.workspace_id,self.scope.actor_id)).fetchone()
        if row is None:raise ContractError('NOT_FOUND')
        proposal=db.execute('SELECT * FROM proposals WHERE proposal_id=%s FOR UPDATE',(row['proposal_id'],)).fetchone()
        self.proposals._restore(proposal)
        return row,proposal

    def _valid(self,db,proposal,identity):
        self._operator(identity)
        current=resolve(self.directory,self.scope.actor_id,self.scope.workspace_id)
        if (current.actor_id,current.workspace_id)!=(self.scope.actor_id,self.scope.workspace_id):raise ContractError('FORBIDDEN')
        # Reutilizar la validación completa, con el usuario actual y el aprobador real.
        authority=Operations(self.proposals,self.provider,{current.actor_id:current},
            principal_resolver=lambda actor,workspace:resolve(self.directory,actor,workspace))
        return authority._valid(db,proposal,current)

    def before_send(self,identifier,identity,version=None):
        with self.runs._database() as db:
            row,proposal=self._load(db,identifier,identity)
            if row['status']!='DISPATCHING' or row['version']!=version:raise ContractError('CONFLICT')
            self._valid(db,proposal,identity)

    def prepare(self,*args,**kwargs):raise ContractError('FORBIDDEN')

    def dispatch(self,identifier,identity,**kwargs):
        if self.guard is None:raise ContractError('LEASE_LOST')
        return super().dispatch(identifier,identity,**kwargs)

    def reconcile(self,identifier,identity):
        if self.guard is None:raise ContractError('LEASE_LOST')
        return super().reconcile(identifier,identity)

    @staticmethod
    def _event(db,row,actor):
        Operations._event(db,row,'service:write-worker')


def create_worker(runs,directory,actor,workspace,provider_path,**kwargs):
    name(actor);name(workspace)
    if runs.schema=='harness_lab':raise ValueError('El servicio de usuarios requiere un schema separado.')
    scope=Identity(actor,workspace,frozenset(),frozenset())
    operations=ServiceWriteOperations(ProposalStore(runs),ServiceWriteProvider(scope,provider_path),scope,directory)
    return WriteWorker(operations,scope,**kwargs)


async def main():
    from .postgres_config import ROOT,local_dsn
    from .postgres_store import PostgresRunStore
    from .user_identity import UserIdentityStore
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--actor',required=True);parser.add_argument('--workspace',required=True)
    parser.add_argument('--schema',default='harness_users_lab')
    parser.add_argument('--provider-db',type=Path,default=ROOT/'data/runtime/jenkins-authenticated-simulator.sqlite3')
    parser.add_argument('--once',action='store_true');parser.add_argument('--lease-seconds',type=float,default=15)
    parser.add_argument('--crash-at',choices=['after-claim','after-send'])
    args=parser.parse_args()
    if args.crash_at and not args.once:parser.error('--crash-at requiere --once')
    directory=UserIdentityStore(PostgresRunStore(local_dsn()))
    worker=create_worker(PostgresRunStore(local_dsn(),schema=args.schema),directory,args.actor,args.workspace,args.provider_db,
        lease_seconds=args.lease_seconds,crash_at=args.crash_at)
    from .telemetry import configure
    configure('harness-user-write-worker')
    stop=asyncio.Event();loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop.set)
    while not stop.is_set():
        try:result=await worker.run_once()
        except ContractError as error:result={'event':'worker_error','error':error.code}
        if result:print(json.dumps(result),flush=True)
        if args.once:return
        try:await asyncio.wait_for(stop.wait(),timeout=.5)
        except TimeoutError:pass


if __name__=='__main__':
    try:asyncio.run(main())
    except (ContractError,ValueError):
        print(json.dumps({'error':'USER_WRITE_WORKER_UNAVAILABLE'}));raise SystemExit(1) from None
