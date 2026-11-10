"""Métricas numéricas acotadas. Nunca deciden autorización, commits ni reintentos."""
from contextlib import contextmanager
import math
import os
from time import monotonic, time
import threading

from prometheus_client import CollectorRegistry, Counter, Histogram, Gauge, start_http_server
from prometheus_client.core import GaugeMetricFamily

TOOLS={'knowledge_search','jira_get_issue','github_get_pull_request','jenkins_get_build'}
ATTEMPTS={'completed','failed','paused','cancelled','lease_lost'}
BUCKETS=(.005,.01,.025,.05,.1,.25,.5,1,2.5,5,10,30,60,120)


class Metrics:
    def __init__(self):
        self.registry=CollectorRegistry()
        Gauge('harness_process_start_time_seconds','Inicio Unix del exporter para detectar transiciones.',
              registry=self.registry).set(time())
        self.attempts=Counter('harness_attempts_total','Intentos terminados del proceso, no runs únicos.',
                              ['outcome'],registry=self.registry)
        self.recoveries=Counter('harness_recovery_attempts_total','Claims con attempts mayor que uno.',registry=self.registry)
        self.attempt_time=Histogram('harness_attempt_duration_seconds','Tiempo activo del intento en el worker.',
                                   ['outcome'],buckets=BUCKETS,registry=self.registry)
        self.operations=Counter('harness_operations_total','Invocaciones reales de puertos del modelo/tools.',
                                ['operation','tool','outcome'],registry=self.registry)
        self.operation_time=Histogram('harness_operation_duration_seconds','Duración de llamadas al puerto.',
                                ['operation','tool','outcome'],buckets=BUCKETS,registry=self.registry)
        self.auth=Counter('harness_auth_rejections_total','Respuestas 401/403 del endpoint de servicio.',
                          ['endpoint','status'],registry=self.registry)
        for outcome in sorted(ATTEMPTS):
            self.attempts.labels(outcome)
        self.state=None
        self.certificate=None
        self.registry.register(self)

    def collect(self):
        if self.state is not None:
            success=GaugeMetricFamily('harness_state_collection_success','Último snapshot PostgreSQL válido.')
            try:
                data=self.state()
                if (set(data['runs'])-{'QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED'}
                        or set(data['jobs'])-{'PENDING','RUNNING','PAUSED','DONE','FAILED','CANCELLED'}):
                    raise ValueError()
                runs=GaugeMetricFamily('harness_runs_state','Runs actuales persistidos.',labels=['status'])
                jobs=GaugeMetricFamily('harness_jobs_state','Jobs actuales persistidos.',labels=['status'])
                # Validar el snapshot completo antes de publicar; nunca convertir fallo de DB en cero.
                for status in ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED'):
                    value=data['runs'].get(status,0)
                    if type(value) is not int or value<0: raise ValueError()
                    runs.add_metric([status],value)
                for status in ('PENDING','RUNNING','PAUSED','DONE','FAILED','CANCELLED'):
                    value=data['jobs'].get(status,0)
                    if type(value) is not int or value<0: raise ValueError()
                    jobs.add_metric([status],value)
                if type(data['expired_leases']) is not int or data['expired_leases']<0: raise ValueError()
                expired=GaugeMetricFamily('harness_jobs_expired_lease','Jobs RUNNING cuyo lease ya venció.')
                expired.add_metric([],data['expired_leases'])
                success.add_metric([],1)
                yield runs
                yield jobs
                yield expired
            except Exception:
                success.add_metric([],0)
            yield success
        if self.certificate is not None:
            success=GaugeMetricFamily('harness_certificate_collection_success','Certificado activo legible.')
            try:
                expiry=float(self.certificate())
                if not math.isfinite(expiry) or expiry<=0: raise ValueError()
                certificate=GaugeMetricFamily('harness_certificate_not_after_seconds','Vencimiento Unix del certificado servido.')
                certificate.add_metric([],expiry)
                success.add_metric([],1)
                yield certificate
            except Exception:
                success.add_metric([],0)
            yield success


METRICS=Metrics()
_server=None
_server_lock=threading.Lock()


def start():
    global _server
    if os.environ.get('HARNESS_METRICS')!='1':
        return
    try:
        with _server_lock:
            if _server is None:
                _server=start_http_server(9100,addr='0.0.0.0',registry=METRICS.registry)
    except Exception:
        # Un endpoint de observación caído no cambia el comportamiento del harness.
        pass


def attach_state(callback):
    METRICS.state=callback


def attach_certificate(callback):
    METRICS.certificate=callback


def safe(function):
    try:
        function()
    except Exception:
        pass


@contextmanager
def operation(kind,tool='none'):
    started=monotonic()
    outcome='success'
    tool=tool if type(tool) is str and tool in TOOLS else 'none' if tool=='none' else 'unknown'
    kind=kind if kind in ('model','tool') else 'unknown'
    try:
        yield
    except BaseException as error:
        outcome='timeout' if isinstance(error,TimeoutError) else 'cancelled' if type(error).__name__=='CancelledError' else 'error'
        raise
    finally:
        elapsed=max(0,monotonic()-started)
        safe(lambda:METRICS.operations.labels(kind,tool,outcome).inc())
        safe(lambda:METRICS.operation_time.labels(kind,tool,outcome).observe(elapsed))


@contextmanager
def attempt(recovered=False):
    started=monotonic()
    outcome=['failed']
    if recovered:
        safe(lambda:METRICS.recoveries.inc())
    def finish(result):
        value=str(result.get('status') or result.get('event','failed')).lower()
        outcome[0]=value if value in ATTEMPTS else 'failed'
    try:
        yield finish
    except BaseException as error:
        outcome[0]='lease_lost' if getattr(error,'code',None)=='LEASE_LOST' else 'cancelled' if type(error).__name__=='CancelledError' else 'failed'
        raise
    finally:
        elapsed=max(0,monotonic()-started)
        safe(lambda:METRICS.attempts.labels(outcome[0]).inc())
        safe(lambda:METRICS.attempt_time.labels(outcome[0]).observe(elapsed))


class MetricsHTTP:
    def __init__(self,app,endpoint):
        self.app,self.endpoint=app,endpoint
        for status in ('401','403'):
            safe(lambda status=status:METRICS.auth.labels(endpoint,status))
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='POST' or scope.get('path','').rstrip('/')!=('/token' if self.endpoint=='token' else '/mcp'):
            return await self.app(scope,receive,send)
        async def measured_send(message):
            if message['type']=='http.response.start' and message['status'] in (401,403):
                safe(lambda:METRICS.auth.labels(self.endpoint,str(message['status'])).inc())
            await send(message)
        await self.app(scope,receive,measured_send)
