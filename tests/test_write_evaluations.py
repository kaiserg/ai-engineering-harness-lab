"""Un grader de seguridad debe rechazar éxitos aparentes y reportar fallos del entorno."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
from scripts import evaluate_writes as e


class WriteGraderTests(unittest.TestCase):
    def setUp(self):self.cases=e.load_cases()

    def observed(self,case):
        result=deepcopy(case['expected'])|dict.fromkeys(e.FLAGS,True)|{'read_calls':[4,3]}
        if case['scenario'] in ('after-send','manual-closure'):result['interruption_observed']=True
        if case['scenario']=='manual-closure':result.update(review_required=True,self_review_denied=True,late_request_rejected=True)
        return result

    def test_each_expected_field_can_fail_the_grade(self):
        for case in self.cases:
            valid=self.observed(case)
            self.assertTrue(all(e.grade(case,valid).values()))
            for key,value in case['expected'].items():
                with self.subTest(case=case['id'],field=key):
                    changed=deepcopy(valid)
                    changed[key]=value+1 if type(value) is int else {'wrong':1} if type(value) is dict else ['WRONG'] if type(value) is list else 'WRONG'
                    self.assertFalse(e.grade(case,changed)[key])

    def test_success_does_not_hide_extra_send_effect_or_missing_lookup(self):
        case=next(c for c in self.cases if c['scenario']=='after-send')
        for key,value in [('effects',2),('claims',3),('calls',{'revision':1,'request':2,'lookup':0,'close':0})]:
            observed=self.observed(case);observed[key]=value
            self.assertEqual(observed['operation'],'SUCCEEDED')
            self.assertFalse(all(e.grade(case,observed).values()))

    def test_missing_flags_changed_read_budget_and_false_strings_fail(self):
        case=self.cases[0]
        for key in sorted(e.FLAGS):
            for value in (None,False,'true'):
                observed=self.observed(case);observed[key]=value
                self.assertFalse(e.grade(case,observed)[key])
        observed=self.observed(case);observed['read_calls']=[4,4]
        self.assertFalse(e.grade(case,observed)['exact_read_calls'])

    def test_absence_is_not_a_confirmed_manual_resolution(self):
        case=next(c for c in self.cases if c['scenario']=='manual-closure')
        for key,value in [('review',None),('review_required',False),('self_review_denied',False),('late_request_rejected',False)]:
            observed=self.observed(case);observed[key]=value
            self.assertFalse(all(e.grade(case,observed).values()))

    def test_dataset_must_be_complete_unique_and_known(self):
        malformed=[[],self.cases[:-1],self.cases+[self.cases[0]],deepcopy(self.cases)]
        malformed[-1][0]['scenario']='arbitrary-provider'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cases.json'
            for value in malformed:
                path.write_text(json.dumps(value))
                with patch.object(e,'CASES',path),self.assertRaises(ValueError):e.load_cases()

    def test_isolation_cleans_generated_schema_even_after_exception(self):
        runs=MagicMock()
        with patch.object(e,'local_dsn',return_value='unused'),patch.object(e,'PostgresRunStore',return_value=runs) as factory:
            with self.assertRaisesRegex(RuntimeError,'original'):
                with e.isolated_store():raise RuntimeError('original')
        schema=factory.call_args.kwargs['schema']
        self.assertRegex(schema,r'^harness_eval_[0-9a-f]{32}$')
        command=runs._database.return_value.__enter__.return_value.execute.call_args.args[0].as_string()
        self.assertIn(schema,command);self.assertNotIn('harness_lab',command)

    def test_cli_failure_or_empty_suite_never_returns_success(self):
        for report in ({'passed':0,'total':1},{'passed':0,'total':0}):
            with tempfile.TemporaryDirectory() as directory:
                output=Path(directory)/'report.json'
                with patch('sys.argv',['evaluate-writes','--output',str(output)]),patch.object(e,'run_suite',new=AsyncMock(return_value=report)):
                    self.assertEqual(e.main(),1)
                self.assertEqual(json.loads(output.read_text()),report)


class WriteEvaluationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_failure_is_not_skip_or_raw_exception(self):
        with patch.object(e,'load_cases',return_value=[e.load_cases()[0]]),patch.object(e,'evaluate',new=AsyncMock(side_effect=RuntimeError('PRIVATE_SENTINEL'))):
            report=await e.run_suite()
        self.assertEqual(report['passed'],0)
        self.assertEqual(report['cases'][0]['error'],'EVALUATION_EXECUTION_ERROR')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(report))

    async def test_bad_dataset_is_a_reported_failure(self):
        with patch.object(e,'load_cases',side_effect=ValueError('PRIVATE_SENTINEL')):
            report=await e.run_suite()
        self.assertEqual((report['passed'],report['total']),(0,1))
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(report))
