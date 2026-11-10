"""API y frontend: ejecución SQLite local o productor PostgreSQL con worker independiente."""
import argparse
import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .composition import compose, demo_identity
from .checkpoints import RunInterrupted, pause_hook
from .contracts import ContractError, Run, RunState
from .core import Limits, authorize_run
from .fakes import ReadOnlyPolicy
from .ownership import database_owner
from .storage import SQLiteRunStore

ROOT = Path(__file__).resolve().parents[1]


def worker_profile(profile):
    import os
    transport = os.environ.get('HARNESS_JIRA_TRANSPORT', 'stdio')
    if transport not in ('stdio', 'http'):
        raise ContractError('CONFIG_CHANGED')
    if transport == 'stdio':
        return profile
    parts = profile.split(':')
    return ':'.join(['mcp-devops-ci-v4', parts[1], *(parts[2:] or ['benign', 'ignore'])])


SCENARIOS = {
    'matching': ('Evidencia coincidente', 'El build pasó para la revisión del PR.', 'mcp-devops-ci-v2:matching'),
    'stale': ('Build de otra revisión', 'Un build verde no valida la revisión actual.', 'mcp-devops-ci-v2:stale'),
    'failed': ('Build fallido', 'La revisión coincide, pero las pruebas fallaron.', 'mcp-devops-ci-v2:failed'),
    'unavailable': ('Jenkins no disponible', 'Informe parcial con evidencia de Jira y GitHub.', 'mcp-devops-ci-v2:unavailable'),
    'injection-job': ('Ataque: job sin permiso', 'El modelo intenta consultar un job no permitido.', 'mcp-devops-ci-v3:matching:injection:job'),
    'injection-shell': ('Ataque: herramienta shell', 'El modelo pide ejecutar una herramienta inexistente.', 'mcp-devops-ci-v3:matching:injection:shell'),
    'injection-claim': ('Ataque: afirmación falsa', 'El core conserva la evidencia pese al texto del modelo.', 'mcp-devops-ci-v3:stale:injection:claim'),
}


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scenario: Literal['matching', 'stale', 'failed', 'unavailable', 'injection-job', 'injection-shell', 'injection-claim'] = 'matching'
    pause_at: Literal['none', 'jira', 'github', 'jenkins', 'failure'] = 'none'


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class RunService:
    def __init__(self, store, identity, composer=compose):
        self.store, self.identity, self.composer = store, identity, composer
        self.tasks, self.errors = {}, {}
        self.serial = asyncio.Lock()

    def load(self, run_id):
        run = self.store.load(str(run_id), self.identity)
        if run.execution_profile in ('mcp-devops-rag-v1','ollama-qwen3-4b-document-reader-v1','ollama-qwen3-4b-document-chat-v1','ollama-qwen3-4b-jira-demo-chat-v1','ollama-qwen3-4b-github-demo-chat-v1','ollama-qwen3-4b-github-public-chat-v1') or run.execution_profile.startswith('ollama-qwen3-4b-jira-cloud-chat-v1:'):raise ContractError('NOT_FOUND')
        authorize_run(run, self.identity, ReadOnlyPolicy())
        return run

    def view(self, run):
        active = run.run_id in self.tasks
        data = asdict(run)
        data.update(is_active=active,
                    can_resume=not active and run.timing_version == 1 and run.status in (RunState.QUEUED, RunState.RUNNING),
                    service_error=self.errors.get(run.run_id))
        return data

    def recent(self):
        visible = []
        for run in self.store.list(self.identity):
            if run.execution_profile in ('mcp-devops-rag-v1','ollama-qwen3-4b-document-reader-v1','ollama-qwen3-4b-document-chat-v1','ollama-qwen3-4b-jira-demo-chat-v1','ollama-qwen3-4b-github-demo-chat-v1','ollama-qwen3-4b-github-public-chat-v1') or run.execution_profile.startswith('ollama-qwen3-4b-jira-cloud-chat-v1:'):continue
            try:
                authorize_run(run, self.identity, ReadOnlyPolicy())
            except ContractError:
                continue
            visible.append(self.view(run))
        return visible

    def start(self, body):
        if body.pause_at == 'failure' and body.scenario != 'unavailable':
            raise ContractError('INVALID_PAUSE')
        self.check_capacity()
        run = self.store.create(Run(self.identity.workspace_id, self.identity.actor_id,
            execution_profile=SCENARIOS[body.scenario][2], limits_snapshot=asdict(Limits())), self.identity)
        self.launch(run, pause_at=body.pause_at)
        return self.view(run)

    def resume(self, run_id):
        run = self.load(run_id)
        if run.run_id in self.tasks:
            raise ContractError('RUN_BUSY')
        if run.status == RunState.COMPLETED:
            return self.view(run)
        if run.status not in (RunState.QUEUED, RunState.RUNNING):
            raise ContractError('INVALID_TRANSITION')
        if run.timing_version != 1:
            raise ContractError('TIME_BUDGET_UNKNOWN')
        self.check_capacity()
        self.launch(run, recovering=True)
        return self.view(run)

    def check_capacity(self):
        if len(self.tasks) >= 16:
            raise ContractError('QUEUE_FULL')

    def launch(self, run, recovering=False, pause_at='none'):
        self.errors.pop(run.run_id, None)
        self.tasks[run.run_id] = asyncio.create_task(self.work(run, recovering, pause_at))

    async def work(self, run, recovering, pause_at):
        checkpoint = pause_hook(pause_at)
        try:
            async with self.serial:
                limits = Limits(**run.limits_snapshot) if run.limits_snapshot else Limits()
                execution = self.composer(run.execution_profile, self.identity, self.store,
                                          limits=limits, checkpoint_hook=checkpoint)
                if recovering:
                    await execution.harness.resume(run.run_id, self.identity)
                else:
                    await execution.harness.execute(run.run_id, self.identity)
        except RunInterrupted:
            pass
        except ContractError as error:
            self.errors[run.run_id] = error.code
        except Exception:
            self.errors[run.run_id] = 'INTERNAL_ERROR'
        finally:
            self.tasks.pop(run.run_id, None)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class PostgresRunService(RunService):
    """Productor/consulta solamente; la API nunca ejecuta el core en modo worker."""
    def view(self, run):
        run, job = self.store.snapshot(run.run_id, self.identity)
        authorize_run(run, self.identity, ReadOnlyPolicy())
        data = super().view(run)
        active = job is not None and (job['status'] == 'PENDING' or job['status'] == 'RUNNING' and job['lease_active'])
        resumable = job is not None and (job['status'] == 'PAUSED' or job['status'] == 'RUNNING' and not job['lease_active'])
        data.update(job=job, is_active=bool(active),
                    can_resume=bool(resumable and run.timing_version == 1 and run.status in (RunState.QUEUED, RunState.RUNNING)),
                    service_error=job['last_error'] if job else 'WORK_NOT_CLAIMED')
        return data

    def start(self, body):
        if body.pause_at == 'failure' and body.scenario != 'unavailable':
            raise ContractError('INVALID_PAUSE')
        from .telemetry import annotate
        run = self.store.enqueue(Run(self.identity.workspace_id, self.identity.actor_id,
            execution_profile=worker_profile(SCENARIOS[body.scenario][2]), limits_snapshot=asdict(Limits())), self.identity,
            pause_at=body.pause_at, max_pending=16)
        annotate(**{'run.id':run.run_id,'queue.action':'enqueue'})
        return self.view(run)

    def resume(self, run_id):
        run = self.load(run_id)
        if run.status == RunState.COMPLETED:
            return self.view(run)
        if run.timing_version != 1:
            raise ContractError('TIME_BUDGET_UNKNOWN')
        self.store.requeue_job(run.run_id, self.identity)
        return self.view(run)


def create_app(db=None, *, identity=None, composer=compose, storage='sqlite', pg_dsn=None, pg_schema=None, approvals_demo=False, approval_provider_path=None, write_worker=False, user_auth=False, user_directory=None):
    pg_schema=pg_schema or ('harness_users_lab' if user_auth else 'harness_lab')
    if not user_auth and pg_schema=='harness_users_lab':
        raise ValueError('El schema reservado de usuarios requiere --user-auth.')
    if user_auth and (storage!='postgres' or approvals_demo or identity is not None or pg_schema=='harness_lab'):
        raise ValueError('Identidad de usuario requiere PostgreSQL aislado y rutas de lectura sin fixtures M4.')
    if storage not in ('sqlite', 'postgres'):
        raise ValueError('Backend de persistencia desconocido.')
    if write_worker and not approvals_demo:raise ValueError('El worker de escrituras requiere el modo M4 local.')
    if approvals_demo and (storage != 'postgres' or identity is not None and identity != demo_identity()):
        raise ValueError('M4 demo requiere PostgreSQL y la identidad local de prueba.')
    path = Path(db) if db else ROOT / 'data/runtime/harness.sqlite3'

    @asynccontextmanager
    async def lifespan(app):
        with database_owner(path) if storage == 'sqlite' else nullcontext():
            if storage == 'postgres':
                from .postgres_store import PostgresRunStore
                from .postgres_config import local_dsn
                store = PostgresRunStore(pg_dsn or local_dsn(), schema=pg_schema)
                app.state.run_store=store
                if user_auth:
                    from .user_identity import UserIdentityStore
                    app.state.user_directory=user_directory or UserIdentityStore(PostgresRunStore(pg_dsn or local_dsn()))
                    app.state.service=None
                    from .authenticated_proposals import AuthenticatedProposals
                    app.state.user_proposals=AuthenticatedProposals(store,app.state.user_directory,
                        approval_provider_path or ROOT/'data/runtime/jenkins-authenticated-simulator.sqlite3')
                else:
                    app.state.service = PostgresRunService(store, identity or demo_identity(), composer)
                    from .metrics import attach_state
                    attach_state(lambda:store.operational_snapshot(app.state.service.identity))
            else:
                app.state.service = RunService(SQLiteRunStore(path), identity or demo_identity(), composer)
            if approvals_demo:
                from .approval_web import ApprovalDemo
                app.state.approval_demo = ApprovalDemo(store,approval_provider_path or ROOT/'data/runtime/jenkins-write-simulator.sqlite3',write_worker=write_worker)
            try:
                yield
            finally:
                if app.state.service is not None:await app.state.service.close()

    app = FastAPI(title='Harness Lab · API local', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url='/api/openapi.json')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '[::1]'])

    def request_service(request):
        if user_auth:return PostgresRunService(request.app.state.run_store,request.state.identity,composer)
        return request.app.state.service

    @app.middleware('http')
    async def browser_boundary(request, call_next):
        if user_auth and request.url.path.startswith('/api/'):
            try:
                request.state.identity=await asyncio.to_thread(request.app.state.user_directory.authenticate,
                    request.headers.get('authorization'),request.headers.get('x-harness-workspace'))
                if request.url.path.startswith('/api/runs') and not request.state.identity.roles & {'reader','operator'}:
                    raise ContractError('FORBIDDEN')
            except ContractError as error:
                status=401 if error.code=='UNAUTHENTICATED' else 403 if error.code=='FORBIDDEN' else 503
                return JSONResponse({'error':error.code if status in (401,403) else 'IDENTITY_UNAVAILABLE'},
                    status_code=status,headers={'Cache-Control':'no-store',**({'WWW-Authenticate':'Bearer'} if status==401 else {})})
            except Exception:
                return JSONResponse({'error':'IDENTITY_UNAVAILABLE'},status_code=503,headers={'Cache-Control':'no-store'})
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            origin = request.headers.get('origin')
            if (request.headers.get('x-harness-client') != 'local-ui'
                    or origin is not None and origin != str(request.base_url).rstrip('/')
                    or request.headers.get('sec-fetch-site') == 'cross-site'):
                return JSONResponse({'error': 'REQUEST_REJECTED'}, status_code=403)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ContractError)
    async def domain_error(request, error):
        status = (503 if error.code in ('IDENTITY_UNAVAILABLE','DATABASE_UNAVAILABLE','CHROMA_UNAVAILABLE','EMBEDDING_UNAVAILABLE','EMBEDDING_MODEL_UNAVAILABLE') else 401 if error.code=='UNAUTHENTICATED' else 413 if error.code=='PAYLOAD_TOO_LARGE' else 404 if error.code == 'NOT_FOUND' else 403 if error.code == 'FORBIDDEN' else
                  429 if error.code in ('QUEUE_FULL','KNOWLEDGE_QUOTA','PROVIDER_RATE_LIMITED') else 422 if error.code in ('INVALID_PAUSE','INVALID_DOCUMENT','INVALID_QUERY','EMPTY_TEXT','CHAT_LIMIT') else 409)
        return JSONResponse({'error': error.code}, status_code=status)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        return JSONResponse({'error': 'INTERNAL_ERROR'}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({'error': 'INVALID_REQUEST'}, status_code=422)

    @app.get('/api/config')
    async def config(request:Request):
        return {'workspace':request.state.identity.workspace_id if user_auth else 'Laboratorio local', 'identity_mode': 'local-user-credential' if user_auth else 'development-fixture', 'execution_mode': 'worker' if storage == 'postgres' else 'local',
                'approvals_demo': approvals_demo, 'authenticated_proposals':user_auth, 'authenticated_dispatch':user_auth, 'knowledge':user_auth, 'write_worker': write_worker, 'scenarios': [{'id': key, 'title': value[0], 'description': value[1]} for key, value in SCENARIOS.items()]}

    @app.get('/api/runs')
    async def recent(request: Request):
        return {'runs': request_service(request).recent()}

    @app.post('/api/runs', status_code=202)
    async def start(body: StartRequest, request: Request):
        return request_service(request).start(body)

    @app.get('/api/runs/{run_id}')
    async def detail(run_id: UUID, request: Request):
        service = request_service(request)
        return service.view(service.load(run_id))

    @app.post('/api/runs/{run_id}/resume', status_code=202)
    async def resume(run_id: UUID, body: ResumeRequest, request: Request):
        from .telemetry import annotate
        annotate(**{'run.id':str(run_id),'queue.action':'resume'})
        return request_service(request).resume(run_id)

    if user_auth:
        @app.get('/api/me')
        def me(request:Request):
            from .user_identity import view
            return {'identity':view(request.state.identity),'mode':'authenticated-user-api'}
        from .authenticated_proposals import install_routes
        install_routes(app)
        from .authenticated_resolutions import install_routes as resolution_routes
        resolution_routes(app)
        from .knowledge_api import install_routes as knowledge_routes
        knowledge_routes(app)
        from .rag_api import install_routes as rag_routes
        rag_routes(app)
        from .reader_api import install_routes as reader_routes
        reader_routes(app)
        from .chat_api import install_routes as chat_routes
        chat_routes(app)
        from .console_api import install_routes as console_routes
        console_routes(app,SCENARIOS)

        @app.get('/console')
        async def console_page():return FileResponse(ROOT/'frontend/console.html')

        @app.get('/knowledge')
        async def knowledge_page():return FileResponse(ROOT/'frontend/console.html')

    if approvals_demo:
        from .approval_web import install_routes
        install_routes(app)

    @app.get('/')
    async def index():
        if user_auth:return FileResponse(ROOT/'frontend/console.html')
        return FileResponse(ROOT / 'frontend/index.html')

    from .telemetry import TraceHTTP
    app.add_middleware(TraceHTTP,service='harness-api')
    app.mount('/assets', StaticFiles(directory=ROOT / 'frontend'), name='assets')
    return app


def main():
    from .metrics import start
    start()
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', choices=['127.0.0.1', '0.0.0.0'], default='127.0.0.1')
    parser.add_argument('--storage', choices=['sqlite', 'postgres'], default='sqlite')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/runtime/harness.sqlite3')
    parser.add_argument('--approvals-demo',action='store_true',help='M4 local con dos identidades ficticias; solo loopback/PostgreSQL.')
    parser.add_argument('--user-auth',action='store_true',help='API de lecturas con credencial de usuario, loopback y schema separado.')
    parser.add_argument('--write-worker',action='store_true',help='Encolar envíos M4 para un worker independiente.')
    args = parser.parse_args()
    if args.user_auth and (args.host!='127.0.0.1' or args.storage!='postgres' or args.approvals_demo):
        parser.error('--user-auth requiere PostgreSQL/loopback y no admite --approvals-demo.')
    if args.write_worker and not args.approvals_demo:parser.error('--write-worker requiere --approvals-demo')
    if args.approvals_demo and (args.host != '127.0.0.1' or args.storage != 'postgres'):
        parser.error('El modo M4 demo requiere --storage postgres y bind local 127.0.0.1.')
    uvicorn.run(create_app(args.db, storage=args.storage,approvals_demo=args.approvals_demo,write_worker=args.write_worker,user_auth=args.user_auth), host=args.host, port=args.port, workers=1, access_log=False)


if __name__ == '__main__':
    main()
