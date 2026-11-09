"""Mantenimiento explícito y recuperable; breve parada de worker/emisor/MCP."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.postgres_config import RUNTIME
from harness.security_lifecycle import (ACTIONS, activate, atomic_json, ensure_generation,
    operation_lock, prepare_change, status, sync_directory, validate)
from scripts.stack import compose_command


def compose(*args):
    subprocess.run(compose_command() + list(args), cwd=ROOT, check=True)


def perform(action, directory=RUNTIME, runner=compose):
    journal = directory / 'security-operation.json'
    with operation_lock(directory):
        ensure_generation(directory)
        if (directory/'security-live-operation.json').exists() and action != 'status':
            raise ValueError('Recover live maintenance before coordinated operations')
        if action == 'status':
            result = status(directory)
            result['pending_operation'] = json.loads(journal.read_text()) if journal.exists() else None
            return result
        if action != 'recover' and journal.exists():
            raise ValueError('A previous operation needs recover')
        if action == 'recover':
            if not journal.exists():
                raise ValueError('No pending security operation')
            operation = json.loads(journal.read_text())
            if operation.get('action') not in ACTIONS or operation.get('phase') not in ('stopping','prepared','activated'):
                raise ValueError('Invalid security journal')
        else:
            if action not in ACTIONS:
                raise ValueError('Unknown security operation')
            current = status(directory)
            import time
            if action in ('rotate-signing-key','retire-signing-key'):
                pending = any(item['retire_at'] > time.time() for item in current['previous'])
                if pending or action == 'retire-signing-key' and not current['previous']:
                    raise ValueError('Previous key is not ready for this action')
            operation = {'action': action, 'phase': 'stopping'}
            atomic_json(journal, operation)
        # SIGTERM: termina el job actual antes de parar; la API conserva admisión durable.
        runner('stop', 'worker')
        runner('stop', 'jira-mcp', 'identity')
        if operation['phase'] == 'stopping':
            generation = prepare_change(operation['action'], directory)
            operation = {'action': operation['action'], 'phase': 'prepared', 'target': generation.name}
            atomic_json(journal, operation)
        else:
            target = operation.get('target', '')
            if len(target) != 32 or any(c not in '0123456789abcdef' for c in target):
                raise ValueError('Invalid journal target')
            generation = directory / 'security-generations' / target
            validate(generation)
        if operation['phase'] == 'prepared':
            activate(generation, directory)
            operation['phase'] = 'activated'
            atomic_json(journal, operation)
        elif ensure_generation(directory) != generation.resolve():
            raise ValueError('Active generation differs from journal')
        from harness.live_materials import publish
        publish('identity',generation,directory)
        publish('jira',generation,directory)
        # Force-recreate remonta archivos; no-deps evita reiniciar PostgreSQL o la API.
        for service in ('identity', 'jira-mcp', 'worker'):
            runner('up', '-d', '--no-build', '--no-deps', '--force-recreate', '--wait', service)
        journal.unlink()
        sync_directory(directory)
        return status(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', *ACTIONS, 'recover'])
    args = parser.parse_args()
    try:
        print(json.dumps(perform(args.action), indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError):
        print('SECURITY_MAINTENANCE_FAILED: inspect status; if pending, run scripts/security.py recover.', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
