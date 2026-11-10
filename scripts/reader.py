"""Cliente del lector LLM local, con sesión autenticada y ejecución durable."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.postgres_config import RUNTIME
from harness.proposals import uuid_text
from scripts.users import load_credential


def main():
    import httpx
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    start=sub.add_parser('start');start.add_argument('--query',required=True);start.add_argument('--pause-after-search',action='store_true')
    for name in ('status','resume'):sub.add_parser(name).add_argument('run_id')
    args=parser.parse_args();credential=load_credential(RUNTIME/'user-sessions',args.session)
    path='/api/knowledge/reader/runs';method='POST';body={}
    if args.command=='start':body={'query':args.query,'pause_after_search':args.pause_after_search}
    if args.command!='start':path+='/'+uuid_text(args.run_id)
    if args.command=='status':method='GET';body=None
    if args.command=='resume':path+='/resume'
    with httpx.Client(timeout=10,trust_env=False,follow_redirects=False) as client:
        response=client.request(method,'http://127.0.0.1:8005'+path,
            headers={'Authorization':'Bearer '+credential['token'],'X-Harness-Client':'local-ui'},json=body)
    print(json.dumps({'http_status':response.status_code,**response.json()},ensure_ascii=False))
    return 0 if response.status_code<400 else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception:
        print(json.dumps({'error':'READER_COMMAND_FAILED'}));raise SystemExit(1) from None
