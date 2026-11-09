"""Generaciones inmutables: preparar, validar y publicar un puntero atómico."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import time
import ipaddress
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from .postgres_config import RUNTIME
from .service_identity import prepare_keys
from .service_tls import prepare_tls, material_directory, private_write
from .signing_keys import public_jwk, parse_public_keys

FILES = ('tls-ca.pem','tls-ca-key.pem','jira-cert.pem','jira-key.pem',
         'identity-cert.pem','identity-key.pem','mcp-client-secret')
ACTIONS = ('renew-certificates','rotate-signing-key','retire-signing-key')


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.' + uuid4().hex)
    private_write(temporary, json.dumps(value, sort_keys=True).encode())
    with temporary.open('rb') as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


@contextmanager
def operation_lock(directory=RUNTIME):
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(directory / 'security.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def activate(generation, directory=RUNTIME):
    generation = generation.resolve()
    parent = (directory / 'security-generations').resolve()
    if generation.parent != parent or not generation.is_dir():
        raise ValueError('Invalid generation')
    temporary = directory / ('security-pointer-' + uuid4().hex)
    temporary.symlink_to(Path('security-generations') / generation.name)
    os.replace(temporary, directory / 'security-current')
    sync_directory(directory)


def _copy(source, directory):
    generation = directory / 'security-generations' / uuid4().hex
    generation.mkdir(parents=True, mode=0o700)
    for name in FILES:
        private_write(generation / name, (source / name).read_bytes())
    (generation / 'issuer').mkdir(mode=0o700)
    for name in ('mcp-worker-private.pem','mcp-worker-public.pem'):
        private_write(generation / 'issuer' / name, (source / 'issuer' / name).read_bytes())
    metadata = source / 'issuer-previous.json'
    private_write(generation / 'issuer-previous.json', metadata.read_bytes() if metadata.exists() else b'[]')
    return generation


def validate(generation):
    prepare_tls(generation)
    active = public_jwk((generation / 'issuer/mcp-worker-private.pem').read_bytes())
    previous = json.loads((generation / 'issuer-previous.json').read_text())
    parse_public_keys([active, *previous])
    # Todo el bundle está escrito antes de cambiar el puntero.
    for path in generation.rglob('*'):
        if path.is_file():
            path.chmod(0o600)
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
    sync_directory(generation / 'issuer')
    sync_directory(generation)
    sync_directory(generation.parent)


def ensure_generation(directory=RUNTIME):
    if (directory / 'security-current').is_symlink():
        current = material_directory(directory)
        if current.parent != (directory / 'security-generations').resolve():
            raise ValueError('Invalid active generation')
        return current
    prepare_tls(directory)
    generation = _copy(directory, directory)
    validate(generation)
    activate(generation, directory)
    return generation


def renew_certificates(generation, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    ca = x509.load_pem_x509_certificate((generation / 'tls-ca.pem').read_bytes())
    ca_key = serialization.load_pem_private_key((generation / 'tls-ca-key.pem').read_bytes(), password=None)
    ca.verify_directly_issued_by(ca)
    if ca.not_valid_after_utc < now + timedelta(days=30):
        raise ValueError('CA renewal requires a separate trust transition')
    for service, hostname in (('jira','jira-mcp'), ('identity','identity')):
        old = x509.load_pem_x509_certificate((generation / (service + '-cert.pem')).read_bytes())
        key = ec.generate_private_key(ec.SECP256R1())
        certificate = (x509.CertificateBuilder().subject_name(old.subject).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.KeyUsage(True,False,False,False,False,False,False,False,False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname), x509.DNSName('localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(ca_key, hashes.SHA256()))
        (generation / (service + '-key.pem')).write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (generation / (service + '-cert.pem')).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def prepare_change(action, directory=RUNTIME, *, now=None):
    if action not in ACTIONS:
        raise ValueError('Invalid action')
    now = int(time.time()) if now is None else now
    source = ensure_generation(directory)
    previous = json.loads((source / 'issuer-previous.json').read_text())
    if action == 'rotate-signing-key' and any(item['lab_retire_at'] > now for item in previous):
        raise ValueError('Wait for previous signing key retirement')
    if action == 'retire-signing-key' and (not previous or any(item['lab_retire_at'] > now for item in previous)):
        raise ValueError('No expired previous key to retire')
    generation = _copy(source, directory)
    if action == 'renew-certificates':
        renew_certificates(generation)
    elif action == 'rotate-signing-key':
        old = public_jwk((source / 'issuer/mcp-worker-private.pem').read_bytes())
        # Se invoca con el emisor detenido: su último token vence como máximo en 60 s.
        old['lab_retire_at'] = now + 90
        (generation / 'issuer-previous.json').write_text(json.dumps([old]))
        for path in (generation / 'issuer').iterdir():
            path.unlink()
        prepare_keys(generation / 'issuer')
    else:
        (generation / 'issuer-previous.json').write_text('[]')
    validate(generation)
    return generation


def status(directory=RUNTIME):
    current = ensure_generation(directory)
    now = datetime.now(timezone.utc)
    result = {'generation': current.name, 'signing_kid': public_jwk(
        (current / 'issuer/mcp-worker-private.pem').read_bytes())['kid'],
        'previous': json.loads((current / 'issuer-previous.json').read_text()), 'certificates': {}}
    # Solo metadatos públicos en la salida; nunca PEM, secretos ni JWT.
    result['previous'] = [{'kid': item['kid'], 'retire_at': item['lab_retire_at']} for item in result['previous']]
    for service in ('jira','identity'):
        cert = x509.load_pem_x509_certificate((current / (service + '-cert.pem')).read_bytes())
        result['certificates'][service] = {'serial': str(cert.serial_number),
            'expires_at': cert.not_valid_after_utc.isoformat(),
            'renewal_due': cert.not_valid_after_utc - now <= timedelta(days=7)}
    return result
