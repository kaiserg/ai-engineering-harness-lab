"""Contrato y normalización de Jira, sobre el transporte MCP compartido."""
from ..contracts import ContractError, ToolResult, pull_request_arguments
from ..mcp_contract import JIRA_INPUT_SCHEMA
from .mcp_read import MCPRead


class MCPJira(MCPRead):
    tool_name = 'jira_get_issue'
    server_module = 'harness.jira_mcp_server'
    input_schema = JIRA_INPUT_SCHEMA

    def __init__(self, *args, content_scenario=None, **kwargs):
        if content_scenario not in (None, 'benign', 'injection'):
            raise ValueError('Contenido de fixture inválido.')
        super().__init__(*args, **kwargs)
        if content_scenario is not None:
            self.parameters.env['HARNESS_MCP_CONTENT'] = content_scenario

    @staticmethod
    def _normalize(result, call):
        payload = MCPRead._payload(result)
        data = payload['data']
        if (type(data) is not dict or not {'issue_key', 'status'} <= set(data)
                or not set(data) <= {'issue_key', 'status', 'pull_request', 'description'}
                or data['issue_key'] != call.arguments['issue_key']
                or data['status'] not in ('IN_REVIEW',)
                or payload['source_id'] != f"jira:{data['issue_key']}:v1"):
            raise ContractError('INVALID_TOOL_OUTPUT')
        if 'description' in data and (type(data['description']) is not str or len(data['description']) > 4000):
            raise ContractError('INVALID_TOOL_OUTPUT')
        if 'pull_request' in data:
            try:
                pull_request_arguments(data['pull_request'])
            except ContractError:
                raise ContractError('INVALID_TOOL_OUTPUT') from None
        # MCP correlaciona la respuesta RPC; el adaptador conserva el call_id del dominio.
        return ToolResult(call.call_id, dict(data), payload['source_id'])
