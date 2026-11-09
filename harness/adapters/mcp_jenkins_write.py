"""Interfaz síncrona del ledger sobre el cliente MCP asíncrono oficial."""
import asyncio
from dataclasses import asdict
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys

from anyio.from_thread import start_blocking_portal
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import CallToolResult
from ..contracts import ContractError
from ..telemetry import span,carrier
from ..write_observability import client_call
from ..jenkins_simulator import JenkinsSimulator
from ..jenkins_write_contract import SCHEMAS,validate,validate_closure


class MCPJenkinsWrite:
    def __init__(self,identity,path,*,client_factory=Client,timeout=4):
        if not 0<timeout<=4:raise ValueError('Timeout MCP inválido')
        self.identity,self.client_factory,self.timeout=identity,client_factory,timeout
        subject={**asdict(identity),**{name:sorted(getattr(identity,name)) for name in ('roles','projects','repositories','jobs')}}
        self.parameters=StdioServerParameters(command=sys.executable,args=['-m','harness.jenkins_write_mcp_server'],
            cwd=Path(__file__).resolve().parents[2],env={'HARNESS_MCP_IDENTITY':json.dumps(subject),
                'HARNESS_JENKINS_SIMULATOR_DB':str(Path(path).resolve())})

    def _identity(self,identity):
        if identity!=self.identity:raise ContractError('FORBIDDEN')
        JenkinsSimulator.authorize(identity)

    def revision(self,workspace):
        self._identity(self.identity)
        if workspace!=self.identity.workspace_id:raise ContractError('FORBIDDEN')
        return self._call('jenkins_current_revision',{})

    def lookup(self,identity,operation_id,payload_hash):
        self._identity(identity)
        return self._call('jenkins_find_request',{'operation_id':operation_id,'payload_hash':payload_hash})

    def close_request(self,identity,operation_id,payload_hash):
        self._identity(identity)
        return self._call('jenkins_close_request',{'operation_id':operation_id,'payload_hash':payload_hash})

    def request(self,identity,operation_id,payload_hash,arguments,not_after):
        self._identity(identity)
        if not isinstance(not_after,datetime) or not_after.utcoffset() is None:raise ContractError('INVALID_ARGUMENT')
        if datetime.now(timezone.utc)>=not_after:raise ContractError('DISPATCH_EXPIRED')
        return self._call('jenkins_request_tests',{'operation_id':operation_id,'payload_hash':payload_hash,
            'arguments':arguments,'not_after':not_after.isoformat()})

    def _call(self,name,arguments):
        validate(name,arguments)
        try:
            # Portal en otro thread: funciona también si el consumidor ya tiene un event loop.
            # No hay loop de agente ni política de reintentos dentro del cliente.
            action={'jenkins_current_revision':'revision','jenkins_request_tests':'request','jenkins_find_request':'lookup','jenkins_close_request':'close'}[name]
            with client_call(action), span('write.mcp',values={'write.phase':'mcp','tool.name':name,'operation.id':arguments.get('operation_id')}):
                with start_blocking_portal() as portal:
                    return portal.call(self._invoke,name,arguments,carrier())
        except ContractError:raise
        except Exception:raise ContractError('MCP_UNAVAILABLE') from None

    async def _invoke(self,name,arguments,parent=None):
        env={**self.parameters.env,'HARNESS_WRITE_TRACEPARENT':parent or '',
            'HARNESS_TRACING':os.environ.get('HARNESS_TRACING','0'),
            'HARNESS_OTLP_ENDPOINT':os.environ.get('HARNESS_OTLP_ENDPOINT','http://jaeger:4318/v1/traces')}
        parameters=self.parameters.model_copy(update={'env':env},deep=True)
        async with asyncio.timeout(self.timeout):
            async with self.client_factory(parameters,cache=None,read_timeout_seconds=self.timeout) as connection:
                listing=await connection.list_tools()
                tools={tool.name:tool.input_schema for tool in listing.tools}
                compatible=len(listing.tools)==len(SCHEMAS) and tools==SCHEMAS
                result=await connection.session.call_tool(name,arguments) if compatible else None
        if not compatible:raise ContractError('MCP_SCHEMA_MISMATCH')
        return self.normalize(name,arguments,result)

    @staticmethod
    def normalize(name,arguments,result):
        if not isinstance(result,CallToolResult):raise ContractError('INVALID_TOOL_OUTPUT')
        payload=result.structured_content
        if result.is_error:
            error=payload.get('error') if type(payload) is dict else None
            code=error.get('code') if type(error) is dict else None
            allowed={'FORBIDDEN','NOT_FOUND','INVALID_ARGUMENT','CONFLICT','SIMULATOR_UNAVAILABLE',
                'SIMULATOR_STATE_LOST','UNKNOWN_TOOL','STALE_PRECONDITION','DISPATCH_EXPIRED','OPERATION_CLOSED'}
            if (type(payload) is not dict or set(payload)!={'error'} or type(error) is not dict
                    or set(error)!={'code'} or type(code) is not str or code not in allowed):
                code='MCP_TOOL_ERROR'
            raise ContractError(code)
        if (type(payload) is not dict or set(payload)!={'schema_version','data'}
                or type(payload['schema_version']) is not int or payload['schema_version']!=1
                or type(payload['data']) is not dict):
            raise ContractError('INVALID_TOOL_OUTPUT')
        data=payload['data']
        if name=='jenkins_current_revision':
            if set(data)!={'revision'} or data['revision'] not in ('revision-A','revision-B'):
                raise ContractError('INVALID_TOOL_OUTPUT')
            return data['revision']
        if name=='jenkins_close_request':
            return validate_closure(data,arguments['operation_id'],arguments['payload_hash'])
        if (set(data)!={'operation_id','payload_hash','receipt'} or data['operation_id']!=arguments['operation_id']
                or data['payload_hash']!=arguments['payload_hash']):
            raise ContractError('INVALID_TOOL_OUTPUT')
        receipt=data['receipt']
        if receipt=='queue:'+arguments['operation_id']:return receipt
        if name=='jenkins_find_request' and receipt is None:return None
        raise ContractError('INVALID_TOOL_OUTPUT')
