"""Comprobar/calentar el modelo semántico local fijado; nunca descarga ni cambia modelos."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.embeddings import MODEL,MODEL_DIGEST,DIMENSION,PROFILES,OllamaEmbedding
from harness.contracts import ContractError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['profiles','warmup'])
    args=parser.parse_args()
    if args.command=='profiles':result=PROFILES
    else:
        start=time.monotonic();vector=OllamaEmbedding().query('local embedding readiness')
        result={'model':MODEL,'digest':MODEL_DIGEST,'dimension':len(vector),
                'expected_dimension':DIMENSION,'seconds':round(time.monotonic()-start,3),'ready':True}
    print(json.dumps(result))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':error.code if isinstance(error,ContractError) else 'EMBEDDING_COMMAND_FAILED'}))
        raise SystemExit(1) from None
