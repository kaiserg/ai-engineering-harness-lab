from dataclasses import replace
import math
import unittest
from harness.contracts import ContractError,ToolResult,knowledge_arguments
from harness.rag_agent import source,validate_result


class RAGContractTests(unittest.TestCase):
    def data(self):
        return {'query':'Atlas','limit':1,'index_id':'a'*32,'matches':[
            {'citation':'['+'a'*32+':11111111-1111-4111-8111-111111111111:0]',
             'filename':'a.md','text':'Atlas','distance':.1,'trust':'untrusted_document'}]}
    def result(self,data):return ToolResult('call',data,source(data),tool_name='knowledge_search')

    def test_query_has_no_identity_or_index_authority_fields(self):
        for extra in ('workspace_id','actor_id','index_id','roles'):
            with self.assertRaises(ContractError):knowledge_arguments({'query':'Atlas','limit':1,extra:'injected'})
        for limit in (True,0,4):
            with self.assertRaises(ContractError):knowledge_arguments({'query':'Atlas','limit':limit})

    def test_forged_citation_trust_and_nonfinite_distance_are_rejected(self):
        for field,value in [('citation','[foreign:0]'),('trust','trusted'),('distance',math.nan),('text','x'*801)]:
            data=self.data();data['matches'][0][field]=value
            with self.assertRaises(ContractError):validate_result(self.result(data))
        data=self.data();data['matches'].append(dict(data['matches'][0]));data['limit']=2
        with self.assertRaises(ContractError):validate_result(self.result(data))

    def test_correlation_to_retrieval_source_is_checked(self):
        data=self.data();result=replace(self.result(data),source_id='knowledge:forged')
        with self.assertRaises(ContractError):validate_result(result)
        data=self.data();self.assertEqual(validate_result(self.result(data)),data)
