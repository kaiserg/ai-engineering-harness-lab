"""Investigación de CI de demostración, con evidencia de tres servidores MCP."""
import re

from .contracts import ContractError, FinalAnswer, result_call
from .core import Limits
from .evidence import finalize_ci_partial
from .fakes import CIScriptedLLM, ReadOnlyPolicy

CI_LIMITS = Limits(model_calls=4, tool_calls=3)
TICKET = 'DEV-123'
NEEDS_TICKET = 'Indicá DEV-123 en tu pregunta para consultar el ticket de demostración.'


def has_ticket(run):
    keys = re.findall(r'(?<![\w-])[A-Z][A-Z0-9]*-[0-9]+(?![\w-])', run.read_request['query'].upper())
    return set(keys) == {TICKET}


class CIChatPlanner(CIScriptedLLM):
    async def decide(self, run, allowed_tools):
        if not has_ticket(run):
            return FinalAnswer(NEEDS_TICKET)
        return await super().decide(run, allowed_tools)


class CIChatPolicy(ReadOnlyPolicy):
    def __init__(self, knowledge):
        self.knowledge = knowledge

    def authorize_context(self, run, identity):
        if self.knowledge.identity() != identity:
            raise ContractError('FORBIDDEN')
        if run.read_request['history_run_ids']:
            raise ContractError('INVALID_CHECKPOINT')
        for result in run.results:
            self.authorize_result(identity, result)

    def authorize(self, identity, call):
        if self.knowledge.identity() != identity:
            raise ContractError('FORBIDDEN')
        super().authorize(identity, call)

    def authorize_result(self, identity, result):
        try:
            self.authorize(identity, result_call(result))
        except ContractError as error:
            if error.code == 'FORBIDDEN':
                raise ContractError('EVIDENCE_REVOKED') from None
            raise


def finish(run):
    if not has_ticket(run):
        if run.results or run.tool_failures:
            raise ContractError('EVIDENCE_MISMATCH')
        return FinalAnswer(NEEDS_TICKET), {'status': 'NEEDS_TICKET', 'sources': []}
    return finalize_ci_partial(run)
