"""ToolPort Jira Cloud usa el mismo cliente MCP; el modelo no recibe la credencial."""
from .mcp_read import MCPRead
from ..contracts import ToolResult
from ..mcp_contract import JIRA_INPUT_SCHEMA
from ..jira_cloud import identifier,PREFIX


class MCPJiraCloud(MCPRead):
    tool_name='jira_get_issue'
    server_module='harness.jira_cloud_mcp_server'
    input_schema=JIRA_INPUT_SCHEMA
    def __init__(self,identity,binding):
        super().__init__(identity);self.binding=identifier(binding)
        self.parameters.env['HARNESS_JIRA_BINDING']=self.binding
    def _normalize(self,result,call):
        from ..jira_chat import validate_result
        payload=self._payload(result)
        answer=ToolResult(call.call_id,payload['data'],payload['source_id'],tool_name='jira_get_issue')
        validate_result(answer,PREFIX+self.binding)
        if answer.data['issue_key']!=call.arguments['issue_key']:
            from ..contracts import ContractError
            raise ContractError('INVALID_TOOL_OUTPUT')
        return answer
