"""PostgreSQL real: intención inmutable, decisiones atómicas y recuperación por reintento."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from threading import Barrier
import unittest
from uuid import uuid4
from psycopg import sql

from harness.composition import compose,demo_identity
from harness.contracts import ContractError,Run
from harness.postgres_config import local_dsn
from harness.postgres_store import PostgresRunStore
from harness.proposals import ProposalStore


class ProposalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='harness_it_'+uuid4().hex
        self.runs=PostgresRunStore(local_dsn(),schema=self.schema)
        self.store=ProposalStore(self.runs)
        self.operator=replace(demo_identity(),roles=frozenset({'reader','operator'}))
        self.approver=replace(self.operator,actor_id='approver-demo',roles=frozenset({'approver'}))
        profile='mcp-devops-ci-v3:stale:benign:ignore'
        execution=compose(profile,self.operator,self.runs)
        run=self.runs.create(Run(self.operator.workspace_id,self.operator.actor_id,execution_profile=profile),self.operator)
        self.run=await execution.harness.execute(run.run_id,self.operator)
        self.assertEqual(self.run.status,'COMPLETED')
        self.key=str(uuid4())
        self.item=self.store.create(self.run.run_id,self.operator,self.key)

    async def asyncTearDown(self):
        assert self.schema.startswith('harness_it_') and len(self.schema)==43
        with self.runs._database() as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))

    def decide(self,**overrides):
        args=dict(payload_hash=self.item['payload_hash'],expected_version=0,decision='APPROVED')
        args.update(overrides)
        return self.store.decide(self.item['proposal_id'],self.approver,**args)

    def code(self,expected,action):
        with self.assertRaises(ContractError) as error:action()
        self.assertEqual(error.exception.code,expected)

    async def test_restart_preserves_payload_evidence_and_read_run(self):
        reopened=ProposalStore(PostgresRunStore(local_dsn(),schema=self.schema))
        loaded=reopened.get(self.item['proposal_id'],self.approver)
        self.assertEqual(loaded,self.item)
        payload=loaded['payload']
        self.assertEqual(payload['arguments']['revision'],'revision-A')
        self.assertEqual(payload['context']['sources'],self.run.assessment['sources'])
        self.assertEqual(self.runs.load(self.run.run_id,self.operator),self.run)
        with self.runs._database() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM jobs').fetchone()['n'],0)

    async def test_create_retry_returns_same_id_hash_and_expiry(self):
        self.assertEqual(self.store.create(self.run.run_id,self.operator,self.key),self.item)
        self.code('CONFLICT',lambda:self.store.create(str(uuid4()),self.operator,self.key))

    async def test_approval_retry_after_response_loss_and_conflicting_decision(self):
        first=self.decide()
        self.store=ProposalStore(PostgresRunStore(local_dsn(),schema=self.schema))
        self.assertEqual(self.decide(),first)
        self.assertEqual((first['status'],first['version'],first['decided_by']),('APPROVED',1,'approver-demo'))
        self.code('CONFLICT',lambda:self.decide(decision='REJECTED'))

    async def test_rejection_is_terminal_and_retryable(self):
        rejected=self.decide(decision='REJECTED')
        self.assertEqual(self.decide(decision='REJECTED'),rejected)
        self.code('CONFLICT',lambda:self.decide())

    async def test_roles_autoapproval_resource_scope_and_workspace(self):
        self.code('FORBIDDEN',lambda:self.store.create(self.run.run_id,demo_identity(),str(uuid4())))
        self.code('FORBIDDEN',lambda:self.store.decide(self.item['proposal_id'],replace(self.operator,roles=frozenset({'approver'})),
            payload_hash=self.item['payload_hash'],expected_version=0,decision='APPROVED'))
        self.code('NOT_FOUND',lambda:self.store.get(self.item['proposal_id'],replace(self.approver,workspace_id='other')))
        self.code('FORBIDDEN',lambda:self.store.get(self.item['proposal_id'],replace(self.approver,jobs=frozenset())))
        self.code('FORBIDDEN',lambda:self.store.get(self.item['proposal_id'],replace(self.approver,projects=frozenset())))
        self.code('FORBIDDEN',lambda:self.store.create(self.run.run_id,replace(self.operator,projects=frozenset()),str(uuid4())))
        self.code('NOT_FOUND',lambda:self.store.get(self.item['proposal_id'],replace(self.operator,actor_id='other')))

    async def test_hash_version_validation_and_sql_payload_immutability(self):
        self.code('CONFLICT',lambda:self.decide(payload_hash='0'*64))
        self.code('CONFLICT',lambda:self.decide(expected_version=2))
        self.code('INVALID_ARGUMENT',lambda:self.decide(expected_version=True))
        with self.assertRaises(ContractError):
            with self.runs._database() as db:
                db.execute("UPDATE proposals SET canonical_payload='{}' WHERE proposal_id=%s",(self.item['proposal_id'],))
        self.assertEqual(self.store.get(self.item['proposal_id'],self.operator),self.item)

    async def test_concurrent_creation_and_competing_decisions(self):
        barrier=Barrier(2)
        fresh_key=str(uuid4())
        def create():
            barrier.wait(timeout=5)
            return self.store.create(self.run.run_id,self.operator,fresh_key)
        values=await asyncio.gather(asyncio.to_thread(create),asyncio.to_thread(create))
        self.assertEqual(values[0],values[1])
        with self.runs._database() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM proposals').fetchone()['n'],2)
        barrier=Barrier(2)
        def decide(choice):
            barrier.wait(timeout=5)
            try:return self.decide(decision=choice)['status']
            except ContractError as error:return error.code
        outcomes=await asyncio.gather(asyncio.to_thread(decide,'APPROVED'),asyncio.to_thread(decide,'REJECTED'))
        self.assertIn('CONFLICT',outcomes)
        self.assertEqual(sum(value in ('APPROVED','REJECTED') for value in outcomes),1)

    async def test_expiration_is_committed_even_when_decision_is_rejected(self):
        # Fixture temporal consistente; no esperar 30 minutos ni debilitar el reloj del servicio.
        identifier=str(uuid4())
        with self.runs._database() as db:
            row=db.execute('SELECT * FROM proposals WHERE proposal_id=%s',(self.item['proposal_id'],)).fetchone()
            past=db.execute("SELECT clock_timestamp()-interval '1 second' AS now").fetchone()['now']
            payload=json.loads(row['canonical_payload']);payload['expires_at']=past.isoformat()
            canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            row.update(proposal_id=identifier,request_key=str(uuid4()),expires_at=past,
                canonical_payload=canonical,payload_hash=sha256(canonical.encode()).hexdigest())
            db.execute(sql.SQL('INSERT INTO proposals ({}) VALUES ({})').format(
                sql.SQL(',').join(map(sql.Identifier,row)),sql.SQL(',').join(sql.Placeholder() for _ in row)),list(row.values()))
        self.item=self.store.get(identifier,self.approver)
        self.assertEqual(self.item['effective_status'],'EXPIRED')
        self.code('APPROVAL_EXPIRED',lambda:self.decide())
        self.assertEqual(self.store.get(identifier,self.approver)['status'],'EXPIRED')

    async def test_other_approver_cannot_claim_an_existing_decision(self):
        self.decide()
        other=replace(self.approver,actor_id='other-approver')
        self.code('CONFLICT',lambda:self.store.decide(self.item['proposal_id'],other,
            payload_hash=self.item['payload_hash'],expected_version=0,decision='APPROVED'))

    async def test_unfinished_run_and_other_owner_cannot_be_proposed(self):
        unfinished=self.runs.create(Run(self.operator.workspace_id,self.operator.actor_id),self.operator)
        self.code('INCOMPLETE_EVIDENCE',lambda:self.store.create(unfinished.run_id,self.operator,str(uuid4())))
        other=replace(self.operator,actor_id='other-operator')
        self.code('NOT_FOUND',lambda:self.store.create(self.run.run_id,other,str(uuid4())))
