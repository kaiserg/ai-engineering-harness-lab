"""Investigación de CI iniciada por un ticket de Jira Cloud."""
from dataclasses import replace
import re

from . import jira_cloud
from .ci_chat import CIChatPolicy
from .ci_live import LivePolicy, finish_for_build, load as load_jenkins
from .contracts import ContractError, FinalAnswer, ToolCall, result_call
from .core import Limits
from .jira_chat import validate_result
from .profiles import JIRA_CI_PREFIX, LIVE_CI_PREFIX

LIMITS = Limits(model_calls=5, tool_calls=4)
NEEDS_INPUT = 'Indicá una clave de Jira y un número de build. Por ejemplo: ¿Pasó el build 1 de PROY-123?'


def load(profile):
    parts = profile.removeprefix(JIRA_CI_PREFIX).split(':')
    if len(parts) != 2:
        raise ContractError('CONFIG_CHANGED')
    binding, token = load_jenkins(LIVE_CI_PREFIX + parts[1])
    return jira_cloud.identifier(parts[0]), binding, token


def current_profile(identity):
    jira = jira_cloud.current()
    config = jira_cloud.load(jira)
    binding, _ = load_jenkins()
    binding.authorize_identity(identity)
    if (config['actor'],config['workspace']) != (identity.actor_id,identity.workspace_id):
        raise ContractError('FORBIDDEN')
    return JIRA_CI_PREFIX + jira + ':' + binding.profile.removeprefix(LIVE_CI_PREFIX)


def requested(query):
    tickets = set(re.findall(r'(?<![\w-])[A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,8}(?![\w-])',query.upper()))
    builds = re.findall(r'\b(?:build|compilación)\s+#?([1-9][0-9]{0,6})(?![\w-])', query, re.IGNORECASE)
    if len(tickets) == len(set(builds)) == 1 and int(builds[0]) <= 1_000_000:
        return tickets.pop(), int(builds[0])
    return None, None


class JiraCIPolicy(LivePolicy):
    def __init__(self, knowledge, binding, jira):
        super().__init__(knowledge,binding)
        self.jira = jira

    def build_number(self, run):
        return requested(run.read_request['query'])[1]

    def authorize_context(self, run, identity):
        self.binding.authorize_identity(identity)
        key, _ = requested(run.read_request['query'])
        config = jira_cloud.load(self.jira)
        if (config['actor'], config['workspace']) != (identity.actor_id, identity.workspace_id):
            raise ContractError('FORBIDDEN')
        calls = [result_call(result) for result in run.results]
        calls.extend(failure.call for failure in run.tool_failures)
        if run.pending_call:
            calls.append(run.pending_call)
        if any(call.name == 'jira_get_issue' and call.arguments != {'issue_key':key} for call in calls):
            raise ContractError('INVALID_CHECKPOINT')
        if any(call.name != 'jira_get_issue' for call in calls):
            if (not run.results or run.results[0].tool_name != 'jira_get_issue'
                    or run.results[0].data.get('pull_requests') != [self.binding.pull()]):
                raise ContractError('EVIDENCE_MISMATCH')
        super().authorize_context(run,identity)
        if key and not run.results:
            jira_cloud.check_scope(config,identity,key)

    def authorize(self, identity, call):
        if call.name == 'jira_get_issue':
            CIChatPolicy.authorize(self, identity, call)
            jira_cloud.check_scope(jira_cloud.load(self.jira),identity,call.arguments['issue_key'])
        else:
            super().authorize(identity,call)

    def authorize_result(self, identity, result):
        super().authorize_result(identity,result)
        if result.tool_name != 'jira_get_issue':
            return
        data = validate_result(result,jira_cloud.PREFIX+self.jira,include_pr=True)
        try:
            jira_cloud.fetch(self.jira,identity,data['issue_key'],access_only=True)
        except ContractError as error:
            if error.code in ('FORBIDDEN','NOT_FOUND','JIRA_CONFIGURATION'):
                raise ContractError('EVIDENCE_REVOKED') from None
            raise


class JiraCIPlanner:
    def __init__(self, binding):
        self.binding = binding

    async def decide(self, run, allowed_tools):
        key, number = requested(run.read_request['query'])
        if key is None:
            return FinalAnswer(NEEDS_INPUT)
        if not run.results:
            return ToolCall('jira_get_issue',{'issue_key':key})
        if run.results[0].data['pull_requests'] == [self.binding.pull()]:
            if len(run.results) == 1:
                return ToolCall('github_get_pull_request',self.binding.pull())
            if len(run.results) == 2 and not run.tool_failures:
                return ToolCall('jenkins_get_build',self.binding.build(number))
        return FinalAnswer('Evidencia reunida.')


def finish(run, binding):
    key, number = requested(run.read_request['query'])
    if key is None and not run.results and not run.tool_failures:
        return FinalAnswer(NEEDS_INPUT), {'status':'NEEDS_ISSUE_BUILD','sources':[]}
    if not run.results or run.results[0].tool_name != 'jira_get_issue' or run.results[0].data['issue_key'] != key:
        raise ContractError('EVIDENCE_MISMATCH')
    jira, *results = run.results
    prefix = f"Jira {key} [{jira.source_id}]. "
    links = jira.data['pull_requests']
    if links != [binding.pull()]:
        if results or run.tool_failures:
            raise ContractError('EVIDENCE_MISMATCH')
        status, message = (
            ('MISSING_PR','La descripción no contiene un vínculo directo a un PR de GitHub.') if not links else
            ('AMBIGUOUS_PR','La descripción contiene vínculos a varios PR.') if len(links)>1 else
            ('PR_NOT_ALLOWED','El PR vinculado queda fuera de la configuración de esta conversación.'))
        return FinalAnswer(prefix + message + ' CI queda sin confirmar.'), {
            'status':status,'sources':[jira.source_id]}
    answer, assessment = finish_for_build(replace(run,results=results),binding,number)
    return FinalAnswer(prefix+answer.text), {**assessment,'sources':[jira.source_id,*assessment['sources']]}
