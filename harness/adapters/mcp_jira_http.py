"""Mismo contrato Jira; conexión HTTP con credencial de vida corta del worker."""
from contextlib import asynccontextmanager
import os
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ..contracts import ContractError
from ..postgres_config import RUNTIME
from ..service_identity import issue_token
from ..telemetry import headers as trace_headers
from .mcp_jira import MCPJira


class MCPJiraHTTP(MCPJira):
    def __init__(self, identity, *, content_scenario=None):
        self.identity = identity
        self.content = content_scenario or 'benign'
        self.invocations = self.connections = 0
        mode = os.environ.get('HARNESS_SERVICE_IDENTITY', 'local-signing')
        if mode not in ('issuer', 'local-signing'):
            raise ContractError('CONFIG_CHANGED')
        self.secure = mode == 'issuer'
        self.url = os.environ.get('HARNESS_JIRA_MCP_URL',
            'https://127.0.0.1:8010/mcp/' if self.secure else 'http://127.0.0.1:8010/mcp/')
        url = urlsplit(self.url)
        if (url.scheme != ('https' if self.secure else 'http') or url.hostname not in ('jira-mcp', '127.0.0.1', 'localhost')
                or url.username or url.password or url.query or url.fragment or url.path != '/mcp/'):
            raise ContractError('CONFIG_CHANGED')

    @asynccontextmanager
    async def connection(self):
        try:
            if self.secure:
                from ..issuer_client import access_token
                token = await access_token(self.identity, self.content, RUNTIME)
            else:
                private_key = (RUNTIME / 'mcp-worker-private.pem').read_bytes()
                token = issue_token(private_key, self.identity, content=self.content)
        except (OSError, ValueError):
            raise ContractError('MCP_IDENTITY_CONFIGURATION') from None

        async def check_response(response):
            if response.status_code == 401:
                raise ContractError('MCP_UNAUTHENTICATED')
            if response.status_code == 403:
                raise ContractError('FORBIDDEN')

        def auth_error(error):
            if isinstance(error, ContractError):
                return error
            for nested in getattr(error, 'exceptions', ()):
                found = auth_error(nested)
                if found:
                    return found
            return None

        try:
            from ..service_tls import trust_context
            async with httpx2.AsyncClient(verify=trust_context(RUNTIME) if self.secure else True, headers={'Authorization': f'Bearer {token}', **trace_headers()},
                    follow_redirects=False, trust_env=False,
                    event_hooks={'response': [check_response]}) as http:
                async with Client(streamable_http_client(self.url, http_client=http), cache=None) as mcp_connection:
                    yield mcp_connection
        except Exception as error:
            denied = auth_error(error)
            if denied:
                raise ContractError(denied.code) from None
            raise
