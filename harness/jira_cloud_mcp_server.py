"""Jira Cloud por MCP stdio; identidad del host y credenciales privadas locales."""
import asyncio
from .contracts import issue_arguments
from .jira_cloud import JiraCloud
from .mcp_contract import JIRA_INPUT_SCHEMA
from .mcp_server import build_read_server,main


def build_server(identity,delay_seconds=0,include_pr=False):
    return build_read_server(identity,'jira_get_issue',JIRA_INPUT_SCHEMA,issue_arguments,JiraCloud(),
        description='Lee un ticket autorizado de Jira Cloud. No admite cambios.')


if __name__=='__main__':asyncio.run(main(build_server))
