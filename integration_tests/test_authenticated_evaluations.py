"""El ejecutor E2E pasa por la API y elimina su schema incluso al fallar."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch
from scripts import evaluate_authenticated as e


class AuthenticatedEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_workers_and_mcp_are_graded_and_cleaned(self):
        schemas=[];original=e.isolated_store
        @contextmanager
        def tracked():
            with original() as runs:
                schemas.append(runs.schema);yield runs
        case=e.load_cases()[0]
        with patch.object(e,'isolated_store',tracked):row=await e.evaluate(case)
        self.assertTrue(row['passed'],row['checks'])
        with original() as probe:
            with probe._database() as db:
                for schema in schemas:self.assertIsNone(db.execute('SELECT to_regnamespace(%s) AS n',(schema,)).fetchone()['n'])
        self.assertEqual(row['observed']['calls']['request'],1)
        self.assertEqual(row['observed']['effects'],1)

    async def test_cleanup_failure_never_returns_a_pass(self):
        original=e.isolated_store
        @contextmanager
        def cleanup_failed():
            with original() as runs:yield runs
            # El schema sí se limpia en la prueba; simular el fallo reportado al salir.
            raise RuntimeError('PRIVATE_SENTINEL')
        with patch.object(e,'isolated_store',cleanup_failed),patch.object(e,'load_cases',return_value=[e.load_cases()[0]]):
            report=await e.run_suite()
        self.assertEqual(report['passed'],0)
        self.assertNotIn('PRIVATE_SENTINEL',str(report))
        self.assertEqual(report['cases'][0]['error'],'EVALUATION_EXECUTION_ERROR')
