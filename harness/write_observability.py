"""Observación fail-soft del canal de escrituras; sin IDs ni contenido en métricas."""
from contextlib import contextmanager
from functools import wraps
import math
import os
from time import monotonic,time
from prometheus_client import CollectorRegistry,Counter,Gauge,Histogram,start_http_server
from prometheus_client.core import GaugeMetricFamily
from .metrics import safe,BUCKETS
from .telemetry import span,annotate

JOBS=('PENDING','RUNNING','DONE','NEEDS_REVIEW')
STATES=('PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED','NOT_APPLIED')
ACTIONS={'revision','request','lookup','close'}


class WriteMetrics:
    def __init__(self):
        self.registry=CollectorRegistry();self.snapshot=None
        Gauge('harness_process_start_time_seconds','Inicio del exporter de escritura.',registry=self.registry).set(time())
        self.calls=Counter('harness_write_calls_total','Intentos de llamadas del cliente MCP; no efectos.',
            ['action','outcome'],registry=self.registry)
        self.call_time=Histogram('harness_write_call_duration_seconds','Duración del cliente MCP, incluido startup.',
            ['action','outcome'],buckets=BUCKETS,registry=self.registry)
        self.attempts=Counter('harness_write_worker_attempts_total','Reclamos atendidos por el worker.',
            ['result'],registry=self.registry)
        self.attempt_time=Histogram('harness_write_worker_duration_seconds','Duración de un reclamo atendido.',
            ['result'],buckets=BUCKETS,registry=self.registry)
        self.registry.register(self)

    def collect(self):
        if self.snapshot is None:return
        success=GaugeMetricFamily('harness_write_collection_success','Snapshot de escrituras válido.')
        try:
            data=self.snapshot()
            if set(data['jobs'])-set(JOBS) or set(data['operations'])-set(STATES):raise ValueError()
            jobs=GaugeMetricFamily('harness_write_jobs_state','Trabajos de escritura persistidos.',labels=['status'])
            ledger=GaugeMetricFamily('harness_write_ledger_state','Operaciones persistidas.',labels=['status'])
            for group,statuses,metric in (('jobs',JOBS,jobs),('operations',STATES,ledger)):
                for status in statuses:
                    value=data[group].get(status,0)
                    if type(value) is not int or value<0:raise ValueError()
                    metric.add_metric([status],value)
            count=data['expired_leases'];age=data['oldest_pending_seconds']
            if type(count) is not int or count<0 or type(age) not in (int,float) or not math.isfinite(age) or age<0:raise ValueError()
            expired=GaugeMetricFamily('harness_write_jobs_expired_lease','Leases vencidos del worker de escrituras.')
            oldest=GaugeMetricFamily('harness_write_oldest_pending_seconds','Edad del trabajo PENDING más antiguo.')
            expired.add_metric([],count);oldest.add_metric([],age);success.add_metric([],1)
            yield jobs;yield ledger;yield expired;yield oldest
        except Exception:success.add_metric([],0)
        yield success


METRICS=WriteMetrics()
_server=None


def start(port,snapshot):
    global _server
    METRICS.snapshot=snapshot
    if os.environ.get('HARNESS_WRITE_METRICS')!='1':return
    try:
        if _server is None:_server=start_http_server(port,addr='127.0.0.1',registry=METRICS.registry)
    except Exception:pass


def phase(name,*,id_key='operation.id'):
    def decorate(function):
        @wraps(function)
        def call(self,identifier,*args,**kwargs):
            values={'write.phase':name}
            if type(identifier) is str:values[id_key]=identifier
            with span('write.'+name,values=values):
                result=function(self,identifier,*args,**kwargs)
                if type(result) is dict:
                    annotate(**{'write.state':result.get('status'),'error.code':result.get('outcome_code')})
                return result
        return call
    return decorate


@contextmanager
def client_call(action):
    started=monotonic();outcome='success'
    action=action if action in ACTIONS else 'unknown'
    try:yield
    except BaseException:
        outcome='error';raise
    finally:
        safe(lambda:METRICS.calls.labels(action,outcome).inc())
        safe(lambda:METRICS.call_time.labels(action,outcome).observe(max(0,monotonic()-started)))


@contextmanager
def worker_attempt():
    started=monotonic();outcome=['error']
    def finish(result):
        result=result if type(result) is dict else {}
        outcome[0]={'DONE':'done','PENDING':'deferred','NEEDS_REVIEW':'review',
            'lease_lost':'lease_lost'}.get(result.get('status') or result.get('event'),'error')
    try:yield finish
    finally:
        safe(lambda:METRICS.attempts.labels(outcome[0]).inc())
        safe(lambda:METRICS.attempt_time.labels(outcome[0]).observe(max(0,monotonic()-started)))
