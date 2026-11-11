"""Configurar el binding local de Jenkins sin poner el token en los argumentos."""
import argparse
from dataclasses import asdict
from getpass import getpass
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.ci_live import Binding, CONFIG
from harness.contracts import ContractError


def configure(binding, token):
    if not re.fullmatch(r'[A-Za-z0-9]{16,160}', token):
        raise ValueError('Token inválido')
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', dir=CONFIG.parent, delete=False) as handle:
        tmp = Path(handle.name)
        try:
            json.dump({**asdict(binding), 'token':token}, handle)
            handle.close()
            os.replace(tmp, CONFIG)
        finally:
            tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18080')
    parser.add_argument('--user', default='lab-reader')
    parser.add_argument('--workspace', default='workspace-alpha')
    parser.add_argument('--actor', default='reader-demo')
    parser.add_argument('--repository', default='octocat/hello-world')
    parser.add_argument('--pull-number', type=int, default=1)
    parser.add_argument('--job', default='atlas-tests')
    parser.add_argument('--token-stdin', action='store_true', help='Leer el token de una entrada privada por pipe.')
    args = parser.parse_args()
    token_stdin = vars(args).pop('token_stdin')
    args.repository = args.repository.lower()
    try:
        binding = Binding(**vars(args))
        token = sys.stdin.readline(162).strip() if token_stdin else getpass('Token Jenkins (entrada oculta): ')
        configure(binding, token)
    except (ValueError, ContractError, OSError):
        parser.exit(1, 'No se pudo guardar la configuración local.\n')
    print('Configuración guardada en data/runtime/jenkins-ci.json con permisos privados.')


if __name__ == '__main__':
    main()
