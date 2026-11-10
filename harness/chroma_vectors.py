"""Chroma REST v2 fijado a loopback: vectores sin texto y transporte acotado."""
import math
import re
import time
import httpx
from .contracts import ContractError

BASE = '/api/v2/tenants/default_tenant/databases/default_database/collections'


class ChromaVectors:
    def __init__(self, check=None):
        self.check=check

    def request(self, method, path, body=None):
        if self.check:self.check()  # Claim y autoridad vigentes antes de cada I/O remoto.
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                response = client.request(method, 'http://127.0.0.1:8012'+path, json=body)
                response.raise_for_status()
                if not response.content:return None
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise ContractError('CHROMA_UNAVAILABLE') from None

    def heartbeat(self):
        return self.request('GET', '/api/v2/heartbeat')

    def collection(self, name):
        if not re.fullmatch(r'harness_rag_[0-9a-f]{32}', name):raise ContractError('INVALID_INDEX')
        return self.request('GET', BASE+'/'+name)['id']

    def build(self, name, chunks, embed):
        deadline = time.monotonic()+30
        self.request('POST', BASE, {'name':name, 'configuration':{'hnsw':{'space':'cosine'}}})
        identifier = self.collection(name)
        batch_size=getattr(embed,'batch_size',64)
        for offset in range(0, len(chunks), batch_size):
            if time.monotonic()>=deadline:raise ContractError('INDEX_DEADLINE')
            batch = chunks[offset:offset+batch_size]
            embeddings=embed.documents([c['content'] for c in batch]) if hasattr(embed,'documents') else [embed(c['content']) for c in batch]
            if time.monotonic()>=deadline:raise ContractError('INDEX_DEADLINE')
            self.request('POST', BASE+'/'+identifier+'/upsert', {
                'ids':[c['chunk_id'] for c in batch],
                'embeddings':embeddings,
                'metadatas':[{'document_id':c['document_id']} for c in batch]})
        if time.monotonic()>=deadline:raise ContractError('INDEX_DEADLINE')
        stored = self.request('POST', BASE+'/'+identifier+'/get', {'limit':1001, 'include':[]})
        if set(stored['ids']) != {c['chunk_id'] for c in chunks}:
            raise ContractError('INCOMPLETE_INDEX')

    def query(self, name, vector, documents, limit):
        if not documents:return []
        result = self.request('POST', BASE+'/'+self.collection(name)+'/query', {
            'query_embeddings':[vector], 'n_results':limit,
            'where':{'document_id':{'$in':documents}}, 'include':['distances']})
        return self.normalize(result,limit)

    async def aquery(self,name,vector,documents,limit):
        if not documents:return []
        if not re.fullmatch(r'harness_rag_[0-9a-f]{32}',name):raise ContractError('INVALID_INDEX')
        try:
            if self.check:self.check()
            async with httpx.AsyncClient(timeout=5,trust_env=False,follow_redirects=False) as client:
                response=await client.get('http://127.0.0.1:8012'+BASE+'/'+name)
                response.raise_for_status();identifier=response.json()['id']
                if self.check:self.check()
                response=await client.post('http://127.0.0.1:8012'+BASE+'/'+identifier+'/query',json={
                    'query_embeddings':[vector],'n_results':limit,
                    'where':{'document_id':{'$in':documents}},'include':['distances']})
                response.raise_for_status()
                return self.normalize(response.json(),limit)
        except (httpx.HTTPError,ValueError,KeyError,TypeError):
            raise ContractError('CHROMA_UNAVAILABLE') from None

    @staticmethod
    def normalize(result,limit):
        try:
            ids, distances = result['ids'][0], result['distances'][0]
            if len(ids) != len(distances) or len(ids)>limit:raise ValueError()
            if any(type(i) is not str or type(d) not in (float,int) or not math.isfinite(d)
                   for i,d in zip(ids,distances)):raise ValueError()
            return list(zip(ids,distances))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ContractError('INVALID_VECTOR_RESULT') from None
