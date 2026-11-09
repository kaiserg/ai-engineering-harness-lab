"""Confiar en endpoints fijos por TLS; ningún jku/URL procedente del token."""
import os
from urllib.parse import urlsplit

import httpx2
import jwt
from .contracts import ContractError
from .identity_server import ISSUER
from .postgres_config import RUNTIME
from .service_tls import trust_context
from .telemetry import traced, headers


def endpoint(value, hosts, path):
    url = urlsplit(value)
    if (url.scheme != 'https' or url.hostname not in hosts or url.path != path
            or url.username or url.password or url.query or url.fragment):
        raise ContractError('CONFIG_CHANGED')
    return value


def issuer_endpoint(path):
    base = os.environ.get('HARNESS_ISSUER_URL', ISSUER)
    return endpoint(base + path, ('identity', 'localhost', '127.0.0.1'), path)


def verification_key(directory=RUNTIME):
    try:
        with httpx2.Client(verify=trust_context(directory), trust_env=False, follow_redirects=False, timeout=3) as client:
            response = client.get(issuer_endpoint('/jwks'))
            response.raise_for_status()
        from .signing_keys import parse_public_keys
        return parse_public_keys(response.json()['keys'])
    except Exception:
        raise ContractError('MCP_ISSUER_CONFIGURATION') from None


@traced('identity.get_token')
async def access_token(identity, content, directory=RUNTIME):
    # El registro del emisor solo permite este actor sintético; no delegación arbitraria.
    if identity.actor_id != 'reader-demo' or identity.workspace_id != 'workspace-alpha':
        raise ContractError('FORBIDDEN')
    try:
        secret = (directory / 'mcp-client-secret').read_text().strip()
        async with httpx2.AsyncClient(verify=trust_context(directory), trust_env=False,
                follow_redirects=False, timeout=3) as client:
            response = await client.post(issuer_endpoint('/token'),
                headers=headers(),
                auth=('harness-worker', secret), data={'grant_type': 'client_credentials',
                    'scope': 'jira:read', 'fixture': content})
        if response.status_code in (401, 403):
            raise ContractError('MCP_UNAUTHENTICATED')
        response.raise_for_status()
        result = response.json()
        if (set(result) != {'access_token', 'token_type', 'expires_in', 'scope'}
                or result['token_type'] != 'Bearer' or result['expires_in'] != 60
                or result['scope'] != 'jira:read' or not isinstance(result['access_token'], str)
                or len(result['access_token']) > 8192):
            raise ValueError()
        return result['access_token']
    except ContractError:
        raise
    except Exception:
        raise ContractError('MCP_ISSUER_UNAVAILABLE') from None
