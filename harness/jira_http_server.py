"""Primer MCP independiente, stateless y autenticado en cada solicitud HTTP."""
from contextlib import asynccontextmanager
from contextvars import ContextVar
import os
import asyncio
import time

from fastapi import FastAPI
from starlette.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .contracts import ContractError, issue_arguments
from .fakes import FakeJira
from .mcp_contract import JIRA_INPUT_SCHEMA
from .mcp_server import build_read_server
from .service_identity import verify_token
from .postgres_config import RUNTIME


def create_app(public_key=None, *, fixture_delay=0, live=None, clock=time.monotonic):
    mode = os.environ.get('HARNESS_SERVICE_IDENTITY', 'local-signing')
    if mode not in ('issuer', 'local-signing'):
        raise ContractError('CONFIG_CHANGED')
    issuer, keys = None, None
    if public_key is None and os.environ.get('HARNESS_SERVICE_IDENTITY') == 'issuer':
        from .issuer_client import verification_key as fetch_key
        from .identity_server import ISSUER
        keys = fetch_key(RUNTIME)
        issuer = ISSUER
    verification_key = public_key if keys is not None else (public_key or (RUNTIME / 'mcp-worker-public.pem').read_bytes())
    binding = ContextVar('jira_service_binding')
    server = build_read_server(None, 'jira_get_issue', JIRA_INPUT_SCHEMA,
        issue_arguments, None, binding=binding.get)
    manager = StreamableHTTPSessionManager(server, stateless=True, json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=['jira-mcp:8443', 'jira-mcp:8000', '127.0.0.1:*', 'localhost:*'],
            allowed_origins=['https://127.0.0.1:*', 'https://localhost:*', 'http://127.0.0.1:*', 'http://localhost:*']))

    key_state = {'keys':keys, 'updated':clock()}
    async def refresh():
        import httpx2
        from .issuer_client import issuer_endpoint
        from .service_tls import trust_context
        from .signing_keys import parse_public_keys
        while True:
            try:
                async with httpx2.AsyncClient(verify=trust_context(RUNTIME),trust_env=False,
                        follow_redirects=False,timeout=2) as client:
                    response = await client.get(issuer_endpoint('/jwks'))
                    response.raise_for_status()
                    new_keys = parse_public_keys(response.json()['keys'])
                key_state.update(keys=new_keys, updated=clock())
            except Exception:
                pass  # El request rechaza un cache de más de 15 s; ningún fallback de autoridad.
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(refresh()) if keys is not None and live else None
        try:
            async with manager.run():
                yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['jira-mcp', '127.0.0.1', 'localhost'])

    @app.get('/health')
    async def health():
        state = live.snapshot() if live else None
        data = {'status':'ok', **({'sequence':state['sequence'],'serial':state['serial'],
            'known_kids':list(key_state['keys']), 'key_deadlines':{kid:value[1] for kid,value in key_state['keys'].items()},
            'jwks_fresh':clock()-key_state['updated'] < 15,
            'live_error':live.error} if state else {})}
        ready = not live or (data['jwks_fresh'] and not data['live_error'])
        return JSONResponse(data,status_code=200 if ready else 503)


    # ASGI directo: no buffers de streaming ni pérdida de contexto por BaseHTTPMiddleware.
    class AuthenticatedMCP:
        async def __call__(self, scope, receive, send):
            headers = {key.lower(): value for key, value in scope.get('headers', [])}
            reason='MISSING_OR_MALFORMED_TOKEN'
            try:
                auth = headers.get(b'authorization', b'').decode('ascii')
                if not auth.startswith('Bearer ') or len(auth) > 8192:
                    raise ContractError('MCP_UNAUTHENTICATED')
                key = verification_key
                if keys is not None:
                    import jwt
                    from .signing_keys import select_key
                    try:
                        if live and clock()-key_state['updated'] >= 15:
                            reason='JWKS_STALE'
                            raise ValueError('Stale key cache')
                        reason='UNKNOWN_OR_RETIRED_KEY'
                        key = select_key(key_state['keys'], auth[7:])
                    except (jwt.PyJWTError, ValueError):
                        raise ContractError('MCP_UNAUTHENTICATED') from None
                reason='TOKEN_INVALID_OR_FORBIDDEN'
                identity, fixture = verify_token(auth[7:], key, **({'issuer': issuer} if issuer else {}))
            except (ContractError, UnicodeError) as error:
                code = getattr(error, 'code', 'MCP_UNAUTHENTICATED')
                if live:
                    import json
                    print(json.dumps({'event':'mcp_auth_rejected','reason':reason,
                        'cache_age_seconds':round(clock()-key_state['updated'],3)}),flush=True)
                status = 403 if code == 'FORBIDDEN' else 401
                await JSONResponse({'error': code}, status_code=status,
                    headers={'WWW-Authenticate': 'Bearer'} if status == 401 else {})(scope, receive, send)
                return
            token = binding.set((identity, FakeJira(fixture_delay, include_pr=True, content_scenario=fixture)))
            try:
                await manager.handle_request(scope, receive, send)
            finally:
                binding.reset(token)

    # Mount evita redirecciones y recibe GET/POST/DELETE del transporte oficial.
    app.mount('/mcp', AuthenticatedMCP())
    from .metrics import MetricsHTTP
    app.add_middleware(MetricsHTTP,endpoint='mcp')
    from .telemetry import TraceHTTP
    app.add_middleware(TraceHTTP,service='harness-jira')
    return app


def main():
    from .metrics import start
    start()
    import uvicorn
    if os.environ.get('HARNESS_LIVE_SECURITY') == '1':
        from .live_materials import LiveMaterials, run_live
        live = LiveMaterials('jira')
        return run_live(create_app(live=live), live,
            host=os.environ.get('HARNESS_MCP_BIND','127.0.0.1'), port=int(os.environ.get('HARNESS_MCP_PORT','8443')))
    tls = ({'ssl_keyfile': str(RUNTIME / 'jira-key.pem'),
            'ssl_certfile': str(RUNTIME / 'jira-cert.pem')}
           if os.environ.get('HARNESS_SERVICE_IDENTITY') == 'issuer' else {})
    uvicorn.run(create_app(), host=os.environ.get('HARNESS_MCP_BIND', '127.0.0.1'),
                port=int(os.environ.get('HARNESS_MCP_PORT', '8010')), access_log=False, **tls)


if __name__ == '__main__':
    main()
