"""Incidente local: worker detenido, alertas y recuperación de evidencia confirmada."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.postgres_config import RUNTIME, local_dsn
from harness.postgres_store import PostgresRunStore
from harness.security_lifecycle import atomic_json, operation_lock
from scripts.stack import compose_command

JOURNAL = RUNTIME / 'incident.json'


def get(url, body=None):
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'X-Harness-Client': 'local-ui'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def api(path, body=None):
    return get('http://127.0.0.1:8002/api' + path, body)


def compose(*args):
    return subprocess.check_output(compose_command() + list(args), cwd=ROOT,
        text=True, stderr=subprocess.STDOUT, timeout=90)


def wait(check, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise TimeoutError('No se alcanzó la condición esperada; use recover.')


def run_state(identifier, status):
    value = api('/runs/' + identifier)
    if value['job']['status'] in ('FAILED', 'CANCELLED'):
        raise ValueError('El run terminó sin éxito; revisar sin repetir herramientas automáticamente.')
    return value if value['job']['status'] == status else None


def evidence(value):
    return hashlib.sha256(json.dumps(value['results'][0], sort_keys=True).encode()).hexdigest()


def trace(identifier):
    with PostgresRunStore(local_dsn())._database() as db:
        row = db.execute('SELECT previous_traceparent FROM jobs WHERE run_id=%s', (identifier,)).fetchone()
    return row['previous_traceparent'].split('-')[1]


def rules(port):
    path = '/api/v1/rules' if port == 9090 else '/api/prometheus/grafana/api/v1/rules'
    return [rule for group in get(f'http://127.0.0.1:{port}' + path)['data']['groups'] for rule in group['rules']]


def alarm_states():
    prom = next(rule for rule in rules(9090) if rule['name'] == 'HarnessExporterUnavailable')
    grafana = next(rule for rule in rules(3003) if rule['uid'] == 'harness-queue-pending')
    if grafana.get('health') != 'ok':
        raise ValueError('La regla de Grafana tiene error de evaluación.')
    worker = [a for a in prom['alerts'] if a['labels'].get('role') == 'worker']
    return {'prometheus': worker[0]['state'] if worker else 'inactive', 'grafana': grafana['state']}


def check_completed(state, done):
    if evidence(done) != state['evidence_sha256'] or (done['model_calls'], done['tool_calls']) != (4, 3):
        raise ValueError('La recuperación no conservó evidencia/presupuesto esperado.')
    if done['job']['attempts'] != 2:
        raise ValueError('Se esperaba exactamente un intento de recuperación.')


def recover(state):
    # La intención durable se escribe ANTES de detener; start es idempotente incluso
    # si el proceso murió entre guardar la intención y ejecutar stop.
    if not state.get('restore_worker'):
        return state
    compose('start', 'worker')
    identifier = state['run_id']
    current = api('/runs/' + identifier)
    if current['job']['status'] == 'PAUSED':
        api('/runs/' + identifier + '/resume', {})
    done = wait(lambda: run_state(identifier, 'DONE'), 45)
    check_completed(state, done)
    state['second_trace'] = trace(identifier)
    wait(lambda: alarm_states() == {'prometheus': 'inactive', 'grafana': 'inactive'}, 90)
    state.update(phase='recovered', restore_worker=False, model_calls=4, tool_calls=3,
        attempts=done['job']['attempts'], alarms_resolved=True)
    atomic_json(JOURNAL, state)
    return state


def exercise():
    if JOURNAL.exists() and json.loads(JOURNAL.read_text()).get('restore_worker'):
        raise ValueError('Hay un incidente pendiente: ejecute recover.')
    if 'worker' not in compose('ps', '--status', 'running', '--services').splitlines():
        raise ValueError('El worker debe estar activo antes del ejercicio.')
    # Consultar todos los jobs con el mismo rol de la app; no modificar trabajos ajenos.
    with PostgresRunStore(local_dsn())._database() as db:
        busy = db.execute("SELECT count(*) AS n FROM jobs WHERE status IN ('PENDING','RUNNING')").fetchone()['n']
    if busy:
        raise ValueError('La cola debe estar inactiva. Termine los otros runs antes del incidente.')
    identifier = api('/runs', {'scenario': 'matching', 'pause_at': 'jira'})['run_id']
    paused = wait(lambda: run_state(identifier, 'PAUSED'), 30)
    state = dict(run_id=identifier, phase='stop_intended', restore_worker=True,
        evidence_sha256=evidence(paused), first_trace=trace(identifier))
    atomic_json(JOURNAL, state)
    try:
        compose('stop', 'worker')
        api('/runs/' + identifier + '/resume', {})
        waiting = run_state(identifier, 'PENDING')
        if not waiting or evidence(waiting) != state['evidence_sha256']:
            raise ValueError('Otro consumidor o una modificación inesperada impide simular el incidente.')
        state['phase'] = 'worker_stopped'
        atomic_json(JOURNAL, state)
        print('Worker detenido; esperando las dos alertas (aprox. 40–60 s).', flush=True)
        wait(lambda: alarm_states() == {'prometheus': 'firing', 'grafana': 'firing'})
        # Verificar entrega real de ambos motores al receptor local, sin avisos externos.
        def delivered():
            alerts = get('http://127.0.0.1:9093/api/v2/alerts')
            return {'prometheus', 'grafana'} <= {a['labels'].get('source') for a in alerts}
        wait(delivered, 45)
        state.update(phase='alerts_firing', alerts_delivered=True)
        atomic_json(JOURNAL, state)
        print('Ambas alertas disparadas y recibidas localmente; restaurando worker.', flush=True)
    finally:
        # También ante excepción o Ctrl-C. Si hay SIGKILL, recover lee el journal.
        state = recover(state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'recover', 'status'])
    args = parser.parse_args()
    if args.command == 'status':
        state = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {'phase': 'not_started'}
    else:
        with operation_lock():
            state = exercise() if args.command == 'run' else recover(json.loads(JOURNAL.read_text()))
    print(json.dumps(state, indent=2))
    for key in ('first_trace', 'second_trace'):
        if key in state:
            print(key + ': http://127.0.0.1:16686/trace/' + state[key])


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as error:
        print('El ejercicio no terminó. Revise scripts/incident.py status y ejecute recover si hay restauración pendiente.', file=sys.stderr)
        raise SystemExit(1) from None
