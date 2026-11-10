"""Cliente de propuestas autenticadas: revisión explícita y admisión de un envío por worker."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.users import load_credential
from harness.postgres_config import RUNTIME
from harness.proposals import uuid_text
from harness.contracts import ContractError


def main():
    import httpx
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('inbox')
    propose=sub.add_parser('propose');propose.add_argument('run_id');propose.add_argument('--request-key',required=True)
    for action in ('review','prepare','operation','resolution','prepare-resolution'):sub.add_parser(action).add_argument('identifier')
    dispatch=sub.add_parser('dispatch');dispatch.add_argument('identifier');dispatch.add_argument('--interrupt',choices=['before-send','after-send'])
    decide=sub.add_parser('decide');decide.add_argument('identifier');decide.add_argument('--decision',required=True,choices=['APPROVED','REJECTED'])
    decide.add_argument('--payload-hash',required=True);decide.add_argument('--version',required=True,type=int)
    confirm=sub.add_parser('confirm-resolution');confirm.add_argument('identifier');confirm.add_argument('--payload-hash',required=True);confirm.add_argument('--version',required=True,type=int)
    args=parser.parse_args();credential=load_credential(RUNTIME/'user-sessions',args.session)
    headers={'Authorization':'Bearer '+credential['token'],'X-Harness-Client':'local-ui'}
    path='/api/proposals';method='GET';body=None
    if args.command=='propose':
        path='/api/runs/'+uuid_text(args.run_id)+'/proposals';method='POST';body={'request_key':uuid_text(args.request_key)}
    elif args.command!='inbox':
        path=('/api/operations/' if args.command in ('operation','dispatch','resolution','prepare-resolution','confirm-resolution') else '/api/proposals/')+uuid_text(args.identifier)
        if args.command in ('resolution','prepare-resolution','confirm-resolution'):
            path+='/resolution'
            if args.command=='prepare-resolution':method='POST';body={}
            elif args.command=='confirm-resolution':path+='/confirm';method='POST';body={'payload_hash':args.payload_hash,'expected_version':args.version}
        elif args.command=='dispatch':path+='/dispatch';method='POST';body={'interrupt':args.interrupt}
        elif args.command=='prepare':path+='/operation';method='POST';body={}
        elif args.command=='decide':
            path+='/decision';method='POST';body={'decision':args.decision,'payload_hash':args.payload_hash,'expected_version':args.version}
    with httpx.Client(trust_env=False,follow_redirects=False,timeout=10) as client:
        response=client.request(method,'http://127.0.0.1:8005'+path,headers=headers,json=body)
    print(json.dumps({'http_status':response.status_code,**response.json()}))
    return 0 if response.status_code<400 else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'USER_PROPOSAL_COMMAND_FAILED'}))
        raise SystemExit(1) from None
