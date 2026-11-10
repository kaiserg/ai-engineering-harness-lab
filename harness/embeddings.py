"""Dos espacios vectoriales explícitos: baseline léxica y modelo semántico local fijado."""
from hashlib import sha256
import math
import re
import unicodedata
import httpx
from .contracts import ContractError

LEXICAL_CONFIG='lexical-sha256-v1:256:cosine:chunk800-overlap100'
MODEL='qwen3-embedding:4b'
MODEL_DIGEST='df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907'
DIMENSION=2560
INSTRUCTION='Instruct: Given a question, retrieve relevant passages that answer the question\nQuery: '
SEMANTIC_CONFIG=f'ollama:{MODEL}@sha256:{MODEL_DIGEST}:{DIMENSION}:cosine:instruct-v1:chunk800-overlap100'
PROFILES={'lexical':LEXICAL_CONFIG,'semantic':SEMANTIC_CONFIG}
BASE='http://127.0.0.1:11434'


def lexical(text):
    words=re.findall(r'[a-z0-9]+',unicodedata.normalize('NFKD',text.casefold()).encode('ascii','ignore').decode())
    vector=[0.0]*256
    for word in words:vector[int(sha256(word.encode()).hexdigest()[:8],16)%256]+=1
    norm=math.sqrt(sum(v*v for v in vector))
    if not norm:raise ContractError('EMPTY_TEXT')
    return [v/norm for v in vector]


class LexicalEmbedding:
    configuration=LEXICAL_CONFIG
    dimension=256
    def __call__(self,text):return lexical(text)
    def documents(self,texts):return [lexical(text) for text in texts]
    def query(self,text):return lexical(text)
    async def aquery(self,text):return lexical(text)


class OllamaEmbedding:
    configuration=SEMANTIC_CONFIG
    dimension=DIMENSION
    batch_size=8
    def __init__(self,check=None):self.check=check
    def _check(self):
        if self.check:self.check()
    @staticmethod
    def verify(tags):
        try:match=next(m for m in tags['models'] if m['name']==MODEL)
        except (KeyError,TypeError,StopIteration):raise ContractError('EMBEDDING_MODEL_UNAVAILABLE') from None
        if match.get('digest')!=MODEL_DIGEST:raise ContractError('EMBEDDING_MODEL_CHANGED')
    @staticmethod
    def vectors(payload,count):
        try:
            vectors=payload['embeddings']
            if payload['model']!=MODEL or type(vectors) is not list or len(vectors)!=count:raise ValueError()
            for vector in vectors:
                if (type(vector) is not list or len(vector)!=DIMENSION
                        or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector)):raise ValueError()
                norm=math.sqrt(sum(v*v for v in vector))
                if not .99<=norm<=1.01:raise ValueError()
            return vectors
        except (KeyError,TypeError,ValueError):raise ContractError('INVALID_EMBEDDING') from None
    @staticmethod
    def body(texts):return {'model':MODEL,'input':texts,'truncate':False,'keep_alive':'10m'}
    def _request(self,client,method,path,body=None):
        self._check()
        response=client.request(method,BASE+path,json=body);response.raise_for_status()
        return response.json()
    def _batch(self,texts):
        try:
            with httpx.Client(timeout=8,trust_env=False,follow_redirects=False) as client:
                self.verify(self._request(client,'GET','/api/tags'))
                payload=self._request(client,'POST','/api/embed',self.body(texts))
                self.verify(self._request(client,'GET','/api/tags'))
                self._check()
                return self.vectors(payload,len(texts))
        except (httpx.HTTPError,ValueError):raise ContractError('EMBEDDING_UNAVAILABLE') from None
    def documents(self,texts):
        vectors=[]
        for start in range(0,len(texts),8):vectors.extend(self._batch(texts[start:start+8]))
        return vectors
    def __call__(self,text):return self.documents([text])[0]
    def query(self,text):return self._batch([INSTRUCTION+text])[0]
    async def aquery(self,text):
        try:
            async with httpx.AsyncClient(timeout=8,trust_env=False,follow_redirects=False) as client:
                async def request(method,path,body=None):
                    self._check();response=await client.request(method,BASE+path,json=body)
                    response.raise_for_status();return response.json()
                self.verify(await request('GET','/api/tags'))
                payload=await request('POST','/api/embed',self.body([INSTRUCTION+text]))
                self.verify(await request('GET','/api/tags'))
                self._check()
                return self.vectors(payload,1)[0]
        except (httpx.HTTPError,ValueError):raise ContractError('EMBEDDING_UNAVAILABLE') from None


def provider_for(configuration,check=None):
    if configuration==LEXICAL_CONFIG:return LexicalEmbedding()
    if configuration==SEMANTIC_CONFIG:return OllamaEmbedding(check)
    raise ContractError('INCOMPATIBLE_INDEX')
