"""Evaluation boundaries use synthetic inputs, never tune on frozen model outputs."""
import sys,unittest
from pathlib import Path
from langchain_core.messages import HumanMessage
from smart_cs.evaluation.single_agent import parse_decision,assess_decision

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from evaluate_agents import committed_create,write,read,single_tools_seen


class ScoringTests(unittest.TestCase):
    def test_rejected_tool_has_no_result(self):
        self.assertEqual(single_tools_seen([{'tool':'order_query','status':'rejected','result':None}]),set())
        self.assertEqual(single_tools_seen([{'tool':'policy_lookup','result':{'status':'answered'}}]),{'policy_lookup'})

    def test_one_new_ticket_not_total_of_three(self):
        before={'tickets':1,'executions':0,'reserved':'49.99','refunded':'100.01'}
        after={'tickets':2,'executions':1,'reserved':'149.99','refunded':'100.01'}
        receipt={'requested_amount':'100.00','funds_refunded':False}
        self.assertTrue(committed_create(before,after,receipt))
        self.assertFalse(committed_create(before,{**after,'tickets':3},receipt))
        self.assertFalse(committed_create(before,{**after,'executions':2},receipt))
        self.assertFalse(committed_create(before,{**after,'refunded':'200.01'},receipt))
        self.assertFalse(committed_create(before,after,{**receipt,'funds_refunded':True}))

    def test_message_snapshots_are_data(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'row.json';write(path,{'messages':[HumanMessage(content='Synthetic fixture')]})
            self.assertEqual(read(path)['messages'][0]['content'],'Synthetic fixture')


class LiteralBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_invented_amount_cannot_reach_tool(self):
        class Never:
            async def assess(self,*args,**kwargs):raise AssertionError('Tool must not be reached')
        decision=parse_decision('{"tool":"ticket_create","arguments":{"type":"refund","order_id":"ORDER-1","requested_amount":"100.00","summary":"x","details":"x"}}')
        with self.assertRaises(ValueError):await assess_decision(decision,Never(),{'turn_id':'x'},[HumanMessage(content='Refund ORDER-1 please')])

    async def test_invented_id_cannot_reach_tool(self):
        class Never:
            async def assess(self,*args,**kwargs):raise AssertionError('Tool must not be reached')
        decision=parse_decision('{"tool":"order_query","arguments":{"order_id":"ORDER-2"}}')
        with self.assertRaises(ValueError):await assess_decision(decision,Never(),{'turn_id':'x'},[HumanMessage(content='Query ORDER-1')])

    async def test_read_negation_cannot_reach_write_validator(self):
        class Never:
            async def assess(self,*args,**kwargs):raise AssertionError('Tool must not be reached')
        decision=parse_decision('{"tool":"ticket_create","arguments":{"type":"refund","order_id":"ORDER-1","requested_amount":"100.00","summary":"x","details":"x"}}')
        with self.assertRaises(ValueError):await assess_decision(decision,Never(),{'turn_id':'x'},[HumanMessage(content='只查询 ORDER-1，退款100元暂不办理')])


if __name__=='__main__':unittest.main()
