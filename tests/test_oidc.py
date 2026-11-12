import time
import unittest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from harness.contracts import ContractError, Identity
from harness.oidc import OidcAuthenticator

ISSUER = 'http://127.0.0.1:8080/realms/harness-lab'
AUDIENCE = 'harness-api'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Signing:
    def __init__(self, key=KEY, fail=None):
        self.key, self.fail = key, fail

    def get_signing_key_from_jwt(self, token):
        if self.fail:
            raise self.fail
        return type('Key', (), {'key': self.key.public_key()})()


class Directory:
    def __init__(self):
        self.enabled = {('reader-demo', 'workspace-alpha')}

    def resolve(self, actor, workspace):
        if (actor, workspace) not in self.enabled:
            raise ContractError('FORBIDDEN')
        return Identity(actor, workspace, frozenset({'reader'}), frozenset())

    def authenticate(self, authorization, requested_workspace=None):
        return 'local'

    def revoke(self, identifier):
        return 'revoked'


def token(key=KEY, headers=None, algorithm='RS256', **overrides):
    claims = {'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'u-1', 'typ': 'Bearer', 'preferred_username': 'reader-demo',
        'exp': int(time.time()) + 300, **overrides}
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


class OidcTests(unittest.TestCase):
    def setUp(self):
        self.auth = OidcAuthenticator(Directory(), issuer=ISSUER, audience=AUDIENCE, jwk_client=Signing())

    def call(self, jwt_token, workspace='workspace-alpha'):
        return self.auth.authenticate('Bearer ' + jwt_token, workspace)

    def rejects(self, code, jwt_token, workspace='workspace-alpha', auth=None):
        with self.assertRaises(ContractError) as caught:
            (auth or self.auth).authenticate('Bearer ' + jwt_token, workspace)
        self.assertEqual(caught.exception.code, code)

    def test_valid_token_resolves_current_permissions_from_directory(self):
        identity = self.call(token())
        self.assertEqual((identity.actor_id, identity.workspace_id, identity.roles), ('reader-demo', 'workspace-alpha', frozenset({'reader'})))

    def test_roles_in_token_are_ignored(self):
        identity = self.call(token(roles=['approver'], realm_access={'roles': ['approver']}))
        self.assertEqual(identity.roles, frozenset({'reader'}))

    def test_local_tokens_go_to_directory_only(self):
        self.assertEqual(self.auth.authenticate('Bearer hsu_' + 'A' * 43, 'workspace-alpha'), 'local')
        self.assertEqual(self.auth.revoke('x'), 'revoked')

    def test_rejects_bad_claims(self):
        self.rejects('UNAUTHENTICATED', token(exp=int(time.time()) - 10))
        self.rejects('UNAUTHENTICATED', token(iss='http://evil/realms/x'))
        self.rejects('UNAUTHENTICATED', token(aud='account'))
        self.rejects('UNAUTHENTICATED', token(aud=None))
        self.rejects('UNAUTHENTICATED', token(exp=None))
        self.rejects('UNAUTHENTICATED', token(sub=None))
        self.rejects('UNAUTHENTICATED', token(typ='ID'))
        self.rejects('UNAUTHENTICATED', token(typ='Refresh'))
        self.rejects('UNAUTHENTICATED', token(typ=None))
        self.rejects('UNAUTHENTICATED', token(preferred_username=None))
        self.rejects('UNAUTHENTICATED', token(preferred_username='Admin; DROP'))

    def test_rejects_wrong_signature_and_algorithms(self):
        self.rejects('UNAUTHENTICATED', token(key=OTHER))
        none = jwt.encode({'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'u', 'exp': int(time.time()) + 300, 'typ': 'Bearer',
            'preferred_username': 'reader-demo'}, key=None, algorithm='none')
        self.rejects('UNAUTHENTICATED', none)
        # Confusión de algoritmo: un HS256 válido con otro secreto debe rechazarse por algoritmo.
        hs = jwt.encode({'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'u', 'exp': int(time.time()) + 300, 'typ': 'Bearer',
            'preferred_username': 'reader-demo'}, 'secreto', algorithm='HS256')
        self.rejects('UNAUTHENTICATED', hs)

    def test_unknown_key_id_and_garbage(self):
        auth = OidcAuthenticator(Directory(), issuer=ISSUER, audience=AUDIENCE, jwk_client=Signing(fail=jwt.PyJWKClientError('kid')))
        self.rejects('UNAUTHENTICATED', token(), auth=auth)
        self.rejects('UNAUTHENTICATED', 'a.b.c')
        self.rejects('UNAUTHENTICATED', 'no-dots')

    def test_jwks_down_is_unavailable_not_unauthenticated(self):
        auth = OidcAuthenticator(Directory(), issuer=ISSUER, audience=AUDIENCE, jwk_client=Signing(fail=jwt.PyJWKClientConnectionError('down')))
        self.rejects('IDENTITY_UNAVAILABLE', token(), auth=auth)

    def test_workspace_required_and_membership_enforced(self):
        self.rejects('FORBIDDEN', token(), workspace=None)
        self.rejects('FORBIDDEN', token(), workspace='workspace-beta')
        self.auth.directory.enabled.clear()
        self.rejects('FORBIDDEN', token())

    def test_requires_explicit_issuer_and_audience(self):
        with self.assertRaises(ValueError):
            OidcAuthenticator(Directory(), issuer='', audience=AUDIENCE, jwk_client=Signing())
        with self.assertRaises(ValueError):
            OidcAuthenticator(Directory(), issuer=ISSUER, audience=None, jwk_client=Signing())

    def test_missing_or_malformed_header(self):
        for header in (None, 'Basic abc', 'Bearer '):
            with self.assertRaises(ContractError):
                self.auth.authenticate(header, 'workspace-alpha')


if __name__ == '__main__':
    unittest.main()
