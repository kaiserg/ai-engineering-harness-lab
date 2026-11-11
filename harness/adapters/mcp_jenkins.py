"""Cliente Python de un servidor Go ya compilado: sin compilación dentro del deadline."""
from pathlib import Path
from mcp.client.stdio import StdioServerParameters

from ..contracts import ContractError, ToolResult
from ..mcp_contract import JENKINS_INPUT_SCHEMA
from .mcp_read import MCPRead


class MCPJenkins(MCPRead):
    tool_name = 'jenkins_get_build'
    server_module = ''
    input_schema = JENKINS_INPUT_SCHEMA

    def __init__(self, identity, delay_seconds=0, scenario='matching', **kwargs):
        super().__init__(identity, delay_seconds, **kwargs)
        binary = Path(__file__).resolve().parents[2] / 'bin/jenkins-mcp'
        self.parameters = StdioServerParameters(command=str(binary), args=[],
            env={**self.parameters.env, 'HARNESS_BUILD_SCENARIO': scenario})

    @staticmethod
    def _normalize(result, call):
        payload = MCPRead._payload(result)
        data = payload['data']
        if (type(data) is not dict
                or set(data) != {'job', 'build_number', 'repository', 'revision', 'result'}
                or data['job'] != call.arguments['job']
                or type(data['build_number']) is not int or data['build_number'] != call.arguments['build_number']
                or data['repository'] != 'demo/atlas' or data['revision'] not in ('revision-A', 'revision-B')
                or data['result'] not in ('SUCCESS', 'FAILURE')
                or payload['source_id'] != f"jenkins:{data['job']}:{data['build_number']}:{data['revision']}"):
            raise ContractError('INVALID_TOOL_OUTPUT')
        return ToolResult(call.call_id, dict(data), payload['source_id'], tool_name=call.name)


class MCPJenkinsLive(MCPJenkins):
    def __init__(self, identity, binding, token):
        super().__init__(identity, scenario='live')
        self.parameters.env.update(HARNESS_JENKINS_URL=binding.url, HARNESS_JENKINS_JOB=binding.job,
                                   HARNESS_JENKINS_USER=binding.user, HARNESS_JENKINS_TOKEN=token)

    @staticmethod
    def _normalize(result, call):
        import re
        from ..contracts import pull_request_arguments

        payload = MCPRead._payload(result)
        data = payload['data']
        if (type(data) is not dict
                or set(data) != {'job','build_number','repository','pull_number','revision','result'}
                or data['job'] != call.arguments['job']
                or type(data['build_number']) is not int or data['build_number'] != call.arguments['build_number']
                or type(data['revision']) is not str
                or not re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', data['revision'])
                or data['result'] not in ('SUCCESS', 'FAILURE')
                or payload['source_id'] != f"jenkins:{data['job']}:{data['build_number']}:{data['revision']}"):
            raise ContractError('INVALID_TOOL_OUTPUT')
        try:
            pull_request_arguments({key: data[key] for key in ('repository', 'pull_number')})
        except ContractError:
            raise ContractError('INVALID_TOOL_OUTPUT') from None
        return ToolResult(call.call_id, dict(data), payload['source_id'], tool_name=call.name)
