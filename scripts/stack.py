"""Operar la app completa usando el contexto local del laboratorio, sin borrar volúmenes."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.postgres_config import RUNTIME


def compose_command():
    context_path = RUNTIME / 'docker-context'
    context = context_path.read_text().strip() if context_path.exists() else None
    return ['docker'] + (['--context', context] if context else []) + ['compose', '-f', str(ROOT / 'compose.yaml'), '--profile', 'app']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['up', 'status', 'down', 'logs', 'reload-dashboard'])
    args = parser.parse_args()
    if args.command == 'down':
        from scripts.security_live import schedule_stop
        schedule_stop()
    if args.command in ('up', 'down'):
        from harness.security_lifecycle import operation_lock
        with operation_lock():
            return execute(args)
    return execute(args)


def execute(args):
    if args.command == 'up':
        if any((RUNTIME/name).exists() for name in ('security-operation.json','security-live-operation.json')):
            raise ValueError('Use scripts/security.py recover before starting services')
        from harness.service_tls import prepare_tls
        from harness.security_lifecycle import ensure_generation
        ensure_generation()
        prepare_tls()
        from harness.live_materials import publish
        current = ensure_generation()
        publish('identity', current)
        publish('jira', current)
        subprocess.run([sys.executable, str(ROOT / 'scripts/postgres.py'), 'up'], cwd=ROOT, check=True)
        subprocess.run(compose_command() + ['build', 'api'], cwd=ROOT, check=True)
        subprocess.run(compose_command() + ['up', '-d', '--no-build', '--wait'], cwd=ROOT, check=True)
        from scripts.security_live import schedule_start
        schedule_start()
        print('Consola de Compose preparada: http://127.0.0.1:8002')
    elif args.command == 'reload-dashboard':
        subprocess.run(compose_command() + ['restart', 'grafana'], cwd=ROOT, check=True)
        subprocess.run(compose_command() + ['up', '-d', '--no-deps', '--no-build', '--wait', 'grafana'], cwd=ROOT, check=True)
    elif args.command == 'status':
        subprocess.run(compose_command() + ['ps'], cwd=ROOT, check=True)
    elif args.command == 'logs':
        subprocess.run(compose_command() + ['logs', '--tail', '80', 'api', 'worker', 'jira-mcp', 'identity'], cwd=ROOT, check=True)
    else:
        subprocess.run(compose_command() + ['down'], cwd=ROOT, check=True)  # Conserva el volumen y credenciales.


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, OSError, ValueError):
        print('No se pudo completar la operación de Compose.', file=sys.stderr)
        raise SystemExit(1)
