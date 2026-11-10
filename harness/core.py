import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from time import monotonic

from .timing import ActiveBudget
from .telemetry import span, annotate
from .metrics import operation

from .contracts import (ContractError, FinalAnswer, Identity, LLMPort, PolicyPort,
                        RunState, RunStore, ToolCall, ToolPort, ToolResult, tool_arguments, result_call, READ_TOOLS, ToolFailure, OPTIONAL_READ_ERRORS)


def authorize_run(run, identity, policy):
    if hasattr(policy,"authorize_context"):policy.authorize_context(run,identity)
    for result in run.results:
        policy.authorize(identity, result_call(result))
        if hasattr(policy,'authorize_result'):policy.authorize_result(identity,result)
    for failure in run.tool_failures:
        policy.authorize(identity, failure.call)
    if run.pending_call:
        if run.pending_call.name not in READ_TOOLS:
            raise ContractError('UNSAFE_RECOVERY')
        policy.authorize(identity, run.pending_call)


@dataclass(frozen=True)
class Limits:
    model_calls: int = 4
    tool_calls: int = 8
    model_timeout: float = 30
    tool_timeout: float = 10
    active_timeout: float = 120

    def __post_init__(self):
        if (type(self.model_calls) is not int or type(self.tool_calls) is not int
                or self.model_calls < 1 or self.tool_calls < 1
                or any(type(value) not in (int, float) or not 0 < value < float('inf')
                       for value in (self.model_timeout, self.tool_timeout, self.active_timeout))):
            raise ValueError('Límites inválidos.')


class Harness:
    def __init__(self, llm: LLMPort, policy: PolicyPort, tools: dict[str, ToolPort],
                 store: RunStore, limits=Limits(), checkpoint_hook=None, clock=monotonic, execution_profile='fake-jira-v1', finalizer=None, partial_tools=(), decision_finalizer=None):
        self.llm, self.policy, self.tools, self.store = llm, policy, dict(tools), store
        self.limits, self.checkpoint_hook = limits, checkpoint_hook
        self.clock = clock
        self.execution_profile = execution_profile
        self.finalizer = finalizer
        self.decision_finalizer = decision_finalizer
        if finalizer is not None and decision_finalizer is not None:
            raise ValueError("Elegir un único finalizador.")
        self.partial_tools = frozenset(partial_tools)
        if not self.partial_tools <= OPTIONAL_READ_ERRORS.keys():
            raise ValueError('Herramienta sin política de fallo parcial.')

    def _checkpoint(self, name, run):
        if self.checkpoint_hook:
            self.checkpoint_hook(name, deepcopy(run))

    def _check_limits(self, run):
        if run.execution_profile != self.execution_profile:
            raise ContractError('CONFIG_CHANGED')
        if run.timing_version != 1:
            raise ContractError('TIME_BUDGET_UNKNOWN')
        if run.limits_snapshot and run.limits_snapshot != asdict(self.limits):
            raise ContractError('CONFIG_CHANGED')

    async def _decide(self,run,identity):
        context_validator=getattr(self.policy,'authorize_context',None)
        if context_validator:await asyncio.to_thread(context_validator,run,identity)
        validator=getattr(self.policy,'authorize_result',None)
        if validator:
            for result in run.results:await asyncio.to_thread(validator,identity,result)
        decision=await self.llm.decide(deepcopy(run),tuple(name for name in
            self.policy.visible_tools(identity) if name in self.tools))
        if context_validator:await asyncio.to_thread(context_validator,run,identity)
        if validator and isinstance(decision,FinalAnswer):
            for result in run.results:await asyncio.to_thread(validator,identity,result)
        return decision  # Revalidaciones incluidas en la reserva/timeout persistidos del modelo.

    async def resume(self, run_id: str, identity: Identity):
        # Recuperación local: el operador debe haber detenido el proceso anterior.
        run = self.store.load(run_id, identity)
        if run.status != RunState.COMPLETED:
            self._check_limits(run)
        authorize_run(run, identity, self.policy)
        if run.status == RunState.COMPLETED:
            return run
        if run.status == RunState.RUNNING:
            run.status = RunState.QUEUED
            self.store.requeue_read(run, identity)
        elif run.status != RunState.QUEUED:
            raise ContractError('INVALID_TRANSITION')
        return await self.execute(run_id, identity)

    async def execute(self, run_id: str, identity: Identity):
        run = self.store.load(run_id, identity)
        self._check_limits(run)
        if run.status != RunState.QUEUED:
            raise ContractError('INVALID_TRANSITION')
        retry_pending = run.pending_call is not None
        # Un intento huérfano pudo consumir toda su reserva: descontarla al recuperar.
        run.active_seconds += run.pending_time_seconds
        run.recovered_time_seconds += run.pending_time_seconds
        run.pending_time_seconds = 0.0
        timer = ActiveBudget(self.limits.active_timeout, self.clock)
        def save(snapshot):
            timer.charge(snapshot)
            if snapshot.status == RunState.COMPLETED:
                timer.remaining(snapshot)
            return self.store.save(snapshot, identity)
        run.status = RunState.RUNNING
        run.limits_snapshot = asdict(self.limits)
        run = save(run)
        try:
            while True:
                timer.remaining(run)
                if run.pending_call is not None:
                    # Tras una interrupción no sabemos si el intento anterior llegó al adaptador.
                    # Un nuevo intento de lectura consume otra reserva, conservando call_id.
                    if retry_pending:
                        if run.tool_calls >= self.limits.tool_calls:
                            raise ContractError('BUDGET_EXCEEDED')
                        run.tool_calls += 1
                        run = save(run)
                        retry_pending = False
                    call = run.pending_call
                    if call.name not in READ_TOOLS or call.name not in self.tools:
                        raise ContractError('UNSAFE_RECOVERY')
                    with span('policy.authorize',values={'run.id':run.run_id,'tool.name':call.name}):
                        self.policy.authorize(identity, call)
                    allowance = timer.reserve(run, self.limits.tool_timeout)
                    run = save(run)
                    self._checkpoint('before-tool', run)
                    # El hook puede haber consumido tiempo; nunca ampliar el remanente.
                    allowance = min(allowance, timer.remaining(run))
                    try:
                        with operation('tool',call.name), span('tool.invoke',values={'run.id':run.run_id,'tool.name':call.name,'tool.call_id':call.call_id}):
                            result = await asyncio.wait_for(self.tools[call.name].invoke(identity, call), allowance)
                    except (ContractError, TimeoutError) as error:
                        timer.charge(run)
                        run.pending_time_seconds = 0.0
                        timer.remaining(run)  # El deadline global siempre bloquea, aun si falló el proveedor.
                        code = error.code if isinstance(error, ContractError) else 'UPSTREAM_TIMEOUT'
                        if (call.name not in self.partial_tools
                                or code not in OPTIONAL_READ_ERRORS[call.name]):
                            raise
                        run.tool_failures.append(ToolFailure(deepcopy(call), code))
                        run.pending_call, run.checkpoint = None, 'READY'
                        run = save(run)
                        self._checkpoint('after-tool-failure', run)
                        continue
                    timer.charge(run)
                    run.pending_time_seconds = 0.0
                    timer.remaining(run)
                    if (not isinstance(result, ToolResult) or result.call_id != call.call_id
                            or result.tool_name != call.name
                            or result.schema_version != 1 or type(result.data) is not dict
                            or not result.source_id):
                        raise ContractError('INVALID_TOOL_OUTPUT')
                    run.results.append(result)
                    run.pending_call, run.checkpoint = None, 'READY'
                    run = save(run)
                    self._checkpoint('after-result', run)
                    continue
                if run.model_calls >= self.limits.model_calls:
                    raise ContractError('BUDGET_EXCEEDED')
                run.model_calls += 1
                run.checkpoint = 'MODEL_PENDING'
                allowance = timer.reserve(run, self.limits.model_timeout)
                run = save(run)
                self._checkpoint('before-model', run)
                allowance = min(allowance, timer.remaining(run))
                with operation('model'), span('model.decide',values={'run.id':run.run_id}):
                    decision = await asyncio.wait_for(
                        self._decide(run,identity), allowance)
                timer.charge(run)
                run.pending_time_seconds = 0.0
                timer.remaining(run)
                if isinstance(decision, FinalAnswer):
                    if type(decision.text) is not str or not decision.text.strip() or len(decision.text) > 4000:
                        raise ContractError('INVALID_MODEL_OUTPUT')
                    if self.finalizer is not None:
                        decision, run.assessment = self.finalizer(deepcopy(run))
                    if self.decision_finalizer is not None:
                        decision, run.assessment = self.decision_finalizer(deepcopy(run), decision)
                    run.answer, run.status, run.checkpoint = decision.text, RunState.COMPLETED, 'READY'
                    return save(run)
                if not isinstance(decision, ToolCall):
                    raise ContractError('INVALID_MODEL_OUTPUT')
                if decision.name not in self.tools:
                    raise ContractError('UNKNOWN_TOOL')
                with span('policy.authorize',values={'run.id':run.run_id,'tool.name':decision.name}):
                    self.policy.authorize(identity, decision)
                call = ToolCall(decision.name, tool_arguments(decision.name, decision.arguments), decision.call_id)
                if run.tool_calls >= self.limits.tool_calls:
                    raise ContractError('BUDGET_EXCEEDED')
                run.tool_calls += 1
                run.pending_call, run.checkpoint = call, 'TOOL_PENDING'
                run = save(run)
        except asyncio.CancelledError:
            timer.charge(run)
            run.pending_time_seconds = 0.0
            run.status, run.outcome_code = RunState.CANCELLED, 'CANCELLED'
            save(run)
            raise
        except (ContractError, TimeoutError) as error:
            timer.charge(run)
            run.pending_time_seconds = 0.0
            run.status = RunState.FAILED
            run.outcome_code = (error.code if isinstance(error, ContractError) else
                                'RUN_DEADLINE_EXCEEDED' if run.active_seconds >= self.limits.active_timeout
                                else 'UPSTREAM_TIMEOUT')
            return save(run)
        except Exception:
            timer.charge(run)
            run.pending_time_seconds = 0.0
            run.status, run.outcome_code = RunState.FAILED, 'INTERNAL_ERROR'
            return save(run)
