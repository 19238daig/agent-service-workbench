import unittest
from smart_cs.evaluation.quality import intent_metrics,aggregate
from smart_cs.evaluation.single_agent import parse_decision


class QualityMetricsTests(unittest.TestCase):
    def test_allowed_alternatives_excluded_only_from_single_label_f1(self):
        rows=[{'status':'evaluated','expected_route':'knowledge_rag','predicted_route':'ticket_handler','allowed_routes':['knowledge_rag','ticket_handler'],'route_correct':True,'joint_correct':True,'format_valid':True},
              {'status':'evaluated','expected_route':'knowledge_rag','predicted_route':'knowledge_rag','route_correct':True,'joint_correct':True,'format_valid':True}]
        value=intent_metrics(rows)
        self.assertEqual(value['route_accuracy_including_errors'],1)
        self.assertEqual(value['macro_f1_including_errors'],1)
        self.assertEqual(value['single_label_f1_selected'],1)

    def test_errors_remain_in_denominator(self):
        rows=[{'status':'error','expected_route':'knowledge_rag'},
              {'status':'evaluated','expected_route':'knowledge_rag','predicted_route':'knowledge_rag','route_correct':True,'joint_correct':True,'format_valid':True}]
        value=intent_metrics(rows)
        self.assertEqual(value['route_accuracy_including_errors'],.5)
        self.assertAlmostEqual(value['f1_by_route']['knowledge_rag'],2/3)

    def test_a1_cannot_propose_authority(self):
        for raw in ['{"tool":"finish","approved":true}','{"tool":"policy_lookup","question":"x","arguments":{"user_id":"x"}}',
                    '{"tool":"ticket_create","arguments":{"user_id":"x"}}']:
            with self.assertRaises((ValueError,TypeError)):parse_decision(raw)
        self.assertEqual(parse_decision('{"tool":"finish"}').tool,'finish')

    def test_unavailable_compliance_not_clean_allow(self):
        result=aggregate([{'task':'compliance','id':'x','status':'error','expected':'allow','duration_ms':1,'calls':[]}])
        self.assertEqual(result['compliance']['clean_allow_rate_including_errors'],0)


if __name__=='__main__':unittest.main()
