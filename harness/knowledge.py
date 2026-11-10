"""M5a: inventario autoritativo PostgreSQL y candidatos vectoriales reemplazables."""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4
from .contracts import ContractError
from .chroma_vectors import ChromaVectors
from .proposals import uuid_text

from .embeddings import LEXICAL_CONFIG,lexical,provider_for

CONFIG=LEXICAL_CONFIG
MAX_BYTES=5*1024*1024
embed=lexical  # Compatibilidad para el baseline y sus pruebas anteriores.


def digest(text):return sha256(text.encode('utf-8')).hexdigest()


def split(text):
    return [text[start:start+800] for start in range(0,len(text),700) if text[start:start+800].strip()]


def validate(filename, text, readers):
    if (type(filename) is not str or not re.fullmatch(r'[\w .-]{1,120}\.(txt|md)',filename)
        or filename.startswith('.') or '/' in filename or '\\' in filename
        or type(text) is not str or not text.strip() or '\x00' in text
        or len(text.encode('utf-8'))>MAX_BYTES
        or type(readers) is not list or len(readers)>20
        or any(type(r) is not str or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',r) for r in readers)):
        raise ContractError('INVALID_DOCUMENT')
    if not any(ch.isalnum() for ch in text):raise ContractError('EMPTY_TEXT')


class Knowledge:
    def __init__(self, runs, authenticate, vectors=None, *, configuration=CONFIG, check=None):
        self.runs, self.authenticate = runs, authenticate
        self.configuration=configuration
        self.embedding=provider_for(configuration,check)
        self.vectors = vectors if vectors is not None else ChromaVectors()
        with runs._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('knowledge:'+runs.schema,))
            db.execute('CREATE TABLE IF NOT EXISTS knowledge_schema_version (version integer PRIMARY KEY)')
            row = db.execute('SELECT version FROM knowledge_schema_version').fetchone()
            if row and row['version'] not in (1,2,3):raise ContractError('STORAGE_SCHEMA_UNSUPPORTED')
            if not row:
                db.execute((Path(__file__).parent/'migrations/knowledge_001.sql').read_text())
                db.execute('INSERT INTO knowledge_schema_version VALUES (1)')
            if not row or row['version']==1:
                db.execute((Path(__file__).parent/'migrations/knowledge_002.sql').read_text())
                db.execute('UPDATE knowledge_schema_version SET version=2')
            if not row or row['version'] in (1,2):
                db.execute((Path(__file__).parent/'migrations/knowledge_003.sql').read_text())
                db.execute('UPDATE knowledge_schema_version SET version=3')

    def identity(self, write=False):
        identity = self.authenticate()  # Revalida sesión/grants; nunca acepta roles del documento.
        if not identity.roles.intersection({'operator'} if write else {'operator','reader'}):
            raise ContractError('FORBIDDEN')
        return identity

    @staticmethod
    def inventory(db, workspace):
        db.execute('INSERT INTO knowledge_inventory(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING',(workspace,))
        return db.execute('SELECT * FROM knowledge_inventory WHERE workspace_id=%s FOR UPDATE',(workspace,)).fetchone()

    def register(self, filename, text, readers=None, *, request_key=None):
        readers = [] if readers is None else readers
        validate(filename,text,readers)
        request_key=uuid_text(request_key) if request_key is not None else None
        payload_hash=digest(json.dumps([filename,text,sorted(set(readers))],ensure_ascii=False))
        identity = self.identity(True)
        identifier = str(uuid4())
        with self.runs._database() as db:
            self.inventory(db,identity.workspace_id)
            if request_key:
                previous=db.execute('''SELECT u.payload_hash,d.document_id,d.state FROM knowledge_uploads u
                    JOIN knowledge_documents d USING(document_id) WHERE u.workspace_id=%s AND u.requested_by=%s AND u.request_key=%s''',
                    (identity.workspace_id,identity.actor_id,request_key)).fetchone()
                if previous:
                    if previous['payload_hash']!=payload_hash:raise ContractError('CONFLICT')
                    return {'document_id':previous['document_id'],'state':previous['state'],'searchable_after':'rebuild'}
            rows = db.execute("SELECT content FROM knowledge_documents WHERE workspace_id=%s AND state='ACTIVE'",(identity.workspace_id,)).fetchall()
            if len(rows)>=20 or sum(len(split(r['content'])) for r in rows)+len(split(text))>1000:
                raise ContractError('KNOWLEDGE_QUOTA')
            db.execute('INSERT INTO knowledge_documents(document_id,workspace_id,owner,filename,content,content_hash,readers,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
                       (identifier,identity.workspace_id,identity.actor_id,filename,text,digest(text),sorted(set(readers)),'ACTIVE'))
            if request_key:
                db.execute('INSERT INTO knowledge_uploads VALUES (%s,%s,%s,%s,%s)',
                    (identity.workspace_id,identity.actor_id,request_key,identifier,payload_hash))
            db.execute('UPDATE knowledge_inventory SET revision=revision+1 WHERE workspace_id=%s',(identity.workspace_id,))
        return {'document_id':identifier,'state':'ACTIVE','searchable_after':'rebuild'}

    def retire(self, identifier):
        identity = self.identity(True)
        with self.runs._database() as db:
            self.inventory(db,identity.workspace_id)
            row = db.execute('SELECT state FROM knowledge_documents WHERE document_id=%s AND workspace_id=%s AND owner=%s FOR UPDATE',
                             (identifier,identity.workspace_id,identity.actor_id)).fetchone()
            if not row:raise ContractError('NOT_FOUND')
            if row['state']=='ACTIVE':
                db.execute("UPDATE knowledge_documents SET state='RETIRED' WHERE document_id=%s",(identifier,))
                db.execute('UPDATE knowledge_inventory SET revision=revision+1 WHERE workspace_id=%s',(identity.workspace_id,))
        return {'document_id':identifier,'state':'RETIRED'}

    def documents(self):
        identity = self.identity()
        with self.runs._database() as db:
            return db.execute("""SELECT d.document_id,d.filename,d.state,d.created_at,
                (d.owner=%s AND %s) AS can_retire,
                (d.state='ACTIVE' AND EXISTS(SELECT 1 FROM knowledge_inventory i JOIN knowledge_chunks c
                    ON c.index_id=i.active_index WHERE i.workspace_id=d.workspace_id AND c.document_id=d.document_id)) AS indexed
                FROM knowledge_documents d WHERE d.workspace_id=%s AND (d.owner=%s OR %s=ANY(d.readers))
                ORDER BY d.created_at DESC,d.document_id LIMIT 100""",
                (identity.actor_id,'operator' in identity.roles,identity.workspace_id,identity.actor_id,identity.actor_id)).fetchall()

    def rebuild(self, *, guard=None):
        identity = self.identity(True)
        identifier = uuid4().hex
        collection = 'harness_rag_'+identifier
        with self.runs._database() as db:
            if guard:guard(db)
            inventory = self.inventory(db,identity.workspace_id)
            # Este operador construye todo el workspace, pero no recibe texto de otros dueños.
            documents = db.execute("SELECT * FROM knowledge_documents WHERE workspace_id=%s AND state='ACTIVE' ORDER BY document_id",(identity.workspace_id,)).fetchall()
            chunks = []
            for document in documents:
                if digest(document['content'])!=document['content_hash']:raise ContractError('CORRUPT_STATE')
                for ordinal, content in enumerate(split(document['content'])):
                    chunks.append({'chunk_id':document['document_id']+':'+str(ordinal),
                                   'document_id':document['document_id'],'ordinal':ordinal,'content':content})
            if not chunks:raise ContractError('EMPTY_INVENTORY')
            if len(chunks)>1000:raise ContractError('KNOWLEDGE_QUOTA')
            db.execute('INSERT INTO knowledge_indexes(index_id,workspace_id,revision,configuration,state,collection_name) VALUES (%s,%s,%s,%s,%s,%s)',
                       (identifier,identity.workspace_id,inventory['revision'],self.configuration,'BUILDING',collection))
            for chunk in chunks:
                db.execute('INSERT INTO knowledge_chunks VALUES (%s,%s,%s,%s,%s,%s)',
                           (identifier,chunk['chunk_id'],chunk['document_id'],chunk['ordinal'],chunk['content'],digest(chunk['content'])))
            if guard:guard(db,index_id=identifier)
        try:
            self.identity(True)
            self.vectors.build(collection,chunks,self.embedding)  # Ninguna transacción PG permanece abierta durante HTTP.
            current = self.identity(True)
            if (current.workspace_id,current.actor_id)!=(identity.workspace_id,identity.actor_id):raise ContractError('FORBIDDEN')
            with self.runs._database() as db:
                if guard:guard(db)
                now = self.inventory(db,identity.workspace_id)
                stale = now['revision'] != inventory['revision']
                db.execute('UPDATE knowledge_indexes SET state=%s WHERE index_id=%s',('STALE' if stale else 'READY',identifier))
                if not stale:
                    db.execute('UPDATE knowledge_inventory SET active_index=%s WHERE workspace_id=%s',(identifier,identity.workspace_id))
                    if guard:guard(db,index_id=identifier,complete=True)
            if stale:raise ContractError('STALE_INDEX')
        except Exception:
            with self.runs._database() as db:
                db.execute("UPDATE knowledge_indexes SET state='FAILED' WHERE index_id=%s AND state='BUILDING'",(identifier,))
            raise
        return {'index_id':identifier,'state':'READY','chunks':len(chunks),'configuration':self.configuration}

    def prepare_search(self, query, limit=3):
        if type(query) is not str or len(query)>2000 or type(limit) is not int or not 1<=limit<=5:
            raise ContractError('INVALID_QUERY')
        if not query.strip():raise ContractError('EMPTY_TEXT')
        identity = self.identity()
        with self.runs._database() as db:
            index = db.execute('''SELECT k.* FROM knowledge_indexes k JOIN knowledge_inventory i
                ON i.active_index=k.index_id WHERE i.workspace_id=%s AND k.state='READY' ''',(identity.workspace_id,)).fetchone()
            if not index:return {'index_id':None,'matches':[]}
            provider_for(index['configuration'])  # Rechazar espacios desconocidos; no inferir dimensiones.
            eligible = db.execute("SELECT document_id FROM knowledge_documents WHERE workspace_id=%s AND state='ACTIVE' AND (owner=%s OR %s=ANY(readers))",
                                  (identity.workspace_id,identity.actor_id,identity.actor_id)).fetchall()
        return identity,index,[r['document_id'] for r in eligible]

    def search(self,query,limit=3):
        prepared=self.prepare_search(query,limit)
        if isinstance(prepared,dict):return prepared
        identity,index,documents=prepared
        if not documents:return self.finish_search(identity,index,[])
        vector=provider_for(index['configuration']).query(query)
        candidates=self.vectors.query(index['collection_name'],vector,documents,limit)
        return self.finish_search(identity,index,candidates)

    async def asearch(self,query,limit=3):
        prepared=await asyncio.to_thread(self.prepare_search,query,limit)
        if isinstance(prepared,dict):return prepared
        identity,index,documents=prepared
        if not documents:return await asyncio.to_thread(self.finish_search,identity,index,[])
        vector=await provider_for(index['configuration']).aquery(query)
        candidates=await self.vectors.aquery(index['collection_name'],vector,documents,limit)
        return await asyncio.to_thread(self.finish_search,identity,index,candidates)

    def finish_search(self,identity,index,candidates):
        current = self.identity()
        if (current.workspace_id,current.actor_id)!=(identity.workspace_id,identity.actor_id):raise ContractError('FORBIDDEN')
        matches = []
        with self.runs._database() as db:
            # Un cambio de puntero invalida todo el resultado; el cliente puede repetir.
            inventory = db.execute('SELECT active_index FROM knowledge_inventory WHERE workspace_id=%s',(identity.workspace_id,)).fetchone()
            if inventory['active_index']!=index['index_id']:raise ContractError('STALE_INDEX')
            for chunk_id, distance in candidates:
                row = db.execute('''SELECT c.*,d.filename FROM knowledge_chunks c JOIN knowledge_documents d USING(document_id)
                    WHERE c.index_id=%s AND c.chunk_id=%s AND d.workspace_id=%s AND d.state='ACTIVE'
                    AND (d.owner=%s OR %s=ANY(d.readers))''',
                    (index['index_id'],chunk_id,identity.workspace_id,identity.actor_id,identity.actor_id)).fetchone()
                if not row:continue  # Chroma no tiene autoridad para conceder acceso.
                if digest(row['content'])!=row['content_hash']:raise ContractError('CORRUPT_STATE')
                matches.append({'citation':f"[{index['index_id']}:{chunk_id}]",'filename':row['filename'],
                                'text':row['content'],'distance':distance,'trust':'untrusted_document'})
        return {'index_id':index['index_id'],'matches':matches}
