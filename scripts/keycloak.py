"""Preparar Keycloak local: secretos en data/runtime (ignorado por Git) y usuarios de prueba por la API admin."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.postgres_config import RUNTIME

BASE = 'http://127.0.0.1:8080'
REALM = 'harness-lab'
USERS = ('reader-demo', 'approver-demo')
ENV_FILE = RUNTIME / 'keycloak.env'
USERS_FILE = RUNTIME / 'keycloak-users.json'


def write_private(path, text):
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)


def init():
    if ENV_FILE.exists():
        print('Ya existe', ENV_FILE)
        return
    write_private(ENV_FILE, f'KC_BOOTSTRAP_ADMIN_USERNAME=kc-admin\nKC_BOOTSTRAP_ADMIN_PASSWORD={secrets.token_urlsafe(24)}\n')
    print('Creado', ENV_FILE, '(contiene la clave del administrador; no se versiona)')


def admin_client():
    values = dict(line.split('=', 1) for line in ENV_FILE.read_text().split())
    client = httpx.Client(base_url=BASE, timeout=10)
    response = client.post('/realms/master/protocol/openid-connect/token', data={'grant_type': 'password', 'client_id': 'admin-cli',
        'username': values['KC_BOOTSTRAP_ADMIN_USERNAME'], 'password': values['KC_BOOTSTRAP_ADMIN_PASSWORD']})
    response.raise_for_status()
    client.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
    return client


def setup():
    if USERS_FILE.exists():
        print('Ya existen', USERS_FILE)
        return
    client = admin_client()
    credentials = {}
    for name in USERS:
        password = secrets.token_urlsafe(18)
        profile = {'username': name, 'enabled': True, 'emailVerified': True, 'firstName': name, 'lastName': 'Demo',
            'email': name + '@example.invalid'}  # Keycloak exige perfil completo antes de permitir el login
        created = client.post(f'/admin/realms/{REALM}/users', json=profile)
        if created.status_code == 409:
            identifier = client.get(f'/admin/realms/{REALM}/users', params={'username': name, 'exact': 'true'}).json()[0]['id']
            client.put(f'/admin/realms/{REALM}/users/{identifier}', json=profile).raise_for_status()
        else:
            created.raise_for_status()
            identifier = created.headers['Location'].rsplit('/', 1)[1]
        client.put(f'/admin/realms/{REALM}/users/{identifier}/reset-password',
            json={'type': 'password', 'value': password, 'temporary': False}).raise_for_status()
        credentials[name] = password
    # Cliente solo para pruebas automáticas: la consola no habilita el grant de contraseña.
    scope = next(item['id'] for item in client.get(f'/admin/realms/{REALM}/client-scopes').json() if item['name'] == 'harness-api')
    created = client.post(f'/admin/realms/{REALM}/clients', json={'clientId': 'harness-cli-test', 'publicClient': True,
        'standardFlowEnabled': False, 'directAccessGrantsEnabled': True, 'defaultClientScopes': ['harness-api']})
    if created.status_code not in (201, 409):
        created.raise_for_status()
    write_private(USERS_FILE, json.dumps(credentials))
    print('Usuarios creados; claves en', USERS_FILE)


def token(user):
    password = json.loads(USERS_FILE.read_text())[user]
    response = httpx.post(f'{BASE}/realms/{REALM}/protocol/openid-connect/token', data={'grant_type': 'password',
        'client_id': 'harness-cli-test', 'username': user, 'password': password}, timeout=10)
    response.raise_for_status()
    return response.json()['access_token']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    sub.add_parser('setup')
    sub.add_parser('token').add_argument('user', choices=USERS)
    args = parser.parse_args()
    if args.command == 'init':
        init()
    elif args.command == 'setup':
        setup()
    else:
        print(token(args.user))


if __name__ == '__main__':
    main()
