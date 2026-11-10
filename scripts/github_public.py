"""Permitir y comprobar lectura pública de un PR; sin token ni modificaciones en GitHub."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.contracts import ContractError,pull_request_arguments
from harness.github_public import fetch
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.user_identity import UserIdentityStore,name


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['allow','check']);parser.add_argument('--repository',required=True)
    parser.add_argument('--pull-number',type=int,default=1);parser.add_argument('--actor',default='reader-demo');parser.add_argument('--workspace',default='workspace-alpha')
    args=parser.parse_args();name(args.actor);name(args.workspace)
    arguments=pull_request_arguments({'repository':args.repository.lower(),'pull_number':args.pull_number})
    runs=PostgresRunStore(local_dsn());directory=UserIdentityStore(runs)
    if args.command=='allow':
        with runs._database() as db:
            row=db.execute('''UPDATE user_memberships SET repositories=(SELECT ARRAY(SELECT DISTINCT unnest(repositories||%s::text[])))
                WHERE actor_id=%s AND workspace_id=%s RETURNING actor_id''',([arguments['repository']],args.actor,args.workspace)).fetchone()
            if not row:raise ContractError('NOT_FOUND')
        print('Repositorio permitido localmente; otros permisos conservados. GitHub no fue modificado.')
    else:
        data=fetch(directory.resolve(args.actor,args.workspace),arguments)
        print(json.dumps({'read_ok':True,'repository':data['repository'],'pull_number':data['pull_number']}))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'GITHUB_CHECK_FAILED'}));raise SystemExit(1) from None
