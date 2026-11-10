"""Operar únicamente Chroma; conserva contexto, servicios y volumen del laboratorio."""
import argparse
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.stack import compose_command
from harness.chroma_vectors import ChromaVectors
from harness.contracts import ContractError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['up','status','stop'])
    command=parser.parse_args().command
    base=compose_command()+['--profile','knowledge']
    if command=='stop':subprocess.run(base+['stop','chroma'],check=True);return
    if command=='up':
        subprocess.run(base+['up','-d','--no-deps','chroma'],check=True)
        deadline=time.monotonic()+30
        while True:
            try:ChromaVectors().heartbeat();break
            except ContractError:
                if time.monotonic()>=deadline:raise
                time.sleep(.5)
    print({'chroma':'ready','heartbeat':ChromaVectors().heartbeat(),'url':'http://127.0.0.1:8012'})


if __name__=='__main__':
    try:main()
    except Exception:
        print('No se pudo completar la operación de Chroma.',file=sys.stderr)
        raise SystemExit(1) from None
