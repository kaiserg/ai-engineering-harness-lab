"""Restauración idempotente y guardas del ejercicio, sin detener servicios."""
import unittest
from unittest.mock import patch
from scripts import incident


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.done = dict(results=[{'confirmed': 'jira'}], model_calls=4, tool_calls=3,
            job={'status': 'DONE', 'attempts': 2})
        self.state = dict(run_id='synthetic', restore_worker=True,
            evidence_sha256=incident.evidence(self.done))

    def test_recovery_starts_worker_and_does_not_requeue_completed_run(self):
        with patch.object(incident, 'compose') as compose, patch.object(incident, 'api', return_value=self.done) as api, \
                patch.object(incident, 'wait', side_effect=[self.done, True]), \
                patch.object(incident, 'trace', return_value='trace'), patch.object(incident, 'atomic_json'):
            result = incident.recover(self.state)
        compose.assert_called_once_with('start', 'worker')
        api.assert_called_once_with('/runs/synthetic')
        self.assertEqual(result['phase'], 'recovered')
        self.assertFalse(result['restore_worker'])

    def test_recovery_resumes_paused_run_once(self):
        with patch.object(incident, 'compose'), patch.object(incident, 'api', return_value={'job': {'status': 'PAUSED'}}) as api, \
                patch.object(incident, 'wait', side_effect=[self.done, True]), \
                patch.object(incident, 'trace', return_value='trace'), patch.object(incident, 'atomic_json'):
            incident.recover(self.state)
        self.assertEqual(api.call_args_list[-1].args, ('/runs/synthetic/resume', {}))
        self.assertEqual(api.call_count, 2)

    def test_changed_evidence_or_budget_is_rejected(self):
        for changed in (dict(self.done, results=[{'different': True}]), dict(self.done, tool_calls=4),
                dict(self.done, job={'attempts': 3})):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                incident.check_completed(self.state, changed)

    def test_completed_journal_is_noop(self):
        with patch.object(incident, 'compose') as compose:
            incident.recover({'restore_worker': False})
        compose.assert_not_called()

    def test_worker_restored_when_alert_check_fails(self):
        paused = dict(self.done, job={'status': 'PAUSED'})
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): return self
            def fetchone(self): return {'n': 0}
        with patch.object(incident.JOURNAL.__class__, 'exists', return_value=False), \
                patch.object(incident, 'compose', return_value='worker\n'), \
                patch.object(incident.PostgresRunStore, '_migrate'), \
                patch.object(incident.PostgresRunStore, '_database', return_value=Database()), \
                patch.object(incident, 'api', return_value={'run_id': 'synthetic'}), \
                patch.object(incident, 'wait', side_effect=[paused, RuntimeError('monitor unavailable')]), \
                patch.object(incident, 'run_state', return_value=self.done), \
                patch.object(incident, 'trace', return_value='trace'), patch.object(incident, 'atomic_json'), \
                patch.object(incident, 'recover', return_value={}) as recover:
            with self.assertRaises(RuntimeError):
                incident.exercise()
        recover.assert_called_once()
