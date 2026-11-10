"""Consola local M5a autenticada; no expone una API ni envía el bearer a Chroma."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.users import load_credential
from harness.contracts import ContractError
from harness.knowledge import Knowledge,MAX_BYTES
from harness.knowledge_queue import KnowledgeQueue
from harness.postgres_config import RUNTIME,local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    upload=sub.add_parser('upload');upload.add_argument('path',type=Path)
    upload.add_argument('--reader',action='append',default=[])
    sub.add_parser('list');sub.add_parser('rebuild')
    sub.add_parser('enqueue').add_argument('--key',required=True)
    sub.add_parser('job').add_argument('job_id')
    sub.add_parser('retire').add_argument('document_id')
    search=sub.add_parser('search');search.add_argument('query');search.add_argument('--limit',type=int,default=3)
    args=parser.parse_args()
    credential=load_credential(RUNTIME/'user-sessions',args.session)
    directory=UserIdentityStore(PostgresRunStore(local_dsn()))
    authenticate=lambda:directory.authenticate('Bearer '+credential['token'])
    authenticate()  # Rechaza antes de crear tablas de conocimiento.
    knowledge=Knowledge(PostgresRunStore(local_dsn(),schema='harness_users_lab'),authenticate)
    if args.command=='upload':
        if args.path.suffix not in ('.txt','.md'):raise ContractError('INVALID_DOCUMENT')
        with args.path.open('rb') as handle:raw=handle.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ContractError('INVALID_DOCUMENT')
        result=knowledge.register(args.path.name,raw.decode('utf-8'),args.reader)
    elif args.command=='enqueue':result=KnowledgeQueue(knowledge).enqueue(args.key)
    elif args.command=='job':result=KnowledgeQueue(knowledge).get(args.job_id)
    elif args.command=='list':result={'documents':knowledge.documents()}
    elif args.command=='rebuild':result=knowledge.rebuild()
    elif args.command=='retire':result=knowledge.retire(args.document_id)
    else:result=knowledge.search(args.query,args.limit)
    print(json.dumps(result,ensure_ascii=False,default=lambda value:value.isoformat()))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'KNOWLEDGE_COMMAND_FAILED'}))
        raise SystemExit(1) from None
