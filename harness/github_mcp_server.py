"""Servidor GitHub local: catálogo separado y datos sintéticos."""
import asyncio

from .contracts import pull_request_arguments
from .fakes import FakeGitHub
from .mcp_server import build_read_server, main
from .mcp_contract import GITHUB_INPUT_SCHEMA


def build_server(identity, delay_seconds=0, include_pr=False):
    return build_read_server(identity, 'github_get_pull_request', GITHUB_INPUT_SCHEMA,
                             pull_request_arguments, FakeGitHub(delay_seconds))


if __name__ == '__main__':
    asyncio.run(main(build_server))
