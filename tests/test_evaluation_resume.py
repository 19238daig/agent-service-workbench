"""Offline date pin and replay markers; no provider calls or business writes."""
from datetime import date
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from smart_cs.evaluation.v2_runner import initialize,read
from smart_cs.retrieval.grounding import GroundedAnswerService
from smart_cs.settings import PROJECT_ROOT


class OfflineClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_pins_explicit_business_date_and_clock_protocol(self):
        from smart_cs.evaluation.v2_assets import ROOT
        manifest=read(ROOT/'manifest.json')
        index=SimpleNamespace(id='fixture',configuration={'source_manifest_hash':manifest['source_manifest_sha256']},model_identity={'embedding':'fixture'})
        database=SimpleNamespace(sessions=lambda:AsyncMock(),close=AsyncMock())
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT/'.local/evaluation') as parent:
            folder=Path(parent)/'clock-test'
            with patch('smart_cs.evaluation.v2_runner.configured_database',return_value=database),patch('smart_cs.evaluation.v2_runner.DenseRetriever.active',new=AsyncMock(return_value=index)):
                config=await initialize(folder,as_of='2026-11-15')
            self.assertEqual(config['as_of'],'2026-11-15');self.assertEqual(config['clock_mode'],'frozen_offline')
            self.assertEqual(read(folder/'run.json')['as_of'],'2026-11-15')

    def test_grounded_offline_date_does_not_follow_later_host_day(self):
        service=GroundedAnswerService(None,clock=lambda:date(2026,11,15))
        doc={'metadata':{'effective_from':'2026-11-15','effective_until':'2026-11-16'}}
        self.assertTrue(service.valid_dates([doc]))
        later=GroundedAnswerService(None,clock=lambda:date(2026,11,16));self.assertFalse(later.valid_dates([doc]))
