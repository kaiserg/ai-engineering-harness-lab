"""Credenciales locales de desarrollo; nunca imprimir DSN o contraseñas."""
import os
from pathlib import Path
import secrets

from psycopg.conninfo import make_conninfo

from .contracts import ContractError

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'data/runtime'


def prepare_secrets():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    for name in ('admin', 'app'):
        path = RUNTIME / f'postgres-{name}-password'
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue  # No rotar una contraseña asociada a un volumen ya inicializado.
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(secrets.token_hex(32))


def local_dsn(*, admin=False):
    role = 'admin' if admin else 'app'
    try:
        password = (RUNTIME / f'postgres-{role}-password').read_text().strip()
    except OSError:
        raise ContractError('POSTGRES_NOT_CONFIGURED') from None
    host = '127.0.0.1' if admin else os.environ.get('HARNESS_POSTGRES_HOST', '127.0.0.1')
    try:
        port = 55432 if admin else int(os.environ.get('HARNESS_POSTGRES_PORT', '55432'))
        if not 1 <= port <= 65535:
            raise ValueError('invalid port')
    except ValueError:
        raise ContractError('POSTGRES_NOT_CONFIGURED') from None
    return make_conninfo(host=host, port=port, dbname='harness_lab',
                         user=f'harness_{role}', password=password)
