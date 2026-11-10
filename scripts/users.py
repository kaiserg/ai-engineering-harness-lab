"""Administración local de fixtures y credenciales M3d1; no es un endpoint de emisión público."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.contracts import ContractError
from harness.postgres_config import local_dsn,RUNTIME
from harness.postgres_store import PostgresRunStore
from harness.proposals import uuid_text
from harness.user_identity import UserIdentityStore,ROLES


def save_credential(directory,credential):
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=directory/(uuid_text(credential['session_id'])+'.json')
    descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as handle:
        json.dump(credential,handle);handle.flush();os.fsync(handle.fileno())
    return path


def load_credential(directory,identifier):
    path=directory/(uuid_text(identifier)+'.json')
    if path.is_symlink() or path.stat().st_mode&0o077:raise ContractError('UNSAFE_CREDENTIAL_FILE')
    return json.loads(path.read_text())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('bootstrap')
    issue=sub.add_parser('issue');issue.add_argument('--actor',required=True);issue.add_argument('--workspace',required=True)
    revoke=sub.add_parser('revoke');revoke.add_argument('session_id')
    who=sub.add_parser('whoami');who.add_argument('session_id');who.add_argument('--workspace')
    grant=sub.add_parser('set-membership');grant.add_argument('--actor',required=True);grant.add_argument('--workspace',required=True)
    grant.add_argument('--roles',nargs='*',choices=sorted(ROLES),default=[]);grant.add_argument('--disabled',action='store_true')
    args=parser.parse_args();directory=RUNTIME/'user-sessions'
    if args.command=='whoami':
        import httpx
        credential=load_credential(directory,args.session_id)
        headers={'Authorization':'Bearer '+credential['token']}
        if args.workspace is not None:headers['X-Harness-Workspace']=args.workspace
        # Destino fijo, sin proxies ni redirects: no enviar credenciales a otro host.
        with httpx.Client(trust_env=False,follow_redirects=False,timeout=5) as client:
            response=client.get('http://127.0.0.1:8004/api/me',headers=headers)
        print(json.dumps({'status':response.status_code,**response.json()}))
        return 0 if response.status_code==200 else 1
    store=UserIdentityStore(PostgresRunStore(local_dsn()))
    if args.command=='bootstrap':result=store.bootstrap()
    elif args.command=='issue':
        credential=store.issue(args.actor,args.workspace)
        try:path=save_credential(directory,credential)
        except BaseException:
            store.revoke(credential['session_id']);raise
        result={key:value for key,value in credential.items() if key!='token'}|{'credential_file':str(path.relative_to(RUNTIME.parent.parent))}
    elif args.command=='revoke':store.revoke(args.session_id);result={'revoked':True}
    else:
        store.set_membership(args.actor,args.workspace,frozenset(args.roles),enabled=not args.disabled)
        result={'membership_updated':True}
    print(json.dumps(result));return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'IDENTITY_COMMAND_FAILED'}))
        raise SystemExit(1) from None
