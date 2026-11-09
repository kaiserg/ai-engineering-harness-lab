"""Tiempo monotónico por sesión; el acumulado y las reservas viajan en el run."""
from .contracts import ContractError


class ActiveBudget:
    def __init__(self, limit, clock):
        self.limit, self.clock = limit, clock
        self.last = clock()

    def charge(self, run):
        now = self.clock()
        run.active_seconds += max(0.0, now - self.last)
        self.last = now

    def remaining(self, run):
        self.charge(run)
        remaining = self.limit - run.active_seconds
        if remaining <= 0:
            raise ContractError('RUN_DEADLINE_EXCEEDED')
        return remaining

    def reserve(self, run, per_call_timeout):
        allowance = min(per_call_timeout, self.remaining(run))
        run.pending_time_seconds = allowance
        return allowance
