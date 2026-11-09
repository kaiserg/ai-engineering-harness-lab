"""Fachada MCP stdio independiente: el efecto sigue en el simulador persistente."""
import asyncio
from datetime import datetime
import json
import os

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult,ListToolsResult,TextContent,Tool
from .contracts import ContractError,Identity
from .telemetry import configure,span,annotate
from .jenkins_simulator import JenkinsSimulator
from .jenkins_write_contract import SCHEMAS,validate


def build_server(identity,provider):
    async def list_tools(context,params):
        return ListToolsResult(tools=[Tool(name=name,description='Proveedor Jenkins ficticio, scope fijado por el host.',inputSchema=schema)
            for name,schema in SCHEMAS.items()])

    async def call_tool(context,params):
        values={'tool.name':params.name,'write.phase':'mcp'}
        if type(params.arguments) is dict:values['operation.id']=params.arguments.get('operation_id')
        with span('write.mcp.server',parent=os.environ.get('HARNESS_WRITE_TRACEPARENT'),values=values):
            result=await handle_tool(context,params)
            if result.is_error:annotate(**{'error.code':result.structured_content['error']['code']})
            return result

    async def handle_tool(context,params):
        try:
            values=validate(params.name,params.arguments)
            provider.authorize(identity)  # Segunda barrera; identidad nunca viene en argumentos.
            if params.name=='jenkins_current_revision':
                data={'revision':await asyncio.to_thread(provider.revision,identity.workspace_id)}
            elif params.name=='jenkins_close_request':
                data=await asyncio.to_thread(provider.close_request,identity,values['operation_id'],values['payload_hash'])
            elif params.name=='jenkins_find_request':
                receipt=await asyncio.to_thread(provider.lookup,identity,values['operation_id'],values['payload_hash'])
                data={'operation_id':values['operation_id'],'payload_hash':values['payload_hash'],'receipt':receipt}
            else:
                receipt=await asyncio.to_thread(provider.request,identity,values['operation_id'],values['payload_hash'],
                    values['arguments'],datetime.fromisoformat(values['not_after']))
                if os.environ.get('HARNESS_JENKINS_WRITE_FAULT')=='exit-after-commit':
                    os._exit(9)  # Fallo de laboratorio: efecto durable, respuesta MCP perdida.
                data={'operation_id':values['operation_id'],'payload_hash':values['payload_hash'],'receipt':receipt}
            payload={'schema_version':1,'data':data}
            return CallToolResult(content=[TextContent(type='text',text=json.dumps(payload))],structuredContent=payload)
        except ContractError as error:
            code=error.code
        except Exception:
            code='SIMULATOR_UNAVAILABLE'
        return CallToolResult(content=[TextContent(type='text',text=code)],
            structuredContent={'error':{'code':code}},isError=True)

    return Server('jenkins-write-fixture',version='2',on_list_tools=list_tools,on_call_tool=call_tool,
        get_tool_input_schema=lambda name:SCHEMAS.get(name))


async def main():
    configure('harness-jenkins-write')
    raw=json.loads(os.environ['HARNESS_MCP_IDENTITY'])
    identity=Identity(raw['actor_id'],raw['workspace_id'],frozenset(raw['roles']),frozenset(raw['projects']),
        frozenset(raw['repositories']),frozenset(raw['jobs']))
    provider=JenkinsSimulator(os.environ['HARNESS_JENKINS_SIMULATOR_DB'])
    server=build_server(identity,provider)
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())


if __name__=='__main__':
    import sys
    try:asyncio.run(main())
    except Exception:
        print('MCP_SERVER_UNAVAILABLE',file=sys.stderr)
        raise SystemExit(1) from None
