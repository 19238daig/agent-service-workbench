import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from langchain_core.messages import HumanMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from smart_cs.agents.knowledge_rag import KnowledgeRAGAgent
from smart_cs.ingestion.parsers import Block,parse
from smart_cs.ingestion.chunking import semantic_chunks
from smart_cs.retrieval.clients import LocalModels
from pypdf import PdfWriter


class PublicIngestionTests(unittest.TestCase):
    def test_html_filters_active_content_and_preserves_article_and_table(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'source.html'
            path.write_text('<html><meta charset="utf-8"><nav><p>菜单噪声</p></nav><main><h1 id="policy">政策</h1><p>第十条 自动续费前须提醒。</p><table><tr><th>状态</th><th>说明</th></tr><tr><td>Open</td><td>待处理</td></tr></table><script>secret_script</script></main></html>',encoding='utf-8')
            blocks=parse(path)
            self.assertEqual(blocks[0].text,'第十条 自动续费前须提醒。')
            self.assertIn('第十条',blocks[0].heading)
            self.assertEqual(blocks[0].anchor,'policy')
            self.assertEqual(blocks[1].kind,'table')
            self.assertIn('状态 | 说明',blocks[1].text)
            self.assertNotIn('噪声',''.join(b.text for b in blocks))
            self.assertNotIn('secret_script',''.join(b.text for b in blocks))

    def test_unextractable_pdf_is_rejected_instead_of_hallucinated_ocr(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'blank.pdf';writer=PdfWriter();writer.add_blank_page(width=300,height=300);writer.write(path)
            with self.assertRaisesRegex(ValueError,'OCR'):parse(path)

    def test_explicit_government_article_beats_header_role_main(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'article.html'
            path.write_text('<html><meta charset="utf-8"><div role="main"><p>登录噪声</p></div></html><table><tr><td><div id="UCAP-CONTENT"><p>第六条 不适用商品：</p><p>消费者定作的商品。</p></div></td></tr></table>',encoding='utf-8')
            blocks=parse(path)
            self.assertEqual(len(blocks),2)
            self.assertEqual(blocks[1].heading,'第六条')
            self.assertNotIn('登录',''.join(b.text for b in blocks))

    def test_pdf_keeps_real_page_numbers_and_article_first_line(self):
        # Source bytes are independently downloaded public material, not generated manuals.
        from smart_cs.settings import PROJECT_ROOT
        path=PROJECT_ROOT/'data/public-after-sales-v1/sources/consumer_protection_regulation.pdf'
        if not path.exists():self.skipTest('Run download_public_knowledge.py for public PDF integration check')
        blocks=parse(path)
        self.assertEqual(max(b.page for b in blocks),15)
        self.assertTrue(any('自动续费' in b.text for b in blocks))
        self.assertTrue(any('第十条' in b.text and b.heading=='第十条' for b in blocks))

    def test_semantic_split_respects_page_and_heading_boundaries(self):
        class Models:
            def tokenize(self,texts,**kwargs):return [[{'text':t,'tokens':20}] for t in texts]
            def embed(self,texts):return [[1.,0.] for _ in texts]
        blocks=[Block('a','A',1),Block('b','A',1),Block('c','A',2),Block('d','B',2)]
        chunks=semantic_chunks(blocks,Models(),max_tokens=80,min_tokens=20)
        self.assertEqual([c['content'] for c in chunks],['a\nb','c','d'])
        self.assertEqual([c['page'] for c in chunks],[1,2,2])
        self.assertTrue(all(c['token_count']<=80 for c in chunks))

    def test_invalid_or_random_shape_vectors_are_rejected(self):
        for values in [[[1.]*768],[[0.]*1024],[[float('nan')]*1024]]:
            with self.assertRaises(ValueError):LocalModels.vectors(values,1)
        normalized=LocalModels.vectors([[1.]*1024],1)
        self.assertAlmostEqual(float(np.linalg.norm(normalized[0])),1.)

    def test_remote_model_endpoints_are_not_silently_used(self):
        with patch.dict('os.environ',{'RAG_EMBEDDING_URL':'https://example.com'}):
            with self.assertRaises(ValueError):LocalModels()

    def test_local_rag_uses_async_retriever_and_actual_reranker_without_chat_ranking(self):
        class Memory:
            local_rag=True;retrieval_mode='local_bge_m3_pgvector'
            async def search(self,query,top_k):
                return [{'id':'chunk1','source':'source.pdf','content':'应在自动续费日期前提醒消费者注意。',
                    'metadata':{'document_id':'doc1','url':'/api/knowledge/documents/doc1/file#page=2','page':2,
                        'effective_from':'2024-07-01','effective_until':None,'version':'v1'}}]
            async def rerank(self,query,docs,top_k):return docs
            async def revalidate(self,docs):pass
        llm=FakeListChatModel(responses=[json.dumps({'status':'answered','claims':[{'text':'应提醒消费者。',
            'references':[{'chunk_id':'chunk1','quote':'应在自动续费日期前提醒消费者注意。'}]}]}),
            json.dumps({'status':'supported','answers_question':True,'scope_valid':True,'claims':[{'claim':1,'supported':True}]})])
        result=asyncio.run(KnowledgeRAGAgent(llm,Memory()).process({'messages':[HumanMessage(content='自动续费提醒？')]}))
        self.assertEqual(result['knowledge_evidence']['citations'][0]['page'],2)
        self.assertEqual(result['knowledge_evidence']['used_count'],1)
        self.assertEqual(result['knowledge_evidence']['support_check'],'literal_quote_and_model_review')
        self.assertIn('应提醒',result['sub_results']['knowledge_rag'])

if __name__=='__main__':unittest.main()
