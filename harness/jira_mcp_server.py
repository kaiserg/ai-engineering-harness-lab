"""Servidor Jira con datos ficticios, separado del servidor GitHub."""
import asyncio
import os

from .contracts import issue_arguments
from .fakes import FakeJira
from .mcp_contract import JIRA_INPUT_SCHEMA
from .mcp_server import build_read_server, main


def build_server(identity, delay_seconds=0, include_pr=False):
    return build_read_server(identity, 'jira_get_issue', JIRA_INPUT_SCHEMA,
                             issue_arguments, FakeJira(delay_seconds, include_pr, os.environ.get('HARNESS_MCP_CONTENT')))


if __name__ == '__main__':
    asyncio.run(main(build_server))
