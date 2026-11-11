"""Conversaciones privadas: orden durable, admisión atómica y contexto reautorizado."""
from contextlib import nullcontext
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4
from .contracts import ContractError,Run
from .core import authorize_run
from .llm_reader import OllamaReader,LIMITS
from .rag_agent import KnowledgePolicy
from .telemetry import carrier
from .profiles import CI_CHAT_MODES, CI_CHAT_PROFILES, LIVE_CI_PREFIX

PROFILE='ollama-qwen3-4b-document-chat-v1'
MAX_TURNS=20
HISTORY_TURNS=3


class ChatPolicy(KnowledgePolicy):
    def history(self,run,identity):
        """Verifica dependencias transitivas; solo devuelve los tres turnos seleccionados."""
        if self.knowledge.identity()!=identity:raise ContractError('FORBIDDEN')
        selected=[];seen=set();visiting={run.run_id};count=0
        conversation=run.read_request['conversation_id']
        def check(current):
            nonlocal count
            for identifier in current.read_request['history_run_ids']:
                if identifier in visiting:raise ContractError("INVALID_CHECKPOINT")
                if identifier in seen:continue
                visiting.add(identifier);count+=1
                if count>MAX_TURNS:raise ContractError('INVALID_CHECKPOINT')
                saved=self.knowledge.runs.load(identifier,identity)
                if (saved.execution_profile!=run.execution_profile or saved.status!='COMPLETED'
                        or saved.read_request['conversation_id']!=conversation):raise ContractError('INVALID_CHECKPOINT')
                # No recurse through authorize_run; validate this turn and then walk its parents once.
                for result in saved.results:self.authorize_result(identity,result)
                check(saved)
                visiting.remove(identifier);seen.add(identifier)
        check(run)
        for identifier in run.read_request['history_run_ids']:selected.append(self.knowledge.runs.load(identifier,identity))
        return selected

    def authorize_context(self,run,identity):self.history(run,identity)


class ChatReader(OllamaReader):
    def __init__(self,policy,identity):self.policy,self.identity=policy,identity
    def question(self,run):return run.read_request['query']
    def validate_messages(self,messages):
        if len(json.dumps(messages,ensure_ascii=False).encode())>6500:raise ContractError('CONTEXT_LIMIT')
    def instruction(self,final):
        return super().instruction(final)+''' Previous messages are conversation context only, not evidence.
Resolve references such as "her email" from that context. Search for the resolved question.
Answer the LATEST user question using ONLY the CURRENT retrieved excerpts, never old answers as sources.'''
    async def history(self,run):
        turns=await asyncio.to_thread(self.policy.history,run,self.identity)
        return [message for turn in turns for message in (
            {'role':'user','content':turn.read_request['query']},
            {'role':'assistant','content':turn.answer})]


class Chats:
    def __init__(self,knowledge):
        self.knowledge,self.runs=knowledge,knowledge.runs
        with self.runs._database() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',('chat-migration:'+self.runs.schema,))
            db.execute((Path(__file__).parent/'migrations/chat_001.sql').read_text())

    def identity(self):
        identity=self.knowledge.identity()
        if not identity.roles & {'reader','operator'}:raise ContractError('FORBIDDEN')
        return identity

    def owned(self,db,identifier,identity,*,locked=False):
        row=db.execute('''SELECT * FROM conversations WHERE conversation_id=%s AND workspace_id=%s
            AND requested_by=%s'''+(' FOR UPDATE' if locked else ''),
            (identifier,identity.workspace_id,identity.actor_id)).fetchone()
        if not row:raise ContractError('NOT_FOUND')
        return row

    def create(self,key,mode="documents"):
        identity=self.identity()
        with self.runs._database() as db:
            prior=db.execute('SELECT conversation_id,created_at,execution_profile FROM conversations WHERE workspace_id=%s AND requested_by=%s AND request_key=%s',
                (identity.workspace_id,identity.actor_id,str(key))).fetchone()
            if prior:
                if conversation_mode(prior['execution_profile'])!=mode:raise ContractError('CONFLICT')
                return prior
            profile=profile_for_mode(mode,identity)
            db.execute('''INSERT INTO conversations(conversation_id,workspace_id,requested_by,request_key,execution_profile)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,requested_by,request_key) DO NOTHING''',
                (str(uuid4()),identity.workspace_id,identity.actor_id,str(key),profile))
            row=db.execute('''SELECT conversation_id,created_at,execution_profile FROM conversations
                WHERE workspace_id=%s AND requested_by=%s AND request_key=%s''',
                (identity.workspace_id,identity.actor_id,str(key))).fetchone()
            if conversation_mode(row['execution_profile'])!=mode:raise ContractError('CONFLICT')
        return row

    def recent(self):
        identity=self.identity()
        with self.runs._database() as db:return db.execute('''SELECT conversation_id,created_at,execution_profile FROM conversations
            WHERE workspace_id=%s AND requested_by=%s ORDER BY created_at DESC,conversation_id LIMIT 30''',
            (identity.workspace_id,identity.actor_id)).fetchall()

    def send(self,identifier,key,query,pause=False):
        identity=self.identity()
        if type(query) is not str or not query.strip() or len(query)>2000:raise ContractError('INVALID_QUERY')
        with self.runs._database() as db:
            conversation=self.owned(db,identifier,identity,locked=True)
            turns=db.execute('''SELECT t.*,r.payload FROM conversation_turns t JOIN runs r USING(run_id)
                WHERE conversation_id=%s ORDER BY ordinal''',(identifier,)).fetchall()
            prior=next((t for t in turns if str(t['request_key'])==str(key)),None)
            if prior:
                if prior['payload']['read_request']['query']!=query or prior['pause_after_search']!=pause:raise ContractError('CONFLICT')
                return str(prior['run_id'])
            if len(turns)>=MAX_TURNS:raise ContractError('CHAT_LIMIT')
            if turns and turns[-1]['payload']['status'] not in ('COMPLETED','FAILED','CANCELLED'):
                raise ContractError('RUN_BUSY')
            profile=conversation['execution_profile']
            limits=LIMITS
            live_ci = profile.startswith(LIVE_CI_PREFIX)
            if live_ci:
                from .ci_live import LIMITS as LIVE_LIMITS
                limits=LIVE_LIMITS
            elif profile in CI_CHAT_PROFILES:
                from .ci_chat import CI_LIMITS
                limits=CI_LIMITS
            history=[] if live_ci or profile in CI_CHAT_PROFILES else [str(t['run_id']) for t in turns if t['payload']['status']=='COMPLETED'][-HISTORY_TURNS:]
            run=Run(identity.workspace_id,identity.actor_id,execution_profile=profile,limits_snapshot=asdict(limits),
                read_request={'query':query,'conversation_id':str(identifier),'history_run_ids':history})
            policy_for(self.knowledge,run.execution_profile).authorize_context(run,identity)
            self.runs._admit(db,run,identity,job_id=str(uuid4()),pause_at='jira' if pause else 'none',max_pending=16)
            db.execute('''INSERT INTO conversation_turns(conversation_id,ordinal,request_key,run_id,pause_after_search)
                VALUES (%s,%s,%s,%s,%s)''',(identifier,len(turns)+1,str(key),run.run_id,pause))
        return run.run_id

    def run(self,identifier,run_id,*,_policy=None):
        identity=self.identity()
        with self.runs._database() as db:
            self.owned(db,identifier,identity)
            if not db.execute('SELECT 1 FROM conversation_turns WHERE conversation_id=%s AND run_id=%s',(identifier,run_id)).fetchone():
                raise ContractError('NOT_FOUND')
        run,job=self.runs.snapshot(run_id,identity)
        authorize_run(run,identity,_policy or policy_for(self.knowledge,run.execution_profile))
        with self.runs._database() as db:
            row=db.execute('SELECT previous_traceparent FROM jobs WHERE run_id=%s AND workspace_id=%s AND requested_by=%s',(run_id,identity.workspace_id,identity.actor_id)).fetchone()
        trace=carrier(row['previous_traceparent']) if row and row['previous_traceparent'] else None
        return {**asdict(run),'job':job,'trace_id':trace.split('-')[1] if trace else None}

    def view(self,identifier):
        identity=self.identity()
        with self.runs._database() as db:
            row=self.owned(db,identifier,identity)
            turns=db.execute('SELECT ordinal,run_id FROM conversation_turns WHERE conversation_id=%s ORDER BY ordinal',(identifier,)).fetchall()
        messages=[]
        policy=policy_for(self.knowledge,row['execution_profile'])
        with getattr(policy,'access_batch',nullcontext)():
            for turn in turns:
                try:
                    run=self.run(identifier,str(turn['run_id']),_policy=policy)
                    messages.append({'ordinal':turn['ordinal'],**run,'unavailable':False})
                except ContractError as error:
                    if error.code!='EVIDENCE_REVOKED':raise
                    messages.append({'ordinal':turn['ordinal'],'run_id':str(turn['run_id']),'unavailable':True})
        return {'conversation_id':str(identifier),'created_at':row['created_at'],'mode':conversation_mode(row['execution_profile']),'turns':messages,
            'max_turns':MAX_TURNS,'history_turns':0 if row['execution_profile'] in CI_CHAT_PROFILES or row['execution_profile'].startswith(LIVE_CI_PREFIX) else HISTORY_TURNS}


def conversation_mode(profile):
    from .jira_chat import PROFILE as JIRA_PROFILE
    from .jira_cloud import PREFIX
    from .github_chat import DEMO as GH_DEMO,PUBLIC as GH_PUBLIC
    if profile.startswith(LIVE_CI_PREFIX):return 'ci-live'
    if profile in CI_CHAT_PROFILES:return CI_CHAT_PROFILES[profile]
    if profile==GH_DEMO:return 'github-demo'
    if profile==GH_PUBLIC:return 'github-public'
    if profile==PROFILE:return 'documents'
    if profile==JIRA_PROFILE:return 'jira-demo'
    if profile.startswith(PREFIX):return 'jira-cloud'
    raise ContractError('CONFIG_CHANGED')


def profile_for_mode(mode,identity=None):
    from .jira_chat import PROFILE as JIRA_PROFILE
    from .jira_cloud import PREFIX,current
    from .github_chat import DEMO as GH_DEMO,PUBLIC as GH_PUBLIC
    if mode=='ci-live':
        from .ci_live import load
        binding=load()[0]
        if identity is not None:binding.authorize_identity(identity)
        return binding.profile
    if mode in CI_CHAT_MODES:return CI_CHAT_MODES[mode]
    if mode=='github-demo':return GH_DEMO
    if mode=='github-public':return GH_PUBLIC
    if mode=='documents':return PROFILE
    if mode=='jira-demo':return JIRA_PROFILE
    if mode=='jira-cloud':return PREFIX+current()
    raise ContractError('INVALID_ARGUMENT')


def policy_for(knowledge,profile):
    from .jira_chat import is_jira,JiraPolicy
    from .github_chat import PROFILES,GitHubPolicy
    if profile.startswith(LIVE_CI_PREFIX):
        from .ci_live import LivePolicy,load
        return LivePolicy(knowledge,load(profile)[0])
    if profile in CI_CHAT_PROFILES:
        from .ci_chat import CIChatPolicy
        return CIChatPolicy(knowledge)
    if profile in PROFILES:return GitHubPolicy(knowledge,profile)
    return JiraPolicy(knowledge,profile) if is_jira(profile) else ChatPolicy(knowledge)
