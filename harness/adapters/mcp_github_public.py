"""Contrato de PR público sobre el cliente MCP existente."""
from .mcp_read import MCPRead
from ..contracts import ContractError,ToolResult
from ..mcp_contract import GITHUB_INPUT_SCHEMA
from ..github_public import validate_data,source


class MCPGitHubPublic(MCPRead):
    tool_name='github_get_pull_request'
    server_module='harness.github_public_mcp_server'
    input_schema=GITHUB_INPUT_SCHEMA
    @staticmethod
    def _normalize(result,call):
        payload=MCPRead._payload(result);data=validate_data(payload['data'])
        if (data['repository']!=call.arguments['repository'] or data['pull_number']!=call.arguments['pull_number']
                or payload['source_id']!=source(data)):raise ContractError('INVALID_TOOL_OUTPUT')
        return ToolResult(call.call_id,data,payload['source_id'],tool_name='github_get_pull_request')
