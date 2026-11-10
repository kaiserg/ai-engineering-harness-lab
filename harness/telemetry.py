"""Instrumentación explícita y acotada: solo metadatos permitidos, nunca payloads."""
import atexit
from contextlib import contextmanager
import os
import re

from opentelemetry import trace, context as otel_context
from opentelemetry.trace import SpanKind, Status, StatusCode, Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_provider = None
TRACEPARENT = re.compile(r'^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$')
ALLOWED = {'run.id','job.attempt','job.fencing_token','job.age_ms','run.outcome',
    'run.model_calls','run.tool_calls','run.confirmed_results','tool.name','tool.call_id',
    'error.code','http.response.status_code','queue.action','operation.id','proposal.id','write.phase','write.state'}
ERROR_CODES = frozenset({
    'APPROVAL_EXPIRED', 'BUDGET_EXCEEDED', 'CANCELLED', 'CONFIG_CHANGED',
    'CONFLICT', 'CORRUPT_STATE', 'DATABASE_ERROR', 'DATABASE_IN_USE',
    'DATABASE_UNAVAILABLE', 'EVIDENCE_MISMATCH', 'FORBIDDEN', 'HTTP_REJECTED',
    'INCOMPLETE_EVIDENCE', 'INTERNAL_ERROR', 'INTERRUPTED', 'INVALID_ARGUMENT',
    'INVALID_CHECKPOINT', 'INVALID_MODEL_OUTPUT', 'INVALID_PAUSE', 'INVALID_TOOL_OUTPUT',
    'INVALID_TRANSITION', 'LEASE_LOST', 'MCP_IDENTITY_CONFIGURATION', 'MCP_ISSUER_CONFIGURATION',
    'MCP_ISSUER_UNAVAILABLE', 'MCP_SCHEMA_MISMATCH', 'MCP_TOOL_ERROR', 'MCP_UNAUTHENTICATED',
    'MCP_UNAVAILABLE', 'NOT_FOUND', 'POSTGRES_NOT_CONFIGURED', 'QUEUE_FULL',
    'RUN_BUSY', 'RUN_DEADLINE_EXCEEDED', 'STORAGE_SCHEMA_UNSUPPORTED', 'TIME_BUDGET_UNKNOWN',
    'EMBEDDING_UNAVAILABLE','EMBEDDING_MODEL_UNAVAILABLE','EMBEDDING_MODEL_CHANGED','INVALID_EMBEDDING','EVIDENCE_REVOKED','INVALID_VECTOR_RESULT','CHROMA_UNAVAILABLE','STALE_INDEX','EMPTY_TEXT','INCOMPATIBLE_INDEX','UNKNOWN_TOOL', 'UNSAFE_RECOVERY', 'UPSTREAM_TIMEOUT', 'UPSTREAM_UNAVAILABLE',
    'WORKER_ATTEMPTS_EXCEEDED', 'WORK_NOT_CLAIMED','OPERATION_BUSY','OPERATION_CLOSED','DISPATCH_EXPIRED',
    'SIMULATOR_UNAVAILABLE','SIMULATOR_STATE_LOST','STALE_PRECONDITION','UNKNOWN_EFFECT','WRITE_ATTEMPTS_EXHAUSTED',
})
TOOLS = {'knowledge_search','jira_get_issue','github_get_pull_request','jenkins_get_build','jenkins_request_tests','jenkins_find_request','jenkins_current_revision','jenkins_close_request'}


def configure(service, *, provider=None):
    global _provider
    if provider is not None:
        _provider = provider
        return provider
    if _provider is not None or os.environ.get('HARNESS_TRACING') != '1':
        return _provider
    try:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        # Destino de infraestructura fijo; ni token ni petición deciden el collector.
        endpoint=os.environ.get('HARNESS_OTLP_ENDPOINT','http://jaeger:4318/v1/traces')
        if endpoint not in ('http://jaeger:4318/v1/traces','http://127.0.0.1:4318/v1/traces'):
            return None
        provider=TracerProvider(resource=Resource({'service.name':service,'deployment.environment.name':'lab'}),
                               shutdown_on_exit=False)
        import requests
        session=requests.Session()
        session.trust_env=False
        exporter=OTLPSpanExporter(endpoint=endpoint,timeout=1,session=session)
        provider.add_span_processor(BatchSpanProcessor(exporter,max_queue_size=256,
            max_export_batch_size=64,schedule_delay_millis=500,export_timeout_millis=1000))
        _provider=provider
        atexit.register(shutdown)
    except Exception:
        return None
    return _provider


def shutdown():
    try:
        if _provider:
            _provider.shutdown()
    except Exception:
        pass


def attributes(values):
    result={}
    for key,value in values.items():
        if key not in ALLOWED:
            continue
        if key in ('run.id','tool.call_id','operation.id','proposal.id') and (type(value) is not str or not re.fullmatch(r'[0-9a-f-]{36}',value)):
            continue
        if key=='write.phase' and value not in {'prepare','authorize','dispatch','lookup','resolution.prepare','resolution.confirm','mcp','worker'}:continue
        if key=='write.state' and value not in {'PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED','NOT_APPLIED','PENDING','RUNNING','DONE','NEEDS_REVIEW','PENDING_REVIEW','CONFIRMED'}:continue
        if key=='tool.name' and value not in TOOLS:
            value='unknown'
        if key=='error.code' and value not in ERROR_CODES:
            continue
        if key=='run.outcome' and value not in {'COMPLETED','FAILED','CANCELLED','PAUSED','FINISHED'}:
            continue
        if type(value) in (str,int,float,bool):
            result[key]=value[:80] if type(value) is str else value
    return result


def annotate(**values):
    try:
        safe=attributes(values)
        trace.get_current_span().set_attributes(safe)
        if 'error.code' in safe:
            trace.get_current_span().set_status(Status(StatusCode.ERROR))
    except Exception:
        pass


def carrier(value=None):
    try:
        data={}
        TraceContextTextMapPropagator().inject(data)
        value=data.get('traceparent') if value is None else value
        return value if type(value) is str and TRACEPARENT.fullmatch(value) and context(value) is not None else None
    except Exception:
        return None


def context(value):
    if type(value) is not str or not TRACEPARENT.fullmatch(value):
        return None
    ctx=TraceContextTextMapPropagator().extract({'traceparent':value})
    return ctx if trace.get_current_span(ctx).get_span_context().is_valid else None


def headers():
    value=carrier()
    return {'traceparent':value} if value else {}


@contextmanager
def span(name, *, values=None, parent=None, root=False, links=(), kind=SpanKind.INTERNAL, start_time=None):
    current=None
    token=None
    try:
        tracer=(_provider or trace.get_tracer_provider()).get_tracer('harness.lab')
        linked=[Link(trace.get_current_span(ctx).get_span_context()) for value in links if (ctx:=context(value)) is not None]
        ctx=otel_context.Context() if root else context(parent) if parent else None
        current=tracer.start_span(name,context=ctx,kind=kind,attributes=attributes(values or {}),
            links=linked,start_time=start_time,record_exception=False,set_status_on_exception=False)
        token=otel_context.attach(trace.set_span_in_context(current,ctx))
    except Exception:
        pass
    try:
        yield current
    except BaseException as error:
        if current is not None:
            try:
                # Mensaje/stacktrace pueden contener secretos; nunca record_exception automático.
                code=getattr(error,'code',None) or ('UPSTREAM_TIMEOUT' if isinstance(error,TimeoutError) else
                    'INTERRUPTED' if type(error).__name__ in ('RunInterrupted','OperationInterrupted') else 'CANCELLED' if
                    type(error).__name__=='CancelledError' else 'INTERNAL_ERROR')
                current.set_attributes(attributes({'error.code':code}))
                current.set_status(Status(StatusCode.ERROR))
            except Exception:
                pass
        raise
    finally:
        try:
            if token is not None:
                otel_context.detach(token)
            if current is not None:
                current.end()
        except Exception:
            pass


def traced(name, *, values=None):
    def decorator(function):
        async def call(*args,**kwargs):
            with span(name,values=values):
                return await function(*args,**kwargs)
        return call
    return decorator


class TraceHTTP:
    """ASGI sin captura de headers/body; solo endpoints operativos conocidos."""
    def __init__(self,app,service):
        self.app,self.service=app,service
        configure(service)

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        path=scope.get('path','')
        name=('api.runs.create' if path=='/api/runs' else 'api.runs.resume' if re.fullmatch(
            r'/api/runs/[0-9a-f-]{36}/resume',path) else None) if self.service=='harness-api' else (
            'identity.token' if path=='/token' else 'mcp.http' if path.rstrip('/')=='/mcp' else None)
        if self.service=='harness-api' and not name:
            if path=='/api/knowledge/agent/runs':name='api.rag.create'
            elif re.fullmatch(r'/api/knowledge/agent/runs/[0-9a-f-]{36}/resume',path):name='api.rag.resume'
        if self.service=='harness-api' and not name:
            match=re.fullmatch(r'/api/operations/([0-9a-f-]{36})/(dispatch|reconcile|resolution|resolution/confirm)',path)
            if match:name='api.write.'+{'dispatch':'submit','reconcile':'lookup','resolution':'resolution.prepare','resolution/confirm':'resolution.confirm'}[match[2]]
        if not name or scope['method']!='POST':
            return await self.app(scope,receive,send)
        incoming=dict(scope.get('headers',[])).get(b'traceparent',b'').decode('ascii',errors='replace')
        values={}
        if self.service=='harness-api' and name.startswith('api.write.'):
            values={'operation.id':path.split('/')[3]}
        with span(name,parent=incoming,root=self.service=='harness-api',kind=SpanKind.SERVER,values=values):
            async def traced_send(message):
                if message['type']=='http.response.start':
                    annotate(**{'http.response.status_code':message['status']})
                    if message['status']>=400:
                        annotate(**{'error.code':'HTTP_REJECTED'})
                await send(message)
            await self.app(scope,receive,traced_send)
