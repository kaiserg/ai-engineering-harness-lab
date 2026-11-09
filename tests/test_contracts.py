from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from harness.contracts import (ContractError, Identity, Lease, Operation, Proposal, Run,
                               RunState, issue_arguments)
from harness.fakes import MemoryRunStore


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.identity = Identity('operator-demo', 'workspace-alpha', frozenset({'operator'}), frozenset({'DEV'}))
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.approver = replace(self.identity, actor_id='approver-demo', roles=frozenset({'approver'}))

    def proposal(self, **changes):
        kwargs = dict(requested_by=self.identity.actor_id, workspace_id=self.identity.workspace_id,
                      tool='jenkins_request_tests', resource='job-tests', arguments={'revision': 'revision-A'},
                      revision='revision-A', expires_at=self.now + timedelta(minutes=30))
        kwargs.update(changes)
        return Proposal.create(**kwargs)

    def test_invalid_transitions_and_terminal_updates_rejected(self):
        store = MemoryRunStore()
        run = store.create(Run(self.identity.workspace_id, self.identity.actor_id), self.identity)
        run.status = RunState.COMPLETED
        with self.assertRaises(ContractError):
            store.save(run, self.identity)
        self.assertEqual(store.load(run.run_id, self.identity).status, RunState.QUEUED)
        run.status = RunState.CANCELLED
        run = store.save(run, self.identity)
        with self.assertRaises(ContractError):
            store.save(run, self.identity)

    def test_stale_version_cannot_overwrite_and_returned_objects_are_copies(self):
        store = MemoryRunStore()
        run = store.create(Run(self.identity.workspace_id, self.identity.actor_id), self.identity)
        stale = store.load(run.run_id, self.identity)
        run.status = RunState.RUNNING
        saved = store.save(run, self.identity)
        with self.assertRaises(ContractError) as error:
            store.save(stale, self.identity)
        self.assertEqual(error.exception.code, 'CONFLICT')
        saved.results.append('external mutation')
        self.assertEqual(store.load(run.run_id, self.identity).results, [])

    def test_argument_schema_rejects_types_and_additional_properties(self):
        for arguments in ({}, {'issue_key': True}, {'issue_key': '../file'},
                          {'issue_key': 'DEV-123', 'workspace': 'other'}, []):
            with self.subTest(arguments=arguments), self.assertRaises(ContractError):
                issue_arguments(arguments)

    def test_approval_hash_binds_revision_payload_and_expiration(self):
        original = self.proposal()
        for changed in (self.proposal(revision='revision-B'),
                        self.proposal(arguments={'revision': 'revision-B'}),
                        self.proposal(expires_at=self.now + timedelta(minutes=5))):
            self.assertNotEqual(original.payload_hash, changed.payload_hash)
            with self.assertRaises(ContractError):
                original.approve(self.approver, changed.payload_hash, 0, self.now)
        approved = original.approve(self.approver, original.payload_hash, 0, self.now)
        self.assertEqual(approved.status, 'APPROVED')
        with self.assertRaises(ContractError):
            approved.approve(self.approver, original.payload_hash, approved.version, self.now)

    def test_autoapproval_other_workspace_expiry_and_version_are_rejected(self):
        proposal = self.proposal()
        for actor in (replace(self.identity, roles=frozenset({'approver'})),
                      replace(self.approver, workspace_id='workspace-beta')):
            with self.assertRaises(ContractError):
                proposal.approve(actor, proposal.payload_hash, 0, self.now)
        with self.assertRaises(ContractError):
            proposal.approve(self.approver, proposal.payload_hash, 0, proposal.expires_at)
        with self.assertRaises(ContractError):
            proposal.approve(self.approver, proposal.payload_hash, 99, self.now)

    def test_proposal_snapshots_arguments_and_rejects_ambiguous_values(self):
        arguments = {'revision': 'revision-A'}
        proposal = self.proposal(arguments=arguments)
        original_hash = proposal.payload_hash
        arguments['revision'] = 'revision-B'
        self.assertEqual(proposal.payload_hash, original_hash)
        with self.assertRaises(ContractError):
            self.proposal(arguments={'ambiguous': 1.5})
        with self.assertRaises(ContractError):
            self.proposal(expires_at=datetime(2030, 1, 1))

    def test_crash_before_and_after_dispatch_preserves_operation_id(self):
        operation = Operation(str(uuid4()))
        self.assertEqual(operation.interrupted().status, 'PREPARED')
        unknown = operation.dispatch().interrupted()
        self.assertEqual(unknown.status, 'UNKNOWN')
        self.assertEqual(unknown.operation_id, operation.operation_id)
        with self.assertRaises(ContractError):
            unknown.dispatch()
        self.assertEqual(unknown.recovery_strategy(), 'MANUAL_RESOLUTION')
        self.assertEqual(unknown.recovery_strategy(reliable_lookup=True), 'LOOKUP')
        self.assertEqual(unknown.recovery_strategy(guaranteed_deduplication=True), 'RETRY_SAME_KEY')

    def test_expired_or_stale_lease_cannot_authorize_worker(self):
        lease = Lease('worker-A', 2, self.now + timedelta(seconds=10))
        lease.check('worker-A', 2, self.now)
        for owner, token, now in [('worker-A', 1, self.now), ('worker-B', 2, self.now),
                                  ('worker-A', 2, lease.expires_at)]:
            with self.assertRaises(ContractError) as error:
                lease.check(owner, token, now)
            self.assertEqual(error.exception.code, 'LEASE_LOST')
