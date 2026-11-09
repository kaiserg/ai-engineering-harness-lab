import argparse
import asyncio
import json
import math
from dataclasses import asdict, replace
from pathlib import Path

from .contracts import ContractError, Identity, Run, RunState
from .core import Limits
from .fakes import MemoryRunStore
from .composition import compose, demo_identity
from .ownership import database_owner
from .storage import SQLiteRunStore
from .profiles import parse_ci_profile, BUILD_SCENARIOS, CONTENT_SCENARIOS, ATTACKS


def display(run, invocations=None, connections=None, adapters=None):
    limit = run.limits_snapshot.get('active_timeout')
    timed = run.timing_version == 1
    print(json.dumps({'run_id': run.run_id, 'status': run.status, 'checkpoint': run.checkpoint,
                      'version': run.version, 'model_calls': run.model_calls,
                      'tool_calls': run.tool_calls, 'saved_results': len(run.results),
                      'pending_call_id': run.pending_call.call_id if run.pending_call else None,
                      'active_seconds': round(run.active_seconds, 6) if timed else None,
                      'pending_time_seconds': round(run.pending_time_seconds, 6) if timed else None,
                      'recovered_time_seconds': round(run.recovered_time_seconds, 6) if timed else None,
                      'active_limit_seconds': limit,
                      'active_remaining_seconds': round(max(0, limit - run.active_seconds - run.pending_time_seconds), 6) if timed and limit is not None else None,
                      'adapter_invocations_this_process': invocations,
                      'mcp_connections_this_process': connections,
                      'execution_profile': run.execution_profile,
                      'assessment': run.assessment,
                      'tool_failures': [asdict(failure) for failure in run.tool_failures],
                      'tools_this_process': {name: {'invocations': adapter.invocations,
                          'connections': getattr(adapter, 'connections', None)} for name, adapter in (adapters or {}).items()},
                      'answer': run.answer, 'outcome_code': run.outcome_code},
                     indent=2, ensure_ascii=False), flush=True)


async def _main(args):
    identity = demo_identity()
    store = SQLiteRunStore(args.db) if args.command else MemoryRunStore()
    limits = Limits()
    backend = args.tool_backend or 'fake'
    workflow = args.workflow or 'jira'
    build_scenario = args.build_scenario or 'matching'
    ci_profile = 'mcp-devops-ci-v3' if args.content_scenario is not None else 'mcp-devops-ci-v2'
    content_scenario = args.content_scenario
    attack = args.attack or 'ignore'
    if args.command == 'resume':
        saved = store.load(args.run_id, identity)
        ci_config = parse_ci_profile(saved.execution_profile)
        if saved.limits_snapshot:
            limits = Limits(**saved.limits_snapshot)
        if args.tool_backend is None:
            backend = 'mcp' if (saved.execution_profile in {'mcp-jira-fixture-v1', 'mcp-devops-read-v1'}
                                or ci_config is not None) else 'fake'
        if args.workflow is None:
            workflow = ('jira-github-jenkins' if ci_config is not None else
                        'jira-github' if saved.execution_profile == 'mcp-devops-read-v1' else 'jira')
        if ci_config is not None:
            ci_profile, saved_build, saved_content, saved_attack = ci_config
            build_scenario = args.build_scenario or saved_build
            content_scenario = args.content_scenario if args.content_scenario is not None else saved_content
            attack = args.attack or saved_attack
            if args.content_scenario is not None:
                ci_profile = 'mcp-devops-ci-v3'
    if ((content_scenario is not None or args.attack is not None) and workflow != 'jira-github-jenkins'
            or args.attack is not None and content_scenario is None
            or attack != 'ignore' and content_scenario != 'injection'):
        raise ContractError('CONFIG_CHANGED')
    if args.active_timeout is not None:
        limits = replace(limits, active_timeout=args.active_timeout)
    if args.build_scenario is not None and workflow != 'jira-github-jenkins':
        raise ContractError('CONFIG_CHANGED')
    if workflow != 'jira' and backend != 'mcp':
        raise ContractError('CONFIG_CHANGED')
    profile = (f'{ci_profile}:{build_scenario}' if workflow == 'jira-github-jenkins' else
               'mcp-devops-read-v1' if workflow == 'jira-github' else
               'mcp-jira-fixture-v1' if backend == 'mcp' else 'fake-jira-v1')
    if ci_profile == 'mcp-devops-ci-v3' and workflow == 'jira-github-jenkins':
        profile += f':{content_scenario}:{attack}'
    def interrupt(point, run):
        if (point == getattr(args, 'interrupt_at', None)
                and (point != 'after-result' or len(run.results) == args.interrupt_after_results)
                and (point != 'before-tool' or not args.interrupt_tool or run.pending_call.name == args.interrupt_tool)):
            display(run, *execution.counters(), adapters=execution.adapters)
            raise SystemExit(2)
    execution = compose(profile, identity, store, limits=limits, checkpoint_hook=interrupt,
                        delay=args.demo_delay, denied=args.scenario == 'denied')
    harness = execution.harness
    if args.command == 'status':
        display(store.load(args.run_id, identity))
        return 0
    if args.command == 'resume':
        result = await harness.resume(args.run_id, identity)
    else:
        run = store.create(Run(identity.workspace_id, identity.actor_id, execution_profile=profile), identity)
        result = await harness.execute(run.run_id, identity)
    display(result, *execution.counters(), adapters=execution.adapters)
    return 0 if result.status == RunState.COMPLETED else 1


async def main(args):
    if args.command:
        with database_owner(args.db):
            return await _main(args)
    return await _main(args)


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('Debe ser un número finito mayor que cero.')
    return number


def cli():
    parser = argparse.ArgumentParser(description='Harness local con modelo y proveedores ficticios; M0/M1/M2.')
    parser.add_argument('--workflow', choices=['jira', 'jira-github', 'jira-github-jenkins'], help='Recorrido; al recuperar se conserva el guardado.')
    parser.add_argument('--content-scenario', choices=CONTENT_SCENARIOS, help='Contenido Jira no confiable de S04.')
    parser.add_argument('--attack', choices=ATTACKS, help='Respuesta simulada del modelo al contenido malicioso; solo S04.')
    parser.add_argument('--build-scenario', choices=BUILD_SCENARIOS, help='Fixture Jenkins; se conserva al recuperar.')
    parser.add_argument('--tool-backend', choices=['fake', 'mcp'], help='Adaptador de Jira; al recuperar se usa el perfil guardado.')
    parser.add_argument('--scenario', choices=['read', 'denied'], default='read')
    parser.add_argument('--db', type=Path, default=Path(__file__).parents[1] / 'data/runtime/harness.sqlite3')
    parser.add_argument('--active-timeout', type=positive_seconds, help='Presupuesto activo total en segundos (120 por defecto).')
    parser.add_argument('--demo-delay', type=positive_seconds, default=0, help='Demora artificial por llamada de cada adaptador falso.')
    commands = parser.add_subparsers(dest='command')
    start = commands.add_parser('start', help='Crear una ejecución persistida en SQLite.')
    start.add_argument('--interrupt-after-results', type=int, choices=[1, 2, 3], default=1, help='Interrumpir after-result tras el resultado 1, 2 o 3.')
    start.add_argument('--interrupt-tool', choices=['jira_get_issue', 'github_get_pull_request', 'jenkins_get_build'], help='Filtrar before-tool por herramienta.')
    start.add_argument('--interrupt-at', choices=['before-model', 'before-tool', 'after-result', 'after-tool-failure'])
    for command in ('resume', 'status'):
        subparser = commands.add_parser(command)
        subparser.add_argument('run_id')
    args = parser.parse_args()
    try:
        return asyncio.run(main(args))
    except ContractError as error:
        print(json.dumps({'error': error.code}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
