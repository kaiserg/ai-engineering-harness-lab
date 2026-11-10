"""Configurar Jira Cloud sin secretos en argumentos, historial de shell o Git."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from harness.contracts import ContractError
from harness.jira_cloud import DIRECTORY,site_origin,identifier,load,current,fetch,binding_for
from harness.user_identity import UserIdentityStore,name
from harness.postgres_store import PostgresRunStore
from harness.postgres_config import local_dsn


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    config=sub.add_parser('configure');config.add_argument('--site',required=True);config.add_argument('--project',required=True)
    config.add_argument('--actor',default='reader-demo');config.add_argument('--workspace',default='workspace-alpha')
    config.add_argument('--unscoped',action='store_true',help='Token sin scopes: usar URL del sitio en vez del gateway.')
    grant=sub.add_parser('grant-local')
    verify=sub.add_parser('check');verify.add_argument('--issue',required=True)
    args=parser.parse_args()
    if args.command=='configure':
        import re
        site=site_origin(args.site);name(args.actor);name(args.workspace)
        if not re.fullmatch(r'[A-Z][A-Z0-9]{1,9}',args.project):raise ContractError('INVALID_ARGUMENT')
        cloud_id=None
        if not args.unscoped:
            with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
                response=client.get(site+'/_edge/tenant_info');response.raise_for_status();cloud_id=identifier(response.json()['cloudId'])
        email=input('Email de la cuenta Atlassian: ').strip();token=getpass.getpass('API token (no se muestra): ').strip()
        if not email or not token:raise ContractError('JIRA_CONFIGURATION')
        data={'site':site,'cloud_id':cloud_id,'email':email,'token':token,'projects':[args.project],'actor':args.actor,'workspace':args.workspace}
        binding=binding_for(data);DIRECTORY.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=DIRECTORY/(binding+'.json');descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as handle:json.dump(data,handle)
        load(binding)
        temp=DIRECTORY/('current-'+binding);temp.write_text(binding);os.replace(temp,DIRECTORY/'current')
        print('Conexión privada configurada. Ejecutá grant-local y check --issue CLAVE_DEL_TICKET.')
    else:
        binding=current();config=load(binding);runs=PostgresRunStore(local_dsn());directory=UserIdentityStore(runs)
        if args.command=='grant-local':
            with runs._database() as db:
                row=db.execute('''UPDATE user_memberships SET projects=(SELECT ARRAY(SELECT DISTINCT unnest(projects||%s::text[])))
                    WHERE actor_id=%s AND workspace_id=%s RETURNING actor_id''',(config['projects'],config['actor'],config['workspace'])).fetchone()
                if not row:raise ContractError('NOT_FOUND')
            print('El actor local tiene acceso a los proyectos configurados; roles y otros recursos conservados.')
        else:
            identity=directory.resolve(config['actor'],config['workspace'])
            fetch(binding,identity,args.issue,access_only=True);print('Acceso de lectura confirmado. No se imprimió el contenido del ticket.')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'JIRA_CONFIGURATION_FAILED'}));raise SystemExit(1) from None
