import json
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4
from harness.chat import ChatReader,PROFILE,ChatPolicy
from harness.contracts import Run,ContractError
from harness.storage import decode,encode,check_read_request


class ChatContractTests(unittest.TestCase):
    def request(self):return {'query':'¿Y su correo?','conversation_id':str(uuid4()),'history_run_ids':[str(uuid4())]}
    def test_context_ids_are_validated_and_preserved(self):
        run=Run('w','a',execution_profile=PROFILE,read_request=self.request())
        self.assertEqual(decode(encode(run)).read_request,run.read_request)
        for values in ([run.run_id]*2,['not-a-uuid'],[str(uuid4()) for _ in range(4)]):
            run.read_request['history_run_ids']=values
            with self.assertRaises(ContractError):check_read_request(run)

    def test_large_context_is_rejected_without_silent_truncation(self):
        reader=ChatReader(None,None)
        with self.assertRaisesRegex(ContractError,'CONTEXT_LIMIT'):reader.validate_messages([{'role':'user','content':'x'*6501}])
        reader.validate_messages([{'role':'user','content':'Atlas'}])

    def test_context_cycles_are_rejected(self):
        run=Run('w','a',execution_profile=PROFILE,read_request=self.request())
        run.read_request['history_run_ids']=[run.run_id]
        identity=object();knowledge=NS(identity=lambda:identity)
        with self.assertRaisesRegex(ContractError,'INVALID_CHECKPOINT'):ChatPolicy(knowledge).history(run,identity)


class ChatContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_reauthorized_user_assistant_pairs_are_added(self):
        turns=[Run('w','a',read_request={'query':'¿Quién coordina Atlas?'},answer='Marta')]
        policy=NS(history=lambda run,identity:turns)
        reader=ChatReader(policy,None)
        history=await reader.history(None)
        self.assertEqual(history,[{'role':'user','content':'¿Quién coordina Atlas?'},{'role':'assistant','content':'Marta'}])
        self.assertIn('not evidence',reader.instruction(True))
