import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from pydantic import ValidationError
from smart_cs.evaluation.assets import ROOT,CORPUS,records,validate,sha,encoded,assign_splits,review_status,register
from smart_cs.evaluation.schema import CASE_ADAPTER,RagCase
from smart_cs.evaluation.runner import evidence_scores
from smart_cs.db.database import Database


@unittest.skipUnless((CORPUS/'sources/seven_day_returns.html').exists(),'Download public snapshots first')
class EvaluationAssetsTests(unittest.TestCase):
    def test_frozen_assets_have_traceable_evidence_and_all_four_tasks(self):
        result=validate()
        self.assertEqual(result['total_cases'],94)
        self.assertEqual(result['rag_candidates'],50)
        self.assertEqual(result['human_gold_cases'],0)
        self.assertEqual(result['corpus_sources'],10)
        self.assertEqual({r.task for r in records()},{'rag','intent','ticket_workflow','compliance'})

    def test_changed_labels_fail_checksum_before_any_evaluation(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'data';shutil.copytree(ROOT,target)
            path=target/'rag/test.jsonl';path.write_text(path.read_text(encoding='utf-8')+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'asset changed'):validate(target)

    def test_shared_sections_never_cross_splits_and_split_is_deterministic(self):
        first=assign_splits(records());second=assign_splits(records())
        self.assertEqual([(r.id,r.split,r.group_id) for r in first],[(r.id,r.split,r.group_id) for r in second])
        grouped={}
        for row in first:
            self.assertEqual(grouped.setdefault(row.group_id,row.split),row.split)

    def test_schema_rejects_wrong_task_fields_and_unsupported_gold_claim(self):
        row=records()[0].model_dump();row['review_status']='human_gold'
        with self.assertRaises(ValidationError):CASE_ADAPTER.validate_python(row)
        row=records()[0].model_dump();row['secret_extra']='value'
        with self.assertRaises(ValidationError):CASE_ADAPTER.validate_python(row)

    def test_document_hit_without_quote_is_not_evidence_recall(self):
        case=next(r for r in records() if isinstance(r,RagCase) and r.evidence)
        ref=case.evidence[0].alternatives[0]
        doc={'content':'unrelated text','metadata':{'document_id':ref.document_id,'page':ref.page}}
        self.assertEqual(evidence_scores(case,[doc],5)['evidence_recall'],0)
        doc['content']=ref.supporting_text
        self.assertGreater(evidence_scores(case,[doc],5)['evidence_recall'],0)

    def test_canonical_document_alias_needs_exact_artifact_hash_and_quote(self):
        case=next(r for r in records() if isinstance(r,RagCase) and r.evidence)
        ref=case.evidence[0].alternatives[0]
        doc={'content':ref.supporting_text,'metadata':{'document_id':'canonical-alias','page':ref.page,'artifact_sha256':'0'*64}}
        self.assertEqual(evidence_scores(case,[doc],5)['evidence_recall'],0)
        doc['metadata']['artifact_sha256']=ref.source_sha256
        self.assertGreater(evidence_scores(case,[doc],5)['evidence_recall'],0)
        doc['content']='unrelated text'
        self.assertEqual(evidence_scores(case,[doc],5)['evidence_recall'],0)

    def test_review_log_requires_matching_case_hash_and_never_auto_approves(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'review.jsonl'
            self.assertEqual(review_status(path)['pending_rag'],50)
            case=next(r for r in records() if isinstance(r,RagCase))
            decision={'case_id':case.id,'case_sha256':'invalid','decision':'approved','reviewer':'unit-test-reviewer','reviewed_at':'2026-11-13T12:00:00+08:00'}
            path.write_text(json.dumps(decision)+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'digest mismatch'):review_status(path)
            decision['case_sha256']=sha(encoded(case.model_dump()))
            path.write_text(json.dumps(decision)+'\n',encoding='utf-8')
            status=review_status(path)
            self.assertFalse(status['gold_gate_passed'])
            self.assertEqual(status['approved_rag'],1)

    def test_register_is_idempotent_and_keeps_labels_out_of_knowledge_tables(self):
        async def run(directory):
            from sqlalchemy import select,func
            from smart_cs.db.models import DatasetSample,KnowledgeDocument
            db=Database('sqlite+aiosqlite:///'+str(Path(directory)/'test.db').replace('\\','/'))
            try:
                await db.initialize(create_schema=True)
                self.assertEqual((await register(db))['inserted'],94)
                self.assertEqual((await register(db))['inserted'],0)
                async with db.sessions() as session:
                    self.assertEqual(await session.scalar(select(func.count()).select_from(DatasetSample)),94)
                    self.assertEqual(await session.scalar(select(func.count()).select_from(KnowledgeDocument)),0)
            finally:await db.close()
        with tempfile.TemporaryDirectory() as folder:asyncio.run(run(folder))

    def test_current_ticket_repository_cases_execute_without_touching_business_database(self):
        from smart_cs.evaluation.runner import ticket_probe
        async def run():
            for case in records():
                if case.task=='ticket_workflow':
                    result=await ticket_probe(case)
                    self.assertNotEqual(result['status'],'failed',case.id)
                    if case.capability_status=='planned':self.assertEqual(result['status'],'not_run')
        asyncio.run(run())

if __name__=='__main__':unittest.main()
