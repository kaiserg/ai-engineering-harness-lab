"""Preparar y operar únicamente el servicio PostgreSQL de este proyecto."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg import sql
from harness.contracts import ContractError
from harness.postgres_config import RUNTIME, local_dsn, prepare_secrets


def provision():
    from psycopg.conninfo import conninfo_to_dict
    password = conninfo_to_dict(local_dsn())['password']
    with psycopg.connect(local_dsn(admin=True), connect_timeout=5) as db:
        if db.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', ('harness_app',)).fetchone() is None:
            db.execute(sql.SQL('CREATE ROLE harness_app LOGIN PASSWORD {}').format(sql.Literal(password)))
        db.execute('GRANT CONNECT, CREATE ON DATABASE harness_lab TO harness_app')
    with psycopg.connect(local_dsn(), connect_timeout=5) as db:
        if db.execute('SELECT rolsuper FROM pg_roles WHERE rolname = current_user').fetchone()[0]:
            raise ContractError('INVALID_DATABASE_ROLE')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', help='Contexto Docker explícito; se conserva localmente tras un up exitoso.')
    parser.add_argument('command', choices=['up', 'status', 'restart', 'down'])
    args = parser.parse_args()
    prepare_secrets()
    context_path = RUNTIME / 'docker-context'
    context = args.context or (context_path.read_text().strip() if context_path.exists() else None)
    command = ['docker'] + (['--context', context] if context else []) + ['compose', '-f', str(ROOT / 'compose.yaml')]
    if args.command == 'up':
        subprocess.run(command + ['up', '-d', '--wait', 'postgres'], cwd=ROOT, check=True)
    elif args.command == 'restart':
        subprocess.run(command + ['restart', 'postgres'], cwd=ROOT, check=True)
        subprocess.run(command + ['up', '-d', '--wait', 'postgres'], cwd=ROOT, check=True)
    elif args.command == 'down':
        active = subprocess.check_output(command + ['--profile', 'app', 'ps', '--status', 'running', '-q', 'api', 'worker'], cwd=ROOT, text=True)
        if active.strip():
            print('La app completa está activa. Usá: uv run python scripts/stack.py down', file=sys.stderr)
            raise SystemExit(1)
        subprocess.run(command + ['down'], cwd=ROOT, check=True)  # El volumen se conserva.
        return
    else:
        subprocess.run(command + ['ps'], cwd=ROOT, check=True)
        return
    provision()
    if context:
        context_path.write_text(context)
    print('PostgreSQL preparado en 127.0.0.1:55432; rol de aplicación sin superusuario.')


if __name__ == '__main__':
    try:
        main()
    except (ContractError, psycopg.Error, subprocess.CalledProcessError, OSError):
        print('No se pudo preparar PostgreSQL. Revisá el motor Docker y la configuración local.', file=sys.stderr)
        raise SystemExit(1)
