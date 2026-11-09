"""CLI local de desarrollo: propuestas/aprobaciones, sin escrituras DevOps."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.composition import compose,demo_identity
from harness.contracts import ContractError,Run
from harness.development import fixture_identity
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.proposals import ProposalStore



async def prepare(runs):
    identity=fixture_identity('operator')
    profile='mcp-devops-ci-v3:stale:benign:ignore'
    execution=compose(profile,identity,runs)
    run=runs.create(Run(identity.workspace_id,identity.actor_id,execution_profile=profile),identity)
    result=await execution.harness.execute(run.run_id,identity)
    if result.status!='COMPLETED':
        raise ContractError('INCOMPLETE_EVIDENCE')
    return result.run_id


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('prepare',help='Crear evidencia sintética con build de otra revisión; imprimir run_id.')
    create=commands.add_parser('create')
    create.add_argument('--run-id',required=True)
    create.add_argument('--key',required=True,help='UUID estable del cliente; reusar al repetir la solicitud.')
    view=commands.add_parser('show');view.add_argument('proposal_id')
    view.add_argument('--actor',choices=['operator','approver'],default='operator')
    decide=commands.add_parser('decide');decide.add_argument('proposal_id')
    decide.add_argument('--hash',required=True);decide.add_argument('--version',required=True,type=int)
    decide.add_argument('--decision',choices=['approve','reject'],required=True)
    args=parser.parse_args()
    runs=PostgresRunStore(local_dsn())
    if args.command=='prepare':
        print(json.dumps({'run_id':asyncio.run(prepare(runs)),'example_request_key':str(uuid4())}))
        return
    store=ProposalStore(runs)
    if args.command=='create':
        result=store.create(args.run_id,fixture_identity('operator'),args.key)
    elif args.command=='show':
        result=store.get(args.proposal_id,fixture_identity(args.actor))
    else:
        result=store.decide(args.proposal_id,fixture_identity('approver'),payload_hash=args.hash,
            expected_version=args.version,decision='APPROVED' if args.decision=='approve' else 'REJECTED')
    print(json.dumps(result,indent=2,ensure_ascii=False))


if __name__=='__main__':
    try:
        main()
    except ContractError as error:
        print(json.dumps({'error':error.code}),file=sys.stderr)
        raise SystemExit(1) from None
