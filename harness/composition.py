"""Composición compartida por consola y web; un solo core y los mismos adaptadores."""
from dataclasses import dataclass

from .contracts import ContractError, Identity
from .core import Harness, Limits
from .fakes import (AdversarialCILLM, CIScriptedLLM, DevOpsScriptedLLM, FakeJira,
                    ReadOnlyPolicy, ScriptedLLM)
from .profiles import parse_ci_profile


def demo_identity():
    return Identity('reader-demo', 'workspace-alpha', frozenset({'reader'}),
                    frozenset({'DEV'}), frozenset({'demo/atlas'}), frozenset({'atlas-tests'}))


@dataclass
class Execution:
    harness: Harness
    adapters: dict

    def counters(self):
        return (sum(adapter.invocations for adapter in self.adapters.values()),
                sum(getattr(adapter, 'connections', 0) for adapter in self.adapters.values())
                if any(hasattr(adapter, 'connections') for adapter in self.adapters.values()) else None)


def compose(profile, identity, store, *, limits=Limits(), checkpoint_hook=None, delay=0, denied=False):
    ci = parse_ci_profile(profile)
    if ci is None and profile not in {'fake-jira-v1', 'mcp-jira-fixture-v1', 'mcp-devops-read-v1'}:
        raise ContractError('CONFIG_CHANGED')
    linked = ci is not None or profile == 'mcp-devops-read-v1'
    if profile == 'fake-jira-v1':
        jira = FakeJira(delay)
    else:
        from .adapters.mcp_jira import MCPJira
        if ci and ci[0] == 'mcp-devops-ci-v4':
            from .adapters.mcp_jira_http import MCPJiraHTTP
            jira = MCPJiraHTTP(identity, content_scenario=ci[2])
        else:
            jira = MCPJira(identity, delay, include_pr=linked, content_scenario=ci[2] if ci else None)
    adapters = {'jira': jira}
    tools = {'jira_get_issue': jira, 'jira_delete_issue': jira}
    model_type = ScriptedLLM
    finalizer, partial_tools = None, ()
    if linked:
        from .adapters.mcp_github import MCPGitHub
        adapters['github'] = tools['github_get_pull_request'] = MCPGitHub(identity, delay)
        model_type = DevOpsScriptedLLM
    if ci:
        from .adapters.mcp_jenkins import MCPJenkins
        from .evidence import finalize_ci, finalize_ci_partial
        adapters['jenkins'] = tools['jenkins_get_build'] = MCPJenkins(identity, delay, scenario=ci[1])
        model_type = CIScriptedLLM
        finalizer = finalize_ci
        if ci[0] != 'mcp-devops-ci-v1':
            finalizer, partial_tools = finalize_ci_partial, ('jenkins_get_build',)
    name = 'jira_delete_issue' if denied else 'jira_get_issue'
    model = (AdversarialCILLM(name, delay, attack=ci[3]) if ci and ci[0] in ('mcp-devops-ci-v3', 'mcp-devops-ci-v4')
             else model_type(name, delay))
    return Execution(Harness(model, ReadOnlyPolicy(), tools, store, limits=limits,
                     checkpoint_hook=checkpoint_hook, execution_profile=profile,
                     finalizer=finalizer, partial_tools=partial_tools), adapters)
