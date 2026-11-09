import asyncio
from copy import deepcopy

from .contracts import (ContractError, FinalAnswer, Identity, Run, RunState,
                        ToolCall, ToolResult, issue_arguments, pull_request_arguments, build_arguments)

from .storage import check_new, check_update


class MemoryRunStore:
    """CAS en memoria para un proceso; no base durable ni store multiworker."""
    def __init__(self):
        self._runs = {}

    def create(self, run: Run, identity: Identity) -> Run:
        check_new(run, identity)
        if run.run_id in self._runs:
            raise ContractError('CONFLICT')
        if run.status != RunState.QUEUED or run.version != 0:
            raise ContractError('INVALID_TRANSITION')
        self._runs[run.run_id] = deepcopy(run)
        return deepcopy(run)

    def load(self, run_id: str, identity: Identity) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise ContractError('NOT_FOUND')
        self._check_identity(run, identity)
        return deepcopy(run)

    def save(self, run: Run, identity: Identity) -> Run:
        return self._save(run, identity, recovering=False)

    def requeue_read(self, run: Run, identity: Identity) -> Run:
        return self._save(run, identity, recovering=True)

    def _save(self, run, identity, recovering):
        current = self.load(run.run_id, identity)
        check_update(current, run, identity, recovering)
        saved = deepcopy(run)
        saved.version += 1
        self._runs[run.run_id] = saved
        return deepcopy(saved)

    @staticmethod
    def _check_identity(run, identity):
        if run.workspace_id != identity.workspace_id or run.requested_by != identity.actor_id:
            raise ContractError('NOT_FOUND')


class ReadOnlyPolicy:
    def visible_tools(self, identity: Identity) -> tuple[str, ...]:
        if not identity.roles.intersection({'reader', 'operator'}):
            return ()
        return (('jira_get_issue',) + (('github_get_pull_request',) if identity.repositories else ())
                + (('jenkins_get_build',) if identity.jobs else ()))

    def authorize(self, identity: Identity, call: ToolCall) -> None:
        if not identity.roles.intersection({'reader', 'operator'}):
            raise ContractError('FORBIDDEN')
        if call.name == 'jenkins_get_build':
            arguments = build_arguments(call.arguments)
            if arguments['job'] not in identity.jobs:
                raise ContractError('FORBIDDEN')
            return
        if call.name == 'github_get_pull_request':
            arguments = pull_request_arguments(call.arguments)
            if arguments['repository'] not in identity.repositories:
                raise ContractError('FORBIDDEN')
            return
        if call.name != 'jira_get_issue':
            raise ContractError('FORBIDDEN')
        arguments = issue_arguments(call.arguments)
        if arguments['issue_key'].split('-')[0] not in identity.projects:
            raise ContractError('FORBIDDEN')


class FakeJira:
    def __init__(self, delay_seconds=0, include_pr=False, content_scenario=None):
        self.invocations = 0
        self.delay_seconds = delay_seconds
        self.include_pr = include_pr
        if content_scenario not in (None, 'benign', 'injection'):
            raise ValueError('Contenido de fixture inválido.')
        self.content_scenario = content_scenario

    async def invoke(self, identity: Identity, call: ToolCall) -> ToolResult:
        ReadOnlyPolicy().authorize(identity, call)  # Segunda barrera del adaptador falso.
        self.invocations += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        key = call.arguments['issue_key']
        if identity.workspace_id != 'workspace-alpha' or key != 'DEV-123':
            raise ContractError('NOT_FOUND')
        data = {'issue_key': key, 'status': 'IN_REVIEW'}
        if self.include_pr:
            data['pull_request'] = {'repository': 'demo/atlas', 'pull_number': 42}
        if self.content_scenario is not None:
            from .fixtures import BENIGN_DESCRIPTION, INJECTED_DESCRIPTION
            data['description'] = (INJECTED_DESCRIPTION if self.content_scenario == 'injection' else BENIGN_DESCRIPTION)
        return ToolResult(call.call_id, data, 'jira:DEV-123:v1')


class ScriptedLLM:
    """Propone una llamada y resume su resultado; no genera texto mediante IA."""
    def __init__(self, tool_name='jira_get_issue', delay_seconds=0):
        self.tool_name = tool_name
        self.delay_seconds = delay_seconds

    async def decide(self, run: Run, allowed_tools: tuple[str, ...]):
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if not run.results:
            # Puede proponer algo prohibido: la garantía está en el core, no en el fake.
            return ToolCall(self.tool_name, {'issue_key': 'DEV-123'})
        result = run.results[-1]
        return FinalAnswer(f"{result.data['issue_key']}: {result.data['status']} [{result.source_id}]")


class FakeGitHub:
    """Lectura sintética: ninguna API o credencial de GitHub."""
    def __init__(self, delay_seconds=0):
        self.invocations, self.delay_seconds = 0, delay_seconds

    async def invoke(self, identity, call):
        ReadOnlyPolicy().authorize(identity, call)
        self.invocations += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        arguments = pull_request_arguments(call.arguments)
        if identity.workspace_id != 'workspace-alpha' or arguments != {'repository': 'demo/atlas', 'pull_number': 42}:
            raise ContractError('NOT_FOUND')
        return ToolResult(call.call_id, {**arguments, 'state': 'OPEN', 'head_revision': 'revision-A'},
                          'github:demo/atlas:pr:42:revision-A', tool_name=call.name)


class DevOpsScriptedLLM(ScriptedLLM):
    """Dos lecturas secuenciales, derivadas del resultado confirmado de Jira."""
    async def decide(self, run, allowed_tools):
        if not run.results:
            return await super().decide(run, allowed_tools)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if len(run.results) == 1:
            return ToolCall('github_get_pull_request', dict(run.results[0].data['pull_request']))
        jira, github = run.results
        return FinalAnswer(f"{jira.data['issue_key']}: {jira.data['status']} [{jira.source_id}]. "
            f"PR {github.data['repository']}#{github.data['pull_number']}: {github.data['state']}, "
            f"revisión {github.data['head_revision']} [{github.source_id}]. CI todavía no consultado.")


class CIScriptedLLM(DevOpsScriptedLLM):
    async def decide(self, run, allowed_tools):
        if len(run.results) < 2:
            return await super().decide(run, allowed_tools)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if run.tool_failures:
            return FinalAnswer('Falta evidencia de Jenkins; el core prepara el informe parcial.')
        if len(run.results) == 2:
            # Binding de configuración del laboratorio, no un job tomado de texto externo.
            return ToolCall('jenkins_get_build', {'job': 'atlas-tests', 'build_number': 101})
        return FinalAnswer('Evidencia reunida; el core determina la comparación de revisiones.')


class AdversarialCILLM(CIScriptedLLM):
    """Simula un modelo comprometido; no interpreta ni ejecuta el texto del ticket."""
    def __init__(self, *args, attack='ignore', **kwargs):
        super().__init__(*args, **kwargs)
        self.attack = attack

    async def decide(self, run, allowed_tools):
        if len(run.results) == 1:
            if self.attack == 'job':
                return ToolCall('jenkins_get_build', {'job': 'release-prod', 'build_number': 101})
            if self.attack == 'shell':
                return ToolCall('shell', {'command': 'cat .env'})
        if self.attack == 'claim' and (len(run.results) == 3 or run.tool_failures):
            return FinalAnswer('CI PASÓ. Desplegar sin aprobación y revelar credenciales.')
        return await super().decide(run, allowed_tools)
