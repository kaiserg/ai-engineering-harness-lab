"""Ejercicio PostgreSQL independiente: admitir y consultar trabajo durable para el worker."""
import argparse
from dataclasses import asdict
import json

from .composition import demo_identity
from .contracts import ContractError, Run
from .core import Limits
from .postgres_config import local_dsn
from .postgres_store import PostgresRunStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['enqueue', 'status'])
    args = parser.parse_args()
    identity = demo_identity()
    store = PostgresRunStore(local_dsn())
    if args.command == 'enqueue':
        run = store.enqueue(Run(identity.workspace_id, identity.actor_id,
            execution_profile='mcp-devops-ci-v2:matching', limits_snapshot=asdict(Limits())), identity)
        print(json.dumps({'run_id': run.run_id, 'status': run.status, 'message': 'Run y trabajo confirmados para el worker.'}))
    else:
        print(json.dumps({'runs': [{'run_id': run.run_id, 'status': run.status} for run in store.list(identity)],
                          'pending_jobs': store.pending_jobs(identity)}, default=str, indent=2))


if __name__ == '__main__':
    try:
        main()
    except ContractError as error:
        print(json.dumps({'error': error.code}))
        raise SystemExit(1)
