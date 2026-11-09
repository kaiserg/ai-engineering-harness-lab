"""Emisor educativo client_credentials: grants fijos y firma fuera del worker."""
import base64
import hashlib
import hmac
import json
import os
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

from .composition import demo_identity
from .postgres_config import RUNTIME
from .service_identity import issue_token

ISSUER = 'https://identity:8443'


def create_app(directory=RUNTIME, *, live=None):
    private = (directory / 'issuer-signing.pem').read_bytes()
    client_secret = (directory / 'mcp-client-secret').read_text().strip()
    if len(client_secret) != 64 or any(c not in '0123456789abcdef' for c in client_secret):
        raise ValueError('Invalid client credential configuration')
    from .signing_keys import public_jwk, parse_public_keys
    import time
    jwk = public_jwk(private)
    kid = jwk['kid']
    previous_path = directory / 'issuer-previous.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else []
    parse_public_keys([jwk, *previous])
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['identity', 'localhost', '127.0.0.1'])

    @app.get('/health')
    async def health():
        state = live.snapshot() if live else None
        return {'status':'ok', **({'sequence':state['sequence'],'serial':state['serial'],
            'signing_kid':state['jwk']['kid'], 'live_error':live.error} if state else {})}

    @app.get('/jwks')
    async def jwks():
        advertised = live.snapshot()['advertised'] if live else [jwk,*previous]
        return {'keys':[item for item in advertised if item.get('lab_retire_at', float('inf')) > time.time()]}

    @app.post('/token')
    async def token(request: Request):
        def error(code, status=400):
            return JSONResponse({'error': code}, status_code=status,
                headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                         **({'WWW-Authenticate': 'Basic'} if status == 401 else {})})
        if request.headers.get('origin') is not None:
            return error('invalid_request', 403)
        try:
            authorization = request.headers.getlist('authorization')
            if len(authorization) != 1 or not authorization[0].startswith('Basic '):
                return error('invalid_client', 401)
            credentials = base64.b64decode(authorization[0][6:], validate=True).decode('ascii')
            client, secret = credentials.split(':', 1)
            if not (hmac.compare_digest(client, 'harness-worker') and hmac.compare_digest(secret, client_secret)):
                return error('invalid_client', 401)
        except (ValueError, UnicodeError):
            return error('invalid_client', 401)
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            return error('invalid_request')
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 4096:
                return error('invalid_request', 413)
        try:
            form = parse_qs(body.decode('ascii'), strict_parsing=True, keep_blank_values=True)
        except (ValueError, UnicodeError):
            return error('invalid_request')
        if any(len(value) != 1 for value in form.values()) or set(form) - {'grant_type', 'scope', 'fixture'}:
            return error('invalid_request')
        if form.get('grant_type') != ['client_credentials']:
            return error('unsupported_grant_type')
        if form.get('scope') != ['jira:read']:
            return error('invalid_scope')
        fixture = form.get('fixture', ['benign'])[0]
        if fixture not in ('benign', 'injection'):
            return error('invalid_request')
        # El cliente no elige actor/workspace/audience/roles. Registro local del emisor.
        state = live.snapshot() if live else None
        credential = issue_token(state['private'] if state else private, demo_identity(), content=fixture,
            issuer=ISSUER, kid=state['jwk']['kid'] if state else kid)
        return JSONResponse({'access_token': credential, 'token_type': 'Bearer',
            'expires_in': 60, 'scope': 'jira:read'}, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
    from .metrics import MetricsHTTP
    app.add_middleware(MetricsHTTP,endpoint='token')
    from .telemetry import TraceHTTP
    app.add_middleware(TraceHTTP,service='harness-identity')
    return app


def main():
    from .metrics import start
    start()
    import uvicorn
    if os.environ.get('HARNESS_LIVE_SECURITY') == '1':
        from .live_materials import LiveMaterials, run_live
        live = LiveMaterials('identity')
        return run_live(create_app(live=live), live, host='0.0.0.0', port=8443)
    uvicorn.run(create_app(), host='0.0.0.0', port=8443, access_log=False,
        ssl_keyfile=str(RUNTIME / 'identity-key.pem'), ssl_certfile=str(RUNTIME / 'identity-cert.pem'),
        ssl_version=__import__('ssl').PROTOCOL_TLS_SERVER)


if __name__ == '__main__':
    main()
