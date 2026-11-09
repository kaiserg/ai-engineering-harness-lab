"""Identidad de servicio del laboratorio; no sustituye un emisor OIDC empresarial."""
import os
from pathlib import Path
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .contracts import ContractError, Identity
from .postgres_config import RUNTIME

ISSUER = 'harness-lab-worker'
AUDIENCE = 'jira-mcp'
SUBJECT = 'harness-worker'


def prepare_keys(directory=RUNTIME):
    directory.mkdir(parents=True, exist_ok=True)
    private, public = directory / 'mcp-worker-private.pem', directory / 'mcp-worker-public.pem'
    if private.exists() != public.exists():
        raise ValueError('Incomplete service key pair; restore both files.')
    if not private.exists():
        key = Ed25519PrivateKey.generate()
        for path, data in ((private, key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                (public, key.public_key().public_bytes(serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
    # No rotación implícita ni reparación silenciosa de una pareja inconsistente.
    key = serialization.load_pem_private_key(private.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError('Expected an Ed25519 service key.')
    if key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo) != public.read_bytes():
        raise ValueError('Service key pair mismatch.')
    for path in (private, public):
        path.chmod(0o600)


def issue_token(private_key, identity, *, content='benign', issuer=ISSUER, kid=None):
    if content not in ('benign', 'injection'):
        raise ContractError('CONFIG_CHANGED')
    now = int(time.time())
    return jwt.encode({'iss': issuer, 'aud': AUDIENCE, 'sub': SUBJECT,
        'iat': now, 'nbf': now, 'exp': now + 60, 'scope': 'jira:read',
        'actor': identity.actor_id, 'workspace': identity.workspace_id,
        'fixture': content}, private_key, algorithm='EdDSA', headers={'kid': kid} if kid else None)


def verify_token(token, public_key, *, issuer=ISSUER):
    try:
        claims = jwt.decode(token, public_key, algorithms=['EdDSA'],
            issuer=issuer, audience=AUDIENCE,
            options={'require': ['iss', 'aud', 'sub', 'iat', 'nbf', 'exp',
                                  'scope', 'actor', 'workspace', 'fixture']})
        if (type(claims['iat']) is not int or type(claims['nbf']) is not int
                or type(claims['exp']) is not int
                or not 0 < claims['exp'] - claims['iat'] <= 60
                or claims['nbf'] != claims['iat']):
            raise ValueError()
    except (jwt.PyJWTError, ValueError, TypeError):
        raise ContractError('MCP_UNAUTHENTICATED') from None
    if (claims['sub'] != SUBJECT or claims['scope'] != 'jira:read'
            or claims['actor'] != 'reader-demo' or claims['workspace'] != 'workspace-alpha'
            or claims['fixture'] not in ('benign', 'injection')):
        raise ContractError('FORBIDDEN')
    # Grants locales: nunca copiamos roles/proyectos suministrados por el cliente.
    identity = Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}), frozenset({'DEV'}))
    return identity, claims['fixture']
