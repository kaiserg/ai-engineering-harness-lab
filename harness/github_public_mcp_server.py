"""Servidor MCP de lectura pública de PRs, sin credenciales GitHub."""
import asyncio
from .contracts import pull_request_arguments
from .github_public import PublicGitHub
from .mcp_contract import GITHUB_INPUT_SCHEMA
from .mcp_server import build_read_server,main


def build_server(identity,delay_seconds=0,include_pr=False):
    return build_read_server(identity,'github_get_pull_request',GITHUB_INPUT_SCHEMA,pull_request_arguments,PublicGitHub(),
        description='Lee un pull request público autorizado. No modifica repositorios.')


if __name__=='__main__':asyncio.run(main(build_server))
