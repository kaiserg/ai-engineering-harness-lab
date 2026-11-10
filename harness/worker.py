"""Consumidor independiente, con lease renovable y checkpoints protegidos por fencing."""
import argparse
import asyncio
import json
import os
import signal
from uuid import uuid4

from .checkpoints import RunInterrupted, pause_hook
from .composition import compose, demo_identity
from .contracts import ContractError, RunState
from .core import Limits
from .telemetry import span, annotate, configure
from .postgres_config import local_dsn
from .postgres_store import PostgresRunStore


class CheckedModel:
    def __init__(self, model, check):
        self.model, self.check = model, check

    async def decide(self, run, allowed):
        self.check()
        return await self.model.decide(run, allowed)


class CheckedTool:
    def __init__(self, tool, check):
        self.tool, self.check = tool, check

    async def invoke(self, identity, call):
        self.check()
        return await self.tool.invoke(identity, call)


class Worker:
    def __init__(self, store, *, identity=None, worker_id=None, lease_seconds=15, composer=compose, checkpoint_hook=None, identity_resolver=None):
        if store.schema=='harness_users_lab' and identity_resolver is None:
            raise ValueError('El schema de usuarios requiere resolución de identidad vigente.')
        store._duration(lease_seconds)
        self.store, self.identity = store, identity or demo_identity()
        self.worker_id, self.lease_seconds = worker_id or 'worker-' + str(uuid4()), lease_seconds
        self.composer, self.checkpoint_hook = composer, checkpoint_hook
        self.identity_resolver=identity_resolver

    async def run_once(self):
        claim = self.store.claim_next(self.worker_id, self.identity, lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        execution = asyncio.create_task(self._execute(claim))
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        try:
            done, _ = await asyncio.wait({execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if execution in done:
                return execution.result()
            heartbeat.result()  # Propagar pérdida de lease o de conexión antes de continuar.
            return await execution  # El heartbeat observó una finalización válida del propio intento.
        except ContractError as error:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            return {'event': 'lease_lost', 'run_id': claim.run_id, 'error': error.code}
        finally:
            if not execution.done():
                execution.cancel()
            heartbeat.cancel()
            await asyncio.gather(execution, heartbeat, return_exceptions=True)

    async def _heartbeat(self, claim):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            if not await asyncio.to_thread(self.store.renew, claim, self.identity, lease_seconds=self.lease_seconds):
                return

    async def _execute(self, claim):
        from .metrics import attempt
        with attempt(recovered=claim.attempts>1) as finish:
            result=await self._execute_traced(claim)
            finish(result)
            return result

    async def _execute_traced(self, claim):
        import time
        queued=claim.telemetry_enqueued_at
        values={'run.id':claim.run_id,'job.attempt':claim.attempts,'job.fencing_token':claim.fencing_token}
        if queued is not None:
            values['job.age_ms']=max(0,(time.time()-queued.timestamp())*1000)
        with span('worker.attempt',root=True,links=(claim.producer_traceparent,claim.previous_traceparent),values=values):
            try:
                self.store.record_attempt_trace(claim,self.identity)
            except Exception:
                pass  # Metadatos de observabilidad no autorizan ni bloquean ejecución.
            result=await self._execute_claim(claim)
            annotate(**{'run.outcome':str(result.get('status') or result.get('event','')).upper()})
            if result.get('error'):
                annotate(**{'error.code':result['error']})
            return result

    async def _execute_claim(self, claim):
        bound = self.store.bind(claim)
        identity=self.identity
        def check():
            self.store.assert_claim(claim,self.identity)
            if self.identity_resolver is not None:
                current=self.identity_resolver()
                if current!=identity:raise ContractError('FORBIDDEN')
        pause = pause_hook(claim.pause_at)
        def checkpoint(point, run):
            check()
            if self.checkpoint_hook:
                self.checkpoint_hook(point, run)
            pause(point, run)
        try:
            if self.identity_resolver is not None:
                identity=self.identity_resolver()
                if ((identity.actor_id,identity.workspace_id)!=(self.identity.actor_id,self.identity.workspace_id)
                        or not identity.roles & {'reader','operator'}):raise ContractError('FORBIDDEN')
            if claim.attempts > claim.max_attempts:
                raise ContractError('WORKER_ATTEMPTS_EXCEEDED')
            run = bound.load(claim.run_id, identity)
            annotate(**{'run.confirmed_results':len(run.results)})
            limits = Limits(**run.limits_snapshot) if run.limits_snapshot else Limits()
            execution = self.composer(run.execution_profile, identity, bound,
                                      limits=limits, checkpoint_hook=checkpoint)
            harness = execution.harness
            harness.llm = CheckedModel(harness.llm, check)
            harness.tools = {name: CheckedTool(tool, check) for name, tool in harness.tools.items()}
            # Reautorizar y recuperar cualquier checkpoint, incluyendo QUEUED y terminales.
            result = await harness.resume(run.run_id, identity)
            self.store.finish_terminal(claim, self.identity)
            annotate(**{'run.model_calls':result.model_calls,'run.tool_calls':result.tool_calls})
            if result.outcome_code:
                annotate(**{'error.code':result.outcome_code})
            return {'event': 'finished', 'run_id': result.run_id, 'status': result.status,
                    'attempt': claim.attempts, 'fencing_token': claim.fencing_token}
        except RunInterrupted:
            self.store.pause_claim(claim, self.identity)
            return {'event': 'paused', 'run_id': claim.run_id, 'attempt': claim.attempts}
        except ContractError as error:
            if error.code == 'LEASE_LOST':
                raise
            self._fail(bound, claim, error.code)
            return {'event': 'failed', 'run_id': claim.run_id, 'error': error.code}
        except Exception:
            self._fail(bound, claim, 'INTERNAL_ERROR')
            return {'event': 'failed', 'run_id': claim.run_id, 'error': 'INTERNAL_ERROR'}

    def _fail(self, bound, claim, code):
        run = bound.load(claim.run_id, self.identity)
        if run.status in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
            self.store.finish_terminal(claim, self.identity)
            return
        if run.status == RunState.QUEUED:
            run.status = RunState.RUNNING
            run = bound.save(run, self.identity)
        run.status, run.outcome_code = RunState.FAILED, code
        # Conservar la reserva de un intento incierto; no declararlo consumido o liberado sin evidencia.
        bound.save(run, self.identity)


async def main(args):
    configure('harness-worker')
    from .metrics import start
    start()
    store = PostgresRunStore(local_dsn(), schema=args.schema)
    user_scope=None;resolver=None
    if args.user_actor:
        from .user_identity import UserIdentityStore
        from .contracts import Identity
        directory=UserIdentityStore(PostgresRunStore(local_dsn()))
        user_scope=Identity(args.user_actor,args.user_workspace,frozenset(),frozenset())
        resolver=lambda:directory.resolve(args.user_actor,args.user_workspace)
    def crash(point, run):
        if (args.crash_at == point
                and (point != 'after-result' or len(run.results) == args.after_results)
                and (point != 'before-tool' or args.tool is None or run.pending_call.name == args.tool)):
            print(json.dumps({'event': 'simulated_crash', 'run_id': run.run_id, 'checkpoint': run.checkpoint}), flush=True)
            os._exit(2)  # Solo simulación explícita: no ejecutar cleanup para probar recuperación real.
    worker = Worker(store, worker_id=args.worker_id, lease_seconds=args.lease_seconds, checkpoint_hook=crash,identity=user_scope,identity_resolver=resolver)
    stopping = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopping.set)
    print(json.dumps({'event': 'worker_ready', 'worker_id': worker.worker_id}), flush=True)
    while not stopping.is_set():
        result = await worker.run_once()
        if result:
            print(json.dumps(result), flush=True)
        if args.once or stopping.is_set():
            return
        if result is None:
            try:
                await asyncio.wait_for(stopping.wait(), timeout=0.5)
            except TimeoutError:
                pass


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--schema')
    parser.add_argument('--user-actor')
    parser.add_argument('--user-workspace')
    parser.add_argument('--worker-id')
    parser.add_argument('--lease-seconds', type=float, default=15)
    parser.add_argument('--crash-at', choices=['after-result', 'before-tool'])
    parser.add_argument('--after-results', type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument('--tool', choices=['jira_get_issue', 'github_get_pull_request', 'jenkins_get_build','knowledge_search'])
    args = parser.parse_args()
    if bool(args.user_actor)!=bool(args.user_workspace):parser.error('Indique --user-actor y --user-workspace juntos.')
    args.schema=args.schema or ('harness_users_lab' if args.user_actor else 'harness_lab')
    if args.user_actor and args.schema=='harness_lab' or not args.user_actor and args.schema=='harness_users_lab':
        parser.error('Separe el schema de runs autenticados del worker de fixtures.')
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except ContractError as error:
        print(json.dumps({'error': error.code}), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    cli()
