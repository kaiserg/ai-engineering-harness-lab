"""Perfil de CI con un PR público y un job de Jenkins local autorizados."""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from .ci_chat import CIChatPolicy
from .contracts import ContractError, FinalAnswer, ToolCall, build_arguments, pull_request_arguments, result_call
from .core import Limits
from .github_chat import GitHubPolicy, PUBLIC
from .postgres_config import RUNTIME

from .profiles import LIVE_CI_PREFIX

LIMITS = Limits(model_calls=4, tool_calls=3)
CONFIG = RUNTIME / 'jenkins-ci.json'
NEEDS_INPUT = 'Indicá el PR configurado y el número del build, por ejemplo repositorio#1 build 1.'


@dataclass(frozen=True)
class Binding:
    url: str
    user: str
    workspace: str
    actor: str
    repository: str
    pull_number: int
    job: str

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or self.url != f'http://127.0.0.1:{parsed.port}'
                or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', self.user)
                or not self.workspace or not self.actor):
            raise ValueError('Configuración de Jenkins inválida')
        pull_request_arguments(self.pull())
        build_arguments(self.build(1))

    @property
    def profile(self):
        return LIVE_CI_PREFIX + sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def authorize_identity(self, identity):
        if (identity.actor_id, identity.workspace_id) != (self.actor, self.workspace):
            raise ContractError('FORBIDDEN')

    def pull(self):
        return {'repository': self.repository, 'pull_number': self.pull_number}

    def build(self, number):
        return {'job': self.job, 'build_number': number}

    def number(self, query):
        reference = re.escape(f'{self.repository}#{self.pull_number}')
        match = re.fullmatch(r'\s*'+reference+r'\s+build\s+([1-9][0-9]{0,6})\s*', query, re.IGNORECASE)
        if match and int(match[1]) <= 1_000_000:
            return int(match[1])
        return None


def load(profile=None):
    try:
        if CONFIG.is_symlink() or CONFIG.stat().st_mode & 0o077 or CONFIG.stat().st_size > 8192:
            raise ValueError()
        data = json.loads(CONFIG.read_text())
        token = data.pop('token')
        if type(token) is not str or not re.fullmatch(r'[A-Za-z0-9]{16,160}', token):
            raise ValueError()
        binding = Binding(**data)
    except (OSError, AttributeError, KeyError, TypeError, ValueError, ContractError):
        raise ContractError('JENKINS_CONFIGURATION') from None
    if profile is not None and profile != binding.profile:
        raise ContractError('CONFIG_CHANGED')
    return binding, token


class LivePolicy(CIChatPolicy):
    def __init__(self, knowledge, binding):
        super().__init__(knowledge)
        self.binding = binding
        self.github = GitHubPolicy(knowledge, PUBLIC)

    def access_batch(self):
        return self.github.access_batch()

    def authorize_context(self, run, identity):
        self.binding.authorize_identity(identity)
        number = self.binding.number(run.read_request['query'])
        calls = [result_call(result) for result in run.results]
        calls.extend(failure.call for failure in run.tool_failures)
        if run.pending_call:
            calls.append(run.pending_call)
        if any(call.name == 'jenkins_get_build' and call.arguments != self.binding.build(number) for call in calls):
            raise ContractError('INVALID_CHECKPOINT')
        with self.access_batch():
            super().authorize_context(run, identity)

    def authorize(self, identity, call):
        super().authorize(identity, call)
        if call.name == 'github_get_pull_request' and call.arguments == self.binding.pull():
            return
        if call.name == 'jenkins_get_build' and call.arguments['job'] == self.binding.job:
            return
        raise ContractError('FORBIDDEN')

    def authorize_result(self, identity, result):
        super().authorize_result(identity, result)
        if result.tool_name == 'github_get_pull_request':
            self.github.authorize_result(identity, result)


class LivePlanner:
    def __init__(self, binding):
        self.binding = binding

    async def decide(self, run, allowed_tools):
        number = self.binding.number(run.read_request['query'])
        if number is None:
            return FinalAnswer(NEEDS_INPUT)
        if not run.results:
            return ToolCall('github_get_pull_request', self.binding.pull())
        if len(run.results) == 1 and not run.tool_failures:
            return ToolCall('jenkins_get_build', self.binding.build(number))
        return FinalAnswer('Evidencia reunida.')


def finish(run, binding):
    number = binding.number(run.read_request['query'])
    if number is None and not run.results and not run.tool_failures:
        return FinalAnswer(NEEDS_INPUT), {'status': 'NEEDS_BUILD', 'sources': []}
    names = [result.tool_name for result in run.results]
    if number is None or names not in (['github_get_pull_request'], ['github_get_pull_request', 'jenkins_get_build']):
        raise ContractError('INCOMPLETE_EVIDENCE')
    pr = run.results[0]
    if any(pr.data[key] != value for key, value in binding.pull().items()):
        raise ContractError('EVIDENCE_MISMATCH')
    sources = [item.source_id for item in run.results]
    pr_sha = pr.data['head_revision']
    prefix = f"PR {binding.repository}#{binding.pull_number}, commit {pr_sha} [{pr.source_id}]. "
    if len(run.results) == 1:
        if (len(run.tool_failures) != 1 or run.tool_failures[0].call.name != 'jenkins_get_build'
                or run.tool_failures[0].call.arguments != binding.build(number)):
            raise ContractError('INCOMPLETE_EVIDENCE')
        return FinalAnswer(prefix + f'Falta evidencia del build {binding.job}#{number}. CI queda sin confirmar.'), {
            'status': 'INCOMPLETE', 'pr_revision': pr_sha, 'build_revision': None,
            'same_revision': None, 'build_successful': None, 'sources': sources}
    build = run.results[1]
    if run.tool_failures or any(build.data[key] != value for key, value in {
            **binding.pull(), **binding.build(number)}.items()):
        raise ContractError('EVIDENCE_MISMATCH')
    build_sha = build.data['revision']
    same = build_sha == pr_sha
    success = build.data['result'] == 'SUCCESS'
    status, explanation = (
        ('STALE_BUILD', 'El build ejecutó otro commit. CI queda sin confirmar para el commit del PR.') if not same else
        ('VERIFIED', 'El build pasó para el commit observado del PR.') if success else
        ('BUILD_FAILED', 'El build falló para el commit observado del PR.'))
    return FinalAnswer(prefix + f'Build {binding.job}#{number}, {build_sha} [{build.source_id}]. ' + explanation), {
        'status': status, 'pr_revision': pr_sha, 'build_revision': build_sha,
        'same_revision': same, 'build_successful': success, 'sources': sources}
