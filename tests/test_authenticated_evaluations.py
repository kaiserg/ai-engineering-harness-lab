"""El evaluador autenticado debe detectar regresiones y nunca convertir fallos en PASS."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from scripts import evaluate_authenticated as e


class AuthenticatedGraderTests(unittest.TestCase):
    def setUp(self):self.cases=e.load_cases()

    def observed(self,case):
        row={k:deepcopy(v) for k,v in case['expected'].items() if k!='claims_range'}
        row.update(dict.fromkeys(e.COMMON,True));row.update(claims=case['expected']['claims_range'][0],read_calls=[4,3])
        row.update(revoked_actor_denied=True,interruption_observed=True,review_required=True,self_review_denied=True,late_request_rejected=True,atomic_job_finish=True)
        return row

    def test_each_declared_result_and_call_count_can_fail_the_grade(self):
        for case in self.cases:
            correct=self.observed(case);self.assertTrue(all(e.grade(case,correct).values()))
            for key in case['expected']:
                if key=='claims_range':continue
                with self.subTest(case=case['id'],field=key):
                    changed=deepcopy(correct);changed[key]='WRONG'
                    self.assertFalse(e.grade(case,changed)[key])
            for action in ('request','lookup','close'):
                changed=deepcopy(correct);changed['calls'][action]+=1
                self.assertFalse(e.grade(case,changed)['calls'])

    def test_success_with_duplicate_effect_or_unbounded_claims_is_not_a_pass(self):
        case=self.cases[0]
        for key,value in (('effects',2),('claims',0),('claims',6),('read_calls',[4,4])):
            row=self.observed(case);row[key]=value
            self.assertEqual(row['operation'],'SUCCEEDED')
            self.assertFalse(all(e.grade(case,row).values()))

    def test_missing_denials_changed_evidence_or_string_flags_are_rejected(self):
        for case in self.cases:
            for key in ('scope_denied','self_approval_denied','source_preserved','request_binding','secret_free_report'):
                row=self.observed(case);row[key]='true'
                self.assertFalse(e.grade(case,row)[key])
        case=self.cases[-1]
        for key in ('review_required','self_review_denied','late_request_rejected','atomic_job_finish'):
            row=self.observed(case);row.pop(key)
            self.assertFalse(e.grade(case,row)[key])

    def test_dataset_must_be_complete_unique_and_known(self):
        bad=[[],self.cases[:-1],deepcopy(self.cases),deepcopy(self.cases)]
        bad[2][0]=bad[2][1];bad[3][0]['scenario']='arbitrary-provider'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cases.json'
            for rows in bad:
                path.write_text(json.dumps(rows))
                with patch.object(e,'CASES',path),self.assertRaises(ValueError):e.load_cases()

    def test_cli_failure_and_empty_suite_return_nonzero(self):
        for report in ({'passed':0,'total':1},{'passed':0,'total':0}):
            with tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'report.json'
                with patch('sys.argv',['eval-auth','--output',str(path)]),patch.object(e,'run_suite',new=AsyncMock(return_value=report)):
                    self.assertEqual(e.main(),1)
                self.assertEqual(json.loads(path.read_text()),report)


class AuthenticatedEvaluationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_failure_is_sanitized_and_fails_the_case(self):
        with patch.object(e,'load_cases',return_value=[e.load_cases()[0]]),patch.object(e,'evaluate',new=AsyncMock(side_effect=RuntimeError('PRIVATE_SENTINEL'))):
            report=await e.run_suite()
        self.assertEqual(report['passed'],0)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(report))
        self.assertEqual(report['cases'][0]['error'],'EVALUATION_EXECUTION_ERROR')

    async def test_bad_dataset_is_a_failure_report_not_an_empty_success(self):
        with patch.object(e,'load_cases',side_effect=ValueError('PRIVATE_SENTINEL')):
            report=await e.run_suite()
        self.assertEqual((report['passed'],report['total']),(0,1))
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(report))
