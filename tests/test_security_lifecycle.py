"""Publicación atómica, solapamiento y recuperación sin cambiar el core."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography import x509
from fastapi.testclient import TestClient

from harness.composition import demo_identity
from harness.identity_server import create_app
from harness.security_lifecycle import (activate, ensure_generation, prepare_change,
    status, operation_lock, atomic_json)
from harness.service_identity import issue_token, verify_token
from harness.service_tls import material_directory
from harness.signing_keys import parse_public_keys, public_jwk, select_key
from scripts.security import perform


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.first = ensure_generation(self.directory)

    def test_initial_generation_is_private_and_restart_preserves_it(self):
        self.assertEqual(ensure_generation(self.directory), self.first.resolve())
        self.assertTrue((self.directory / 'security-current').is_symlink())
        for path in self.first.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with operation_lock(self.directory):
            with self.assertRaises(BlockingIOError):
                with operation_lock(self.directory):
                    pass

    def test_renew_changes_leaf_cert_and_key_but_keeps_ca_issuer_and_client(self):
        new = prepare_change('renew-certificates', self.directory)
        self.assertEqual(material_directory(self.directory), self.first.resolve())
        for name in ('tls-ca.pem','tls-ca-key.pem','mcp-client-secret','issuer/mcp-worker-private.pem'):
            self.assertEqual((new / name).read_bytes(), (self.first / name).read_bytes())
        for service in ('jira','identity'):
            before = x509.load_pem_x509_certificate((self.first / (service + '-cert.pem')).read_bytes())
            after = x509.load_pem_x509_certificate((new / (service + '-cert.pem')).read_bytes())
            self.assertNotEqual(before.serial_number, after.serial_number)
            self.assertNotEqual((new / (service + '-key.pem')).read_bytes(), (self.first / (service + '-key.pem')).read_bytes())
            ca = x509.load_pem_x509_certificate((new / 'tls-ca.pem').read_bytes())
            after.verify_directly_issued_by(ca)
        activate(new, self.directory)
        self.assertEqual(material_directory(self.directory), new.resolve())

    def test_bad_staging_leaves_current_unchanged(self):
        with patch('harness.security_lifecycle.validate', side_effect=ValueError('invalid')):
            with self.assertRaises(ValueError):
                prepare_change('renew-certificates', self.directory)
        self.assertEqual(material_directory(self.directory), self.first.resolve())
        with self.assertRaises(ValueError):
            activate(self.directory, self.directory)

    def test_rotation_keeps_previous_public_key_until_deadline_and_rejects_it_after(self):
        now = int(time.time())
        old_private = (self.first / 'issuer/mcp-worker-private.pem').read_bytes()
        old_jwk = public_jwk(old_private)
        old_token = issue_token(old_private, demo_identity(), kid=old_jwk['kid'])
        new = prepare_change('rotate-signing-key', self.directory, now=now)
        active = public_jwk((new / 'issuer/mcp-worker-private.pem').read_bytes())
        previous = json.loads((new / 'issuer-previous.json').read_text())
        self.assertNotEqual(active['kid'], old_jwk['kid'])
        self.assertNotIn('d', previous[0])
        keys = parse_public_keys([active, *previous])
        verify_token(old_token, select_key(keys, old_token, now=now+1))
        with self.assertRaises(ValueError):
            select_key(keys, old_token, now=now+90)
        activate(new, self.directory)
        with self.assertRaises(ValueError):
            prepare_change('rotate-signing-key', self.directory, now=now+1)
        with self.assertRaises(ValueError):
            prepare_change('retire-signing-key', self.directory, now=now+1)
        retired = prepare_change('retire-signing-key', self.directory, now=now+91)
        self.assertEqual(json.loads((retired / 'issuer-previous.json').read_text()), [])
        self.assertEqual((retired / 'issuer/mcp-worker-private.pem').read_bytes(), (new / 'issuer/mcp-worker-private.pem').read_bytes())

    def test_issuer_publishes_both_keys_then_filters_previous_without_restart(self):
        now = int(time.time())
        new = prepare_change('rotate-signing-key', self.directory, now=now)
        # Representar el tmpfs del emisor: clave activa más públicas anteriores.
        (new / 'issuer-signing.pem').write_bytes((new / 'issuer/mcp-worker-private.pem').read_bytes())
        with TestClient(create_app(new), base_url='https://localhost') as client:
            self.assertEqual(len(client.get('/jwks').json()['keys']), 2)
            with patch('time.time', return_value=now+91):
                self.assertEqual(len(client.get('/jwks').json()['keys']), 1)

    def test_duplicate_kid_private_jwk_or_invalid_deadline_fail_closed(self):
        jwk = public_jwk((self.first / 'issuer/mcp-worker-private.pem').read_bytes())
        for keys in ([jwk,jwk], [jwk | {'d':'private'}], [jwk | {'lab_retire_at':'tomorrow'}]):
            with self.assertRaises((ValueError, jwt.PyJWTError)):
                parse_public_keys(keys)

    def test_recover_after_activation_reuses_same_generation_and_key(self):
        calls = []
        def fail_on_up(*args):
            calls.append(args)
            if args[0] == 'up':
                raise OSError('simulated process interruption')
        with self.assertRaises(OSError):
            perform('rotate-signing-key', self.directory, fail_on_up)
        before = status(self.directory)
        self.assertTrue((self.directory / 'security-operation.json').exists())
        with self.assertRaises(ValueError):
            perform('renew-certificates', self.directory, lambda *args: None)
        perform('recover', self.directory, lambda *args: calls.append(args))
        after = status(self.directory)
        self.assertEqual(before, after)
        self.assertFalse((self.directory / 'security-operation.json').exists())
        self.assertTrue(all('--no-deps' in args for args in calls if args[0] == 'up'))

    def test_recover_before_publication_completes_original_action(self):
        atomic_json(self.directory / 'security-operation.json', {'action':'renew-certificates','phase':'stopping'})
        before = status(self.directory)
        perform('recover', self.directory, lambda *args: None)
        self.assertNotEqual(status(self.directory)['generation'], before['generation'])
        self.assertFalse((self.directory / 'security-operation.json').exists())

    def test_recover_prepared_generation_does_not_generate_another_key(self):
        new = prepare_change('rotate-signing-key', self.directory)
        atomic_json(self.directory / 'security-operation.json',
            {'action':'rotate-signing-key','phase':'prepared','target':new.name})
        perform('recover', self.directory, lambda *args: None)
        self.assertEqual(material_directory(self.directory), new.resolve())
