"""Read-only local HTTP and real-model Knowledge Agent integration for step 23.

Does not create conversations, tickets, approvals, indexes, or evaluation labels.
The one real Agent call includes the existing reranker; it is NOT the B2 benchmark.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
from unittest.mock import patch

from dotenv import load_dotenv
import httpx
from langchain_core.messages import HumanMessage

from authenticated_client import authenticated_client
from smart_cs.settings import PROJECT_ROOT, ENV_PATH
from smart_cs.db.database import configured_database
from smart_cs.memory.bge_memory import BGEKnowledgeMemory
from smart_cs.agents.knowledge_rag import KnowledgeRAGAgent
from smart_cs.models import build_agent_models
from smart_cs.retrieval.hybrid import HybridConfig
from smart_cs.retrieval.lexical import tokenizer, fingerprint

OUT = PROJECT_ROOT / '.local/hybrid'
QUERY = '根据公开资料，哪些商品不适用七日无理由退货？'


def http_checks():
    result = {}
    with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, timeout=60) as client:
        for account in ['customer-a', 'customer-b']:
            with authenticated_client(client, account):
                responses = {}
                for mode in ['dense', 'hybrid', 'default']:
                    body = {'query': QUERY, 'top_k': 5}
                    if mode != 'default':
                        body['mode'] = mode
                    response = client.post('/api/knowledge/search', json=body)
                    response.raise_for_status()
                    assert 'no-store' in response.headers.get('cache-control', '')
                    value = response.json()
                    assert value['search'] == ('hybrid_rrf' if mode == 'hybrid' else 'exact_cosine')
                    assert value['documents'] and len({d['id'] for d in value['documents']}) == len(value['documents'])
                    responses[mode] = value
                assert [d['id'] for d in responses['dense']['documents']] == [d['id'] for d in responses['default']['documents']]
                audit = responses['hybrid']['retrieval']
                assert audit['tokenizer_sha256'] == tokenizer().fingerprint
                assert audit['config_sha256'] == fingerprint(HybridConfig.deployed().model_dump())
                assert audit['scores_are_confidence'] is False
                result[account] = {
                    'passed': True, 'default_search': responses['default']['search'],
                    'hybrid_search': responses['hybrid']['search'], 'audit': audit,
                    'hybrid_chunk_ids': [d['id'] for d in responses['hybrid']['documents']],
                }
        ready = client.get('/health/ready')
        ready.raise_for_status()
        assert ready.json()['ready'] is True
    return result


async def agent_check():
    database = configured_database()
    try:
        with patch.dict(os.environ, {'RAG_RETRIEVAL_MODE': 'hybrid'}):
            memory = BGEKnowledgeMemory(database)
            assert memory.mode == 'hybrid'
            llm = build_agent_models()['knowledge_rag']
            state = await KnowledgeRAGAgent(llm, memory).process({'messages': [HumanMessage(content=QUERY)]})
        evidence = state['knowledge_evidence']
        assert evidence['retrieval_mode'] == 'local_bge_m3_bm25_rrf'
        assert evidence['retrieved_count'] > 0
        assert evidence['status'] in {'answered', 'clarify'}
        if evidence['status'] == 'answered':
            assert evidence['citations']
        assert os.getenv('RAG_RETRIEVAL_MODE', 'dense') == 'dense'
        return {'passed': True, 'scope': 'single real Knowledge Agent integration, not quality evaluation',
                'evidence': evidence, 'agent_models': state['agent_models'],
                'answer': state['sub_results']['knowledge_rag'],
                'business_writes': 0, 'conversation_writes': 0,
                'reranker_included': True, 'global_mode_after_check': 'dense'}
    finally:
        await database.close()


def main():
    load_dotenv(ENV_PATH)
    OUT.mkdir(parents=True, exist_ok=True)
    assert os.getenv('RAG_RETRIEVAL_MODE', 'dense') == 'dense'
    report = {'passed': False, 'checked_at': datetime.now(timezone.utc).isoformat()}
    report['http'] = http_checks()
    report['agent'] = asyncio.run(agent_check())
    report['passed'] = True
    (OUT / 'live-integration.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'passed': True, 'http_accounts': 2, 'real_agent_status': report['agent']['evidence']['status'],
                      'default_mode': 'dense'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
