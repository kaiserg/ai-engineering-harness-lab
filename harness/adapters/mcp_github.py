"""Segundo servicio MCP: mismo transporte y controles, contrato propio."""
from ..contracts import ContractError, ToolResult
from ..mcp_contract import GITHUB_INPUT_SCHEMA
from .mcp_read import MCPRead


class MCPGitHub(MCPRead):
    tool_name = 'github_get_pull_request'
    server_module = 'harness.github_mcp_server'
    input_schema = GITHUB_INPUT_SCHEMA

    @staticmethod
    def _normalize(result, call):
        payload = MCPRead._payload(result)
        data = payload['data']
        if (type(data) is not dict
                or set(data) != {'repository', 'pull_number', 'state', 'head_revision'}
                or type(data['pull_number']) is not int
                or data['repository'] != call.arguments['repository']
                or data['pull_number'] != call.arguments['pull_number']
                or data['state'] != 'OPEN' or data['head_revision'] != 'revision-A'
                or payload['source_id'] != f"github:{data['repository']}:pr:{data['pull_number']}:{data['head_revision']}"):
            raise ContractError('INVALID_TOOL_OUTPUT')
        return ToolResult(call.call_id, dict(data), payload['source_id'], tool_name=call.name)
