"""ToolPort explícito: cliente MCP, ciclo de vida y validación de frontera."""
from dataclasses import asdict
import json
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult

from ..contracts import ContractError, ToolCall, ToolResult, tool_arguments
from ..fakes import ReadOnlyPolicy
from ..telemetry import span


class MCPRead:
    def __init__(self, identity, delay_seconds=0, client_factory=Client, include_pr=False):
        self.identity, self.client_factory = identity, client_factory
        self.invocations, self.connections = 0, 0
        subject = {**asdict(identity), 'roles': sorted(identity.roles),
                   'projects': sorted(identity.projects), 'repositories': sorted(identity.repositories), 'jobs': sorted(identity.jobs)}
        self.parameters = StdioServerParameters(
            command=sys.executable, args=['-m', self.server_module],
            cwd=Path(__file__).resolve().parents[2],
            env={'HARNESS_MCP_IDENTITY': json.dumps(subject), 'HARNESS_MCP_DELAY': str(delay_seconds), 'HARNESS_MCP_LINKED_PR': str(int(include_pr))})

    def connection(self):
        return self.client_factory(self.parameters)

    async def invoke(self, identity, call: ToolCall) -> ToolResult:
        if identity != self.identity:
            raise ContractError('FORBIDDEN')
        ReadOnlyPolicy().authorize(identity, call)
        arguments = tool_arguments(call.name, call.arguments)
        if call.name != self.tool_name:
            raise ContractError('UNKNOWN_TOOL')
        self.connections += 1
        compatible, result = False, None
        try:
            # El core envuelve todo este await: startup, initialize, list y tools/call.
            async with self.connection() as mcp_connection:
                with span('mcp.discover',values={'tool.name':self.tool_name}):
                    listing = await mcp_connection.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                compatible = (self.tool_name in tools
                              and tools[self.tool_name].input_schema == self.input_schema)
                if compatible:
                    self.invocations += 1
                    # Sesión explícita: sin bucle de callbacks ni reintento automático del Client.
                    with span('mcp.call',values={'tool.name':self.tool_name,'tool.call_id':call.call_id}):
                        result = await mcp_connection.session.call_tool(self.tool_name, arguments)
        except ContractError:
            raise
        except TimeoutError:
            raise
        except Exception:
            raise ContractError('MCP_UNAVAILABLE') from None
        if not compatible:
            raise ContractError('MCP_SCHEMA_MISMATCH')
        return self._normalize(result, call)

    @staticmethod
    def _payload(result):
        if not isinstance(result, CallToolResult):
            raise ContractError('INVALID_TOOL_OUTPUT')
        payload = result.structured_content
        if result.is_error:
            error = payload.get('error') if type(payload) is dict else None
            code = error.get('code') if type(error) is dict else None
            if type(code) is not str or code not in {'FORBIDDEN', 'NOT_FOUND', 'INVALID_ARGUMENT', 'UNKNOWN_TOOL', 'UPSTREAM_UNAVAILABLE','PROVIDER_RATE_LIMITED'}:
                code = 'MCP_TOOL_ERROR'
            raise ContractError(code)
        if (type(payload) is not dict or set(payload) != {'schema_version', 'data', 'source_id'}
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1):
            raise ContractError('INVALID_TOOL_OUTPUT')
        return payload
