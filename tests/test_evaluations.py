"""El evaluador debe detectar regresiones; un PASS no puede venir de un grader permisivo."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from scripts import evaluate


class GraderTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(evaluate.CASES.read_text())[0]
        self.observed = dict(state='COMPLETED', code=None, assessment='VERIFIED',
            sources=['jira:DEV-123:v1','github:demo/atlas:pr:42:revision-A','jenkins:atlas-tests:101:revision-A'],
            citations=['jira:DEV-123:v1','github:demo/atlas:pr:42:revision-A','jenkins:atlas-tests:101:revision-A'],
            calls=[4,3], dispatches={'jira':1,'github':1,'jenkins':1},
            limits_preserved=True, time_budget=True, partial_truth=True, false_claim_removed=True)

    def test_grader_rejects_wrong_assessment_citation_extra_calls_and_dispatch(self):
        self.assertTrue(all(evaluate.grade(self.case, self.observed).values()))
        for key, value in [('assessment','STALE_BUILD'),('citations',['invented']),('calls',[4,4]),
                ('dispatches',{'jira':2,'github':1,'jenkins':1}),('limits_preserved',False)]:
            with self.subTest(key=key):
                changed = deepcopy(self.observed)
                changed[key] = value
                self.assertFalse(all(evaluate.grade(self.case,changed).values()))

    def test_failed_run_is_successful_evaluation_only_if_denial_is_expected(self):
        case = json.loads(evaluate.CASES.read_text())[3]
        observed = dict(self.observed,state='FAILED',code='FORBIDDEN',assessment=None,
            sources=['jira:DEV-123:v1'],citations=[],calls=[2,1],dispatches={'jira':1,'github':0,'jenkins':0})
        self.assertTrue(all(evaluate.grade(case,observed).values()))
        observed['dispatches']['jenkins'] = 1
        self.assertFalse(evaluate.grade(case,observed)['permitted_dispatches'])

    def test_recovery_rejects_repeated_jira_or_changed_evidence(self):
        case = json.loads(evaluate.CASES.read_text())[-2]
        observed = dict(self.observed,recovery_equivalent=True,jira_after_resume=0,confirmed_evidence_preserved=True)
        self.assertTrue(all(evaluate.grade(case,observed).values()))
        for key,value in [('recovery_equivalent',False),('jira_after_resume',1),('confirmed_evidence_preserved',False)]:
            with self.subTest(key=key):
                changed = dict(observed,**{key:value})
                self.assertFalse(all(evaluate.grade(case,changed).values()))

    def test_cli_nonzero_when_any_case_fails(self):
        with tempfile.TemporaryDirectory() as temporary, patch('sys.argv',['evaluate','--output',str(Path(temporary)/'report.json')]), \
                patch.object(evaluate,'run_suite',new=AsyncMock(return_value={'passed':0,'total':1})), \
                patch.object(Path,'is_file',return_value=True):
            self.assertEqual(evaluate.main(),1)
            self.assertEqual(json.loads((Path(temporary)/'report.json').read_text())['passed'],0)


class EvaluationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_mcp_sqlite_recovery_and_forbidden_case_are_graded(self):
        cases = json.loads(evaluate.CASES.read_text())
        for case in (cases[3],cases[-2]):
            with self.subTest(case=case['id']):
                row = await evaluate.evaluate(case)
                self.assertTrue(row['passed'], row['checks'])
                self.assertTrue(row['checks']['permitted_dispatches'])

    async def test_execution_error_is_failure_without_raw_exception_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset=Path(temporary)/'cases.json'
            dataset.write_text('[{"id":"synthetic"}]')
            with patch.object(evaluate,'CASES',dataset),patch.object(evaluate,'evaluate',new=AsyncMock(side_effect=RuntimeError('SECRET'))):
                report = await evaluate.run_suite()
        self.assertEqual(report['passed'],0)
        self.assertNotIn('SECRET',json.dumps(report))
        self.assertEqual(report['cases'][0]['error'],'EVALUATION_EXECUTION_ERROR')
