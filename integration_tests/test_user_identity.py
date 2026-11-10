"""Credencial de usuario, scope y revocación con PostgreSQL real y HTTP local."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from psycopg import sql
from fastapi.testclient import TestClient
from harness.contracts import ContractError
from harness.postgres_store import PostgresRunStore
from harness.postgres_config import local_dsn
from harness.user_identity import UserIdentityStore
from harness.user_identity_api import create_app
from scripts.users import save_credential


class UserIdentityTests(unittest.TestCase):
    def setUp(self):
        self.schema='harness_auth_it_'+uuid4().hex
        self.runs=PostgresRunStore(local_dsn(),schema=self.schema)
        self.addCleanup(self.cleanup)
        self.store=UserIdentityStore(self.runs);self.store.bootstrap()
        self.credential=self.store.issue('reader-demo','workspace-alpha')
        self.header='Bearer '+self.credential['token']

    def cleanup(self):
        self.assertRegex(self.schema,r'^harness_auth_it_[0-9a-f]{32}$')
        with self.runs._database() as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def code(self,code,action):
        with self.assertRaises(ContractError) as error:action()
        self.assertEqual(error.exception.code,code)

    def test_persisted_session_is_hash_only_and_reopens_without_new_permissions(self):
        with self.runs._database() as db:
            row=db.execute('SELECT * FROM user_sessions').fetchone()
            self.assertEqual(row['token_hash'],sha256(self.credential['token'].encode()).hexdigest())
            self.assertNotIn(self.credential['token'],str(row))
            self.assertEqual(db.execute('SELECT version FROM schema_version').fetchone()['version'],3)
        reopened=UserIdentityStore(PostgresRunStore(local_dsn(),schema=self.schema))
        identity=reopened.authenticate(self.header)
        self.assertEqual((identity.actor_id,identity.workspace_id),('reader-demo','workspace-alpha'))
        self.assertEqual(identity.roles,{'reader','operator'})

    def test_scope_missing_token_service_jwt_and_forged_token_are_rejected(self):
        self.code('FORBIDDEN',lambda:self.store.authenticate(self.header,'workspace-beta'))
        self.code('FORBIDDEN',lambda:self.store.issue('reader-beta','workspace-alpha'))
        for token in (None,'','Bearer eyJhbGciOiJFZERTQSJ9.fixture.signature','Bearer hsu_'+'A'*43,'Bearer '+self.credential['token']+'extra'):
            self.code('UNAUTHENTICATED',lambda:self.store.authenticate(token))

    def test_membership_revocation_and_role_changes_are_read_on_each_request(self):
        self.store.set_membership('reader-demo','workspace-alpha',frozenset({'reader'}))
        self.assertEqual(self.store.authenticate(self.header).roles,{'reader'})
        self.store.set_membership('reader-demo','workspace-alpha',frozenset(),enabled=False)
        self.store.bootstrap()  # Nunca deshacer una revocación por volver a arrancar.
        self.code('UNAUTHENTICATED',lambda:self.store.authenticate(self.header))
        self.code('FORBIDDEN',lambda:self.store.issue('reader-demo','workspace-alpha'))

    def test_disabled_account_and_expired_session_fail_closed(self):
        expired='hsu_'+'B'*43
        with self.runs._database() as db:
            db.execute('''INSERT INTO user_sessions(session_id,token_hash,actor_id,workspace_id,created_at,expires_at)
                VALUES (%s,%s,'reader-demo','workspace-alpha',clock_timestamp()-interval '2 hours',clock_timestamp()-interval '1 hour')''',
                (str(uuid4()),sha256(expired.encode()).hexdigest()))
        self.code('UNAUTHENTICATED',lambda:self.store.authenticate('Bearer '+expired))
        with self.runs._database() as db:db.execute("UPDATE user_accounts SET enabled=false WHERE actor_id='reader-demo'")
        self.store.bootstrap()
        self.code('UNAUTHENTICATED',lambda:self.store.authenticate(self.header))

    def test_revocation_persists_and_does_not_revoke_other_users(self):
        second=self.store.issue('approver-demo','workspace-alpha')
        self.store.revoke(self.credential['session_id']);self.store.revoke(self.credential['session_id'])
        reopened=UserIdentityStore(self.runs)
        self.code('UNAUTHENTICATED',lambda:reopened.authenticate(self.header))
        self.assertEqual(reopened.authenticate('Bearer '+second['token']).roles,{'approver'})

    def test_http_derives_identity_and_does_not_expose_or_accept_business_routes(self):
        with TestClient(create_app(store=self.store),base_url='http://127.0.0.1') as client:
            missing=client.get('/api/me')
            self.assertEqual(missing.status_code,401)
            self.assertEqual(missing.headers['www-authenticate'],'Bearer')
            response=client.get('/api/me',headers={'Authorization':self.header,'X-Actor-ID':'approver-demo','X-Roles':'approver'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['identity']['actor_id'],'reader-demo')
            self.assertEqual(response.json()['identity']['roles'],['operator','reader'])
            self.assertFalse(response.json()['business_routes_connected'])
            self.assertNotIn(self.credential['token'],response.text)
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(client.get('/api/me',headers={'Authorization':self.header,'X-Harness-Workspace':'workspace-beta'}).status_code,403)
            self.assertEqual(client.get('/api/me?actor=approver-demo',headers={'Authorization':self.header}).status_code,422)
            self.assertEqual(client.get('/api/runs',headers={'Authorization':self.header}).status_code,404)
            self.assertEqual(client.post('/api/sessions',json={'actor':'approver-demo'}).status_code,404)
            self.store.revoke(self.credential['session_id'])
            self.assertEqual(client.get('/api/me',headers={'Authorization':self.header}).status_code,401)

    def test_private_credential_file_cannot_be_overwritten_or_created_through_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)
            path=save_credential(target,self.credential)
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            self.assertEqual(json.loads(path.read_text())['token'],self.credential['token'])
            with self.assertRaises(FileExistsError):save_credential(target,self.credential)
            path.unlink();path.symlink_to(target/'victim')
            with self.assertRaises(FileExistsError):save_credential(target,self.credential)
            self.assertFalse((target/'victim').exists())
