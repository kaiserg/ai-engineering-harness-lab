"""Ejecutar el evaluador real y comprobar que elimina su schema temporal."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch
from scripts import evaluate_writes as e


class WriteEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self,scenario):
        schemas=[];original=e.isolated_store
        @contextmanager
        def tracked():
            with original() as runs:
                schemas.append(runs.schema)
                yield runs
        case=next(c for c in e.load_cases() if c['scenario']==scenario)
        with patch.object(e,'isolated_store',tracked):row=await e.evaluate(case)
        self.assertTrue(row['passed'],row['checks'])
        self.assertTrue(schemas)
        # Usar un schema de pruebas propio incluso para comprobar la limpieza.
        with original() as probe:
            with probe._database() as db:
                for schema in schemas:
                    self.assertIsNone(db.execute('SELECT to_regnamespace(%s) AS namespace',(schema,)).fetchone()['namespace'])
        return row

    async def test_real_server_death_recovers_by_lookup_with_one_effect(self):
        row=await self.run_case('response-lost')
        self.assertEqual(row['observed']['calls'],{'revision':1,'request':1,'lookup':1,'close':0})
        self.assertEqual(row['observed']['effects'],1)

    async def test_manual_closure_requires_review_and_blocks_late_send(self):
        row=await self.run_case('manual-closure')
        self.assertEqual(row['observed']['effects'],0)
        self.assertTrue(row['checks']['late_request_rejected'])
        self.assertEqual(row['observed']['review'],'CONFIRMED')
