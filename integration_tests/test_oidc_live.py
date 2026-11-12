"""Verificación contra un Keycloak real. Requiere `stack` + perfil oidc y `scripts/keycloak.py setup`."""
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from harness.contracts import ContractError
from harness.oidc import OidcAuthenticator
from tests.test_oidc import Directory

BASE = 'http://127.0.0.1:8080/realms/harness-lab'


@unittest.skipUnless(os.environ.get('HARNESS_TEST_LIVE_OIDC') == '1', 'defina HARNESS_TEST_LIVE_OIDC=1 con Keycloak en marcha')
class LiveOidcTests(unittest.TestCase):
    def setUp(self):
        self.auth = OidcAuthenticator(Directory(), issuer=BASE, audience='harness-api', jwks_url=BASE + '/protocol/openid-connect/certs')

    def test_real_token_is_accepted_and_tampering_rejected(self):
        import keycloak
        token = keycloak.token('reader-demo')
        self.assertEqual(self.auth.authenticate('Bearer ' + token, 'workspace-alpha').actor_id, 'reader-demo')
        with self.assertRaises(ContractError) as caught:
            self.auth.authenticate('Bearer ' + token[:-4] + 'AAAA', 'workspace-alpha')
        self.assertEqual(caught.exception.code, 'UNAUTHENTICATED')

    def test_wrong_audience_is_rejected(self):
        import keycloak
        other = OidcAuthenticator(Directory(), issuer=BASE, audience='otra-api', jwks_url=BASE + '/protocol/openid-connect/certs')
        with self.assertRaises(ContractError):
            other.authenticate('Bearer ' + keycloak.token('reader-demo'), 'workspace-alpha')


if __name__ == '__main__':
    unittest.main()
