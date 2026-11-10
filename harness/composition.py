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


def compose(profile, identity, store, *, limits=Limits(), checkpoint_hook=None, delay=0, denied=False, knowledge=None):
    from .rag_agent import PROFILE,KnowledgePolicy,KnowledgeTool,RAGScriptedLLM,finalize_rag
    from .llm_reader import PROFILE as READER_PROFILE,OllamaReader,finish
    from .chat import PROFILE as CHAT_PROFILE,ChatPolicy,ChatReader
    from .jira_chat import is_jira,JiraReader,JiraPolicy,finish as finish_jira
    from .jira_cloud import PREFIX as CLOUD_PREFIX
    from .github_chat import PROFILES as GH_PROFILES,PUBLIC as GH_PUBLIC,GitHubReader,GitHubPolicy,finish as finish_github
    github_chat=profile in GH_PROFILES
    jira_chat=is_jira(profile)
    if profile in (READER_PROFILE,CHAT_PROFILE) or jira_chat or github_chat:
        if knowledge is None:
            from .postgres_store import PostgresRunStore
            from .user_identity import UserIdentityStore
            from .knowledge import Knowledge
            if not hasattr(store,'_dsn') or store.schema=='harness_lab':raise ContractError('CONFIG_CHANGED')
            directory=UserIdentityStore(PostgresRunStore(store._dsn))
            knowledge=Knowledge(store,lambda:directory.resolve(identity.actor_id,identity.workspace_id))
        if github_chat:
            if profile==GH_PUBLIC:
                from .adapters.mcp_github_public import MCPGitHubPublic
                adapter=MCPGitHubPublic(identity)
            else:
                from .adapters.mcp_github import MCPGitHub
                adapter=MCPGitHub(identity)
            policy=GitHubPolicy(knowledge,profile)
            return Execution(Harness(GitHubReader(policy,identity),policy,{'github_get_pull_request':adapter},store,
                limits=limits,checkpoint_hook=checkpoint_hook,execution_profile=profile,decision_finalizer=finish_github),{'github':adapter})
        if jira_chat:
            if profile.startswith(CLOUD_PREFIX):
                from .adapters.mcp_jira_cloud import MCPJiraCloud
                adapter=MCPJiraCloud(identity,profile[len(CLOUD_PREFIX):])
            else:
                from .adapters.mcp_jira import MCPJira
                adapter=MCPJira(identity,content_scenario='benign')
            policy=JiraPolicy(knowledge,profile)
            return Execution(Harness(JiraReader(policy,identity),policy,{'jira_get_issue':adapter},store,
                limits=limits,checkpoint_hook=checkpoint_hook,execution_profile=profile,decision_finalizer=finish_jira),{'jira':adapter})
        adapter=KnowledgeTool(knowledge)
        policy=ChatPolicy(knowledge) if profile==CHAT_PROFILE else KnowledgePolicy(knowledge)
        model=ChatReader(policy,identity) if profile==CHAT_PROFILE else OllamaReader()
        return Execution(Harness(model,policy,{'knowledge_search':adapter},store,
            limits=limits,checkpoint_hook=checkpoint_hook,execution_profile=profile,decision_finalizer=finish),{'knowledge':adapter})
    rag=profile==PROFILE
    ci = parse_ci_profile('mcp-devops-ci-v2:matching' if rag else profile)
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
    policy=ReadOnlyPolicy()
    if rag:
        if knowledge is None:
            from .postgres_store import PostgresRunStore
            from .user_identity import UserIdentityStore
            from .knowledge import Knowledge
            if not isinstance(store,PostgresRunStore) or store.schema=='harness_lab':raise ContractError('CONFIG_CHANGED')
            directory=UserIdentityStore(PostgresRunStore(store._dsn))
            knowledge=Knowledge(store,lambda:directory.resolve(identity.actor_id,identity.workspace_id))
        model=RAGScriptedLLM(name,delay)
        adapters['knowledge']=tools['knowledge_search']=KnowledgeTool(knowledge)
        policy=KnowledgePolicy(knowledge)
        finalizer,partial_tools=finalize_rag,()
    return Execution(Harness(model, policy, tools, store, limits=limits,
                     checkpoint_hook=checkpoint_hook, execution_profile=profile,
                     finalizer=finalizer, partial_tools=partial_tools), adapters)
