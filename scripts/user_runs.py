"""Cliente del recorrido de lecturas autenticado; nunca imprime ni pasa tokens por argumentos."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.users import load_credential
from harness.contracts import ContractError
from harness.postgres_config import RUNTIME
from harness.proposals import uuid_text
from harness.web import SCENARIOS


def main():
    import httpx
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True)
    parser.add_argument('--workspace')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('me');sub.add_parser('list')
    start=sub.add_parser('start');start.add_argument('--scenario',choices=sorted(SCENARIOS),default='matching')
    start.add_argument('--pause-at',choices=['none','jira','github','jenkins','failure'],default='none')
    for action in ('status','resume'):sub.add_parser(action).add_argument('run_id')
    args=parser.parse_args();credential=load_credential(RUNTIME/'user-sessions',args.session)
    headers={'Authorization':'Bearer '+credential['token'],'X-Harness-Client':'local-ui'}
    if args.workspace:headers['X-Harness-Workspace']=args.workspace
    path='/api/me' if args.command=='me' else '/api/runs';body=None;method='GET'
    if args.command=='start':method='POST';body={'scenario':args.scenario,'pause_at':args.pause_at}
    elif args.command in ('status','resume'):
        path+='/'+uuid_text(args.run_id)
        if args.command=='resume':path+='/resume';method='POST';body={}
    with httpx.Client(trust_env=False,follow_redirects=False,timeout=10) as client:
        response=client.request(method,'http://127.0.0.1:8005'+path,headers=headers,json=body)
    print(json.dumps({'http_status':response.status_code,**response.json()}))
    return 0 if response.status_code<400 else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'USER_RUN_COMMAND_FAILED'}))
        raise SystemExit(1) from None
