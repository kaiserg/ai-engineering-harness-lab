"""Compilar antes de ejecutar el harness; cachés y binario locales, excluidos de Git."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def go_environment():
    go = shutil.which('go') or str(ROOT / '.tools/go/bin/go')
    if not Path(go).is_file():
        raise SystemExit('Instalá Go (versión en .go-version) y volvé a ejecutar este script.')
    env = {**os.environ, 'GOPATH': str(ROOT / '.cache/go'), 'GOMODCACHE': str(ROOT / '.cache/go/mod'),
           'GOCACHE': str(ROOT / '.cache/go/build'), 'GOTOOLCHAIN': 'local'}
    return go, env


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='Ejecutar también las pruebas de Go.')
    args = parser.parse_args()
    go, env = go_environment()
    if args.test:
        subprocess.run([go, 'test', '-mod=readonly', './...'], cwd=ROOT / 'services/jenkins-mcp', env=env, check=True)
    (ROOT / 'bin').mkdir(exist_ok=True)
    subprocess.run([go, 'build', '-trimpath', '-mod=readonly', '-o', str(ROOT / 'bin/jenkins-mcp'), '.'],
                   cwd=ROOT / 'services/jenkins-mcp', env=env, check=True)
    print('Servidor preparado: bin/jenkins-mcp')
