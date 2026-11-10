"""Servidor stdio local: identidad de desarrollo fijada por el proceso host."""
import json
import os

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from .contracts import ContractError, Identity, ToolCall
from .telemetry import span, annotate


def build_read_server(identity, tool_name, input_schema, validate, backend, *, binding=None, description='Consulta un recurso ficticio autorizado. Solo lectura.'):
    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(name=tool_name,
            description=description, inputSchema=input_schema)])

    async def call_tool(context, params):
        with span('mcp.tool',values={'tool.name':tool_name}):
            return await handle_tool(context,params)

    async def handle_tool(context, params):
        try:
            if params.name != tool_name:
                raise ContractError('UNKNOWN_TOOL')
            call = ToolCall(params.name, validate(params.arguments))
            subject, provider = binding() if binding else (identity, backend)
            result = await provider.invoke(subject, call)  # El fake revalida acceso en el servidor.
            payload = {'schema_version': 1, 'data': result.data, 'source_id': result.source_id}
            return CallToolResult(content=[TextContent(type='text', text=json.dumps(payload))],
                                  structuredContent=payload)
        except ContractError as error:
            annotate(**{'error.code':error.code})
            return CallToolResult(content=[TextContent(type='text', text=error.code)],
                                  structuredContent={'error': {'code': error.code}}, isError=True)
        except Exception:
            annotate(**{'error.code':'UPSTREAM_UNAVAILABLE'})
            return CallToolResult(content=[TextContent(type='text', text='UPSTREAM_UNAVAILABLE')],
                                  structuredContent={'error': {'code': 'UPSTREAM_UNAVAILABLE'}}, isError=True)

    return Server(tool_name, version='1', on_list_tools=list_tools, on_call_tool=call_tool,
                  get_tool_input_schema=lambda name: input_schema if name == tool_name else None)


async def main(server_factory):
    # No se cargan .env ni credenciales; configuración sintética del host, no del modelo.
    subject = json.loads(os.environ['HARNESS_MCP_IDENTITY'])
    identity = Identity(subject['actor_id'], subject['workspace_id'],
                        frozenset(subject['roles']), frozenset(subject['projects']),
                        frozenset(subject.get('repositories', [])), frozenset(subject.get('jobs', [])))
    server = server_factory(identity, float(os.environ.get('HARNESS_MCP_DELAY', '0')),
                          os.environ.get('HARNESS_MCP_LINKED_PR') == '1')
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
