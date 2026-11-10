from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Protocol
from uuid import uuid4


class ContractError(Exception):
    """Error de dominio sanitizado: el código no contiene argumentos del usuario."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RunState(StrEnum):
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'


RUN_TRANSITIONS = {
    RunState.QUEUED: {RunState.RUNNING, RunState.CANCELLED},
    RunState.RUNNING: {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED},
}


@dataclass(frozen=True)
class Identity:
    actor_id: str
    workspace_id: str
    roles: frozenset[str]
    projects: frozenset[str]
    repositories: frozenset[str] = frozenset()
    jobs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict
    call_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    data: dict
    source_id: str
    schema_version: int = 1
    tool_name: str = 'jira_get_issue'


@dataclass(frozen=True)
class ToolFailure:
    call: ToolCall
    code: str


@dataclass(frozen=True)
class FinalAnswer:
    text: str


@dataclass
class Run:
    workspace_id: str
    requested_by: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: RunState = RunState.QUEUED
    version: int = 0
    schema_version: int = 2
    model_calls: int = 0
    tool_calls: int = 0
    results: list[ToolResult] = field(default_factory=list)
    answer: str | None = None
    outcome_code: str | None = None
    checkpoint: str = 'READY'
    pending_call: ToolCall | None = None
    limits_snapshot: dict = field(default_factory=dict)
    active_seconds: float = 0.0
    pending_time_seconds: float = 0.0
    recovered_time_seconds: float = 0.0
    timing_version: int = 1
    execution_profile: str = 'fake-jira-v1'
    assessment: dict | None = None
    tool_failures: list[ToolFailure] = field(default_factory=list)
    read_request: dict | None = None


def issue_arguments(arguments: dict) -> dict:
    if (type(arguments) is not dict or set(arguments) != {'issue_key'}
            or type(arguments['issue_key']) is not str
            or not re.fullmatch(r'[A-Z][A-Z0-9]{1,9}-[1-9][0-9]{0,8}', arguments['issue_key'])):
        raise ContractError('INVALID_ARGUMENT')
    return {'issue_key': arguments['issue_key']}


def pull_request_arguments(arguments: dict) -> dict:
    if (type(arguments) is not dict or set(arguments) != {'repository', 'pull_number'}
            or type(arguments['repository']) is not str
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', arguments['repository'])
            or type(arguments['pull_number']) is not int or not 1 <= arguments['pull_number'] <= 1_000_000):
        raise ContractError('INVALID_ARGUMENT')
    return dict(arguments)


def build_arguments(arguments):
    if (type(arguments) is not dict or set(arguments) != {'job', 'build_number'}
            or type(arguments['job']) is not str
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', arguments['job'])
            or type(arguments['build_number']) is not int or not 1 <= arguments['build_number'] <= 1_000_000):
        raise ContractError('INVALID_ARGUMENT')
    return dict(arguments)


READ_TOOLS = frozenset({'jira_get_issue', 'github_get_pull_request', 'jenkins_get_build','knowledge_search'})

# Solo estas ausencias se pueden representar en un informe parcial.
OPTIONAL_READ_ERRORS = {'jenkins_get_build': frozenset({'UPSTREAM_UNAVAILABLE', 'UPSTREAM_TIMEOUT'})}


def knowledge_arguments(arguments):
    if (type(arguments) is not dict or set(arguments)!={'query','limit'}
            or type(arguments['query']) is not str or not arguments['query'].strip()
            or len(arguments['query'])>2000 or type(arguments['limit']) is not int
            or not 1<=arguments['limit']<=3):
        raise ContractError('INVALID_ARGUMENT')
    return dict(arguments)


def tool_arguments(name, arguments):
    if name=='knowledge_search':return knowledge_arguments(arguments)
    if name == 'jira_get_issue':
        return issue_arguments(arguments)
    if name == 'github_get_pull_request':
        return pull_request_arguments(arguments)
    if name == 'jenkins_get_build':
        return build_arguments(arguments)
    raise ContractError('UNKNOWN_TOOL')


def result_call(result):
    # Reautorizar únicamente el recurso identificado por cada resultado guardado.
    keys = {'jira_get_issue': ('issue_key',),
            'github_get_pull_request': ('repository', 'pull_number'),
            'jenkins_get_build': ('job', 'build_number'),
            'knowledge_search':('query','limit')}.get(result.tool_name)
    if keys is None or type(result.data) is not dict:
        raise ContractError('INVALID_TOOL_OUTPUT')
    return ToolCall(result.tool_name, tool_arguments(result.tool_name,
                    {key: result.data.get(key) for key in keys}), result.call_id)


class LLMPort(Protocol):
    async def decide(self, run: Run, allowed_tools: tuple[str, ...]) -> ToolCall | FinalAnswer: ...


class PolicyPort(Protocol):
    def visible_tools(self, identity: Identity) -> tuple[str, ...]: ...
    def authorize(self, identity: Identity, call: ToolCall) -> None: ...


class ToolPort(Protocol):
    async def invoke(self, identity: Identity, call: ToolCall) -> ToolResult: ...


class RunStore(Protocol):
    def create(self, run: Run, identity: Identity) -> Run: ...
    def load(self, run_id: str, identity: Identity) -> Run: ...
    def save(self, run: Run, identity: Identity) -> Run: ...
    def requeue_read(self, run: Run, identity: Identity) -> Run: ...


@dataclass(frozen=True)
class Proposal:
    """Contrato puro de aprobación; no es todavía un flujo de ejecución de escrituras."""
    requested_by: str
    workspace_id: str
    canonical_payload: str
    expires_at: datetime
    status: str = 'PENDING_APPROVAL'
    version: int = 0

    @classmethod
    def create(cls, requested_by, workspace_id, tool, resource, arguments, revision, expires_at):
        def validate(value):
            if type(value) is dict:
                if not all(type(key) is str for key in value):
                    raise ContractError('INVALID_ARGUMENT')
                for item in value.values():
                    validate(item)
            elif type(value) is list:
                for item in value:
                    validate(item)
            elif type(value) not in (str, int, bool, type(None)):
                raise ContractError('INVALID_ARGUMENT')
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ContractError('INVALID_ARGUMENT')
        validate(arguments)
        payload = json.dumps({'actor': requested_by, 'workspace': workspace_id, 'tool': tool,
                              'resource': resource, 'arguments': arguments, 'revision': revision,
                              'expires_at': expires_at.astimezone(timezone.utc).isoformat()},
                             sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        return cls(requested_by, workspace_id, payload, expires_at)

    @property
    def payload_hash(self):
        return sha256(self.canonical_payload.encode()).hexdigest()

    def approve(self, identity, payload_hash, expected_version, now):
        if (identity.workspace_id != self.workspace_id or 'approver' not in identity.roles
                or identity.actor_id == self.requested_by):
            raise ContractError('FORBIDDEN')
        if self.status != 'PENDING_APPROVAL' or self.version != expected_version:
            raise ContractError('CONFLICT')
        if self.expires_at <= now:
            raise ContractError('APPROVAL_EXPIRED')
        if self.payload_hash != payload_hash:
            raise ContractError('CONFLICT')
        return replace(self, status='APPROVED', version=self.version + 1)


@dataclass(frozen=True)
class Lease:
    owner: str
    fencing_token: int
    expires_at: datetime

    def check(self, owner, fencing_token, now):
        if owner != self.owner or fencing_token != self.fencing_token or now >= self.expires_at:
            raise ContractError('LEASE_LOST')


@dataclass(frozen=True)
class Operation:
    operation_id: str
    status: str = 'PREPARED'

    def dispatch(self):
        if self.status != 'PREPARED':
            raise ContractError('INVALID_TRANSITION')
        return replace(self, status='DISPATCHING')

    def interrupted(self):
        # PREPARED sigue sin despacharse; DISPATCHING puede haber tenido un efecto.
        return replace(self, status='UNKNOWN') if self.status == 'DISPATCHING' else self

    def recovery_strategy(self, *, guaranteed_deduplication=False, reliable_lookup=False):
        if self.status != 'UNKNOWN':
            raise ContractError('INVALID_TRANSITION')
        if guaranteed_deduplication:
            return 'RETRY_SAME_KEY'
        return 'LOOKUP' if reliable_lookup else 'MANUAL_RESOLUTION'
