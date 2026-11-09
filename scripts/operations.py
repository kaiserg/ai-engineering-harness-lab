"""Ledger y efecto Jenkins ficticio. Ningún Jenkins real recibe escrituras."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from harness.contracts import ContractError
from harness.jenkins_simulator import JenkinsSimulator
from harness.operations import Operations,OperationInterrupted
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.proposals import ProposalStore
from harness.development import fixture_identity


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport',choices=['mcp','direct'],default='mcp')
    commands=parser.add_subparsers(dest='command',required=True)
    prepare=commands.add_parser('prepare');prepare.add_argument('proposal_id')
    for name in ('show','dispatch','reconcile','resolution','prepare-resolution','confirm-resolution'):
        command=commands.add_parser(name);command.add_argument('operation_id')
        if name=='confirm-resolution':
            command.add_argument('--hash',required=True);command.add_argument('--version',required=True,type=int)
        if name=='dispatch':command.add_argument('--interrupt',choices=['before-send','after-send'])
    head=commands.add_parser('fixture-head');head.add_argument('--revision',choices=['revision-A','revision-B'])
    args=parser.parse_args()
    identity=fixture_identity('operator');approver=fixture_identity('approver')
    path=ROOT/'data/runtime/jenkins-write-simulator.sqlite3'
    if args.command=='fixture-head':
        provider=JenkinsSimulator(path)
        if args.revision:provider.set_revision(identity.workspace_id,args.revision)
        result={'revision':provider.revision(identity.workspace_id)}
    else:
        from harness.adapters.mcp_jenkins_write import MCPJenkinsWrite
        provider=MCPJenkinsWrite(identity,path) if args.transport=='mcp' else JenkinsSimulator(path)
        service=Operations(ProposalStore(PostgresRunStore(local_dsn())),provider,
            {identity.actor_id:identity,approver.actor_id:approver})
        if args.command=='prepare':result=service.prepare(args.proposal_id,identity)
        elif args.command=='show':result=service.get(args.operation_id,identity)
        elif args.command=='dispatch':result=service.dispatch(args.operation_id,identity,interrupt=args.interrupt)
        elif args.command=='reconcile':result=service.reconcile(args.operation_id,identity)
        else:
            from harness.resolutions import Resolutions
            resolutions=Resolutions(service)
            if args.command=='resolution':result=resolutions.get(args.operation_id,identity)
            elif args.command=='prepare-resolution':result=resolutions.prepare(args.operation_id,identity)
            else:result=resolutions.confirm(args.operation_id,identity,approver,payload_hash=args.hash,expected_version=args.version)
    print(json.dumps(result,indent=2))
    return 1 if (result or {}).get('status') in ('UNKNOWN','NOT_APPLIED') else 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except OperationInterrupted:
        print(json.dumps({'checkpoint':'DISPATCHING','next':'Espere 10 segundos y ejecute reconcile; no reenvíe.'}),file=sys.stderr)
        raise SystemExit(2) from None
    except ContractError as error:
        print(json.dumps({'error':error.code}),file=sys.stderr)
        raise SystemExit(1) from None
