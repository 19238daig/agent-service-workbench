"""Candidate integrity, review gates and metric accounting; no provider requests."""
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from sqlalchemy import select,func
from smart_cs.evaluation.v2_assets import ROOT,rows,validate,review_status,register,sha,encoded,split_groups
from smart_cs.evaluation.v2_schema import IntentV2,RagV2
from smart_cs.evaluation.v2_runner import valid_intent_format,intent_score,cached,write
from smart_cs.evaluation.v2_metrics import classification,summarize
from smart_cs.db.database import Database
from smart_cs.db.models import DatasetSample,KnowledgeDocument


class EvaluationV2Tests(unittest.TestCase):
    def test_release_counts_groups_and_gold_status_are_explicit(self):
        value=validate();self.assertEqual(value['counts'],{'rag':300,'intent':1500,'compliance':200,'ticket_workflow':100})
        self.assertFalse(value['gold_gate_passed']);self.assertEqual(value['independent_human_gold_cases'],0)
        self.assertEqual(value['families']['intent'],150);self.assertEqual(value['families']['rag'],175)

    def test_legacy_assets_remain_bound_and_not_rewritten(self):
        manifest=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
        from smart_cs.settings import PROJECT_ROOT
        for relative,digest in manifest['legacy_manifests'].items():self.assertEqual(sha((PROJECT_ROOT/relative).read_bytes()),digest)

    def test_document_holdout_never_occurs_in_development(self):
        cases=[r for r in rows() if isinstance(r,RagV2) and any(ref.source=='zammad_ticket_actions.html' for group in r.evidence for ref in group.alternatives)]
        self.assertEqual(len(cases),2);self.assertTrue(all(r.split=='test' for r in cases));self.assertEqual(len({r.group_id for r in cases}),1)

    def test_every_paraphrase_and_evidence_group_has_one_split(self):
        from smart_cs.evaluation.v2_assets import keys
        splits={}
        for row in rows():
            for key in keys(row):self.assertEqual(splits.setdefault(key,row.split),row.split)
            if row.lineage.legacy_split in {'train','validation'}:self.assertNotEqual(row.split,'test')

    def test_current_compound_and_privacy_labels_are_reannotated(self):
        compound=[r for r in rows() if r.task=='intent' and r.lineage.legacy_case and 'multi_intent' in r.lineage.legacy_case]
        self.assertEqual(len(compound),100);self.assertTrue(all(r.required_tasks and r.expected_route=='ticket_handler' for r in compound))
        private=[r for r in rows() if r.task=='intent' and r.lineage.legacy_case and 'account_help-04' in r.lineage.legacy_case]
        self.assertTrue(all(r.expected_route=='compliance_checker' and r.lineage.label_change for r in private))

    def test_frozen_file_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'dataset';shutil.copytree(ROOT,target)
            with (target/'rag/test.jsonl').open('a',encoding='utf-8') as stream:stream.write('{}\n')
            with self.assertRaisesRegex(ValueError,'Frozen'):validate(target)

    def test_review_requires_hashes_human_attestation_and_never_auto_approves(self):
        case=rows()[0];manifest=validate()['manifest_sha256']
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'reviews.jsonl';self.assertEqual(review_status(path)['pending'],2100)
            record={'case_id':case.id,'case_sha256':sha(encoded(case.model_dump())),'manifest_sha256':manifest,'decision':'approved','reviewer':'unit-test-placeholder','reviewer_kind':'automated','independent_of_generation':True,'reviewed_at':datetime.now(timezone.utc).isoformat()}
            path.write_text(json.dumps(record)+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'human'):review_status(path)
            # Fixture-only declaration, never stored in the actual review log.
            record['reviewer_kind']='human';path.write_text(json.dumps(record)+'\n',encoding='utf-8');value=review_status(path)
            self.assertEqual(value['approved'],1);self.assertFalse(value['gold_gate_passed'])
            record['case_sha256']='0'*64;path.write_text(json.dumps(record)+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'binding'):review_status(path)

    def test_invalid_model_output_does_not_count_as_correct_unknown(self):
        self.assertFalse(valid_intent_format('not json'));self.assertFalse(valid_intent_format(json.dumps({'primary_intent':'unknown','confidence':True})))
        payload={'primary_intent':'transaction','secondary_intent':'ticket_transition','confidence':.9,'entities':{},'suggested_agent':'ticket_handler','tasks':['ticket_transition'],'slots':{'expected_version':'3'}}
        self.assertFalse(valid_intent_format(json.dumps(payload)));payload['slots']['expected_version']=3;self.assertTrue(valid_intent_format(json.dumps(payload)))

    def test_read_query_with_extra_write_task_fails_joint_metric(self):
        case=next(r for r in rows() if isinstance(r,IntentV2) and r.label=='order_query')
        predicted=SimpleNamespace(primary_intent=SimpleNamespace(value='transaction'),confidence=.99,suggested_agent='ticket_handler',tasks=['order_query','ticket_create'],entities=case.expected_entities)
        result=intent_score(case,predicted);self.assertTrue(result['route_correct']);self.assertFalse(result['joint_correct'])

    def test_error_cases_remain_in_accuracy_denominator(self):
        evaluated={'task':'intent','status':'evaluated','id':'a','group_id':'g1','expected_route':'ticket_handler','predicted_route':'ticket_handler','route_correct':True,'joint_correct':True,'tasks_correct':True,'entities_correct':True}
        value=classification([evaluated,{'id':'b','status':'error','group_id':'g2'}]);self.assertEqual(value['route_accuracy_including_errors'],.5);self.assertEqual(value['error_or_interrupted'],1)

    def test_fail_closed_compliance_is_reported_as_unavailable_not_quality_success(self):
        value=summarize([{'id':'x','task':'compliance','status':'error','expected':'block','predicted':'block','reviewer':'fail_closed'}])
        self.assertEqual(value['compliance']['fail_closed_unavailable'],1);self.assertIsNone(value['compliance']['candidate_block_recall']);self.assertEqual(value['errors'],['x'])

    def test_interrupted_model_marker_is_not_automatically_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'case.json';write(path,{'id':'case','status':'started'});value=cached(path)
            self.assertEqual(value['status'],'interrupted_unknown');self.assertFalse(value['automatic_model_replay']);self.assertEqual(cached(path),value)

    def test_registration_is_idempotent_and_separate_from_knowledge(self):
        async def run(folder):
            db=Database('sqlite+aiosqlite:///'+(Path(folder)/'data.db').as_posix())
            try:
                await db.initialize(create_schema=True);self.assertEqual((await register(db))['inserted'],2100);self.assertEqual((await register(db))['inserted'],0)
                async with db.sessions() as s:
                    self.assertEqual(await s.scalar(select(func.count()).select_from(DatasetSample)),2100);self.assertEqual(await s.scalar(select(func.count()).select_from(KnowledgeDocument)),0)
                async with db.sessions.begin() as s:
                    item=await s.scalar(select(DatasetSample).limit(1));item.payload={'tampered':True}
                with self.assertRaisesRegex(ValueError,'sample drift'):await register(db)
            finally:await db.close()
        with tempfile.TemporaryDirectory() as folder:asyncio.run(run(folder))
