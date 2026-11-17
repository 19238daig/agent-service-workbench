"""One authenticated, read-only policy chat through the real production graph.

Adds a NEW chat/run/checkpoint only. Does not approve or execute business actions.
Existing /rerank compatibility is compared with versioned 512-token inference.
"""
import asyncio
import argparse
from datetime import datetime, timezone
import json
import os
from uuid import uuid4

from dotenv import load_dotenv
import httpx
from authenticated_client import authenticated_client
from evaluate_reranker import OUT, read, write
from smart_cs.settings import ENV_PATH
from smart_cs.retrieval.clients import LocalModels
from smart_cs.retrieval.reranking import RerankConfig, CrossEncoderReranker, digest
from smart_cs.db.database import configured_database
from smart_cs.db.models import ChatSession
from smart_cs.persistence.postgres import PostgresCheckpointStore


async def compatibility():
    profile = RerankConfig.model_validate(read(OUT/'legacy-profile.json'))
    query = '退款申请需要什么资料？'
    passages = ['退款申请请提供订单号和退款原因。', '地球围绕太阳公转。']
    old = await LocalModels().arerank(query, passages)
    new = await CrossEncoderReranker(profile).score(query, passages)
    error = max(abs(a-b) for a, b in zip(old, new['scores']))
    assert error <= 1e-6
    return {'passed': True, 'legacy_512_scores': old, 'versioned_512_scores': new['scores'],
            'max_absolute_difference': error, 'purpose': 'API compatibility smoke, not quality evaluation'}


async def checkpoint_evidence(session_id, user_id):
    database = configured_database(); store = None
    try:
        async with database.sessions() as s:
            session = await s.get(ChatSession, session_id)
            assert session is not None and session.user_id == user_id
            thread = session.thread_id
        store = await PostgresCheckpointStore(database).open()
        checkpoint = await store.saver.aget_tuple({'configurable': {'thread_id': thread}})
        assert checkpoint is not None
        evidence = checkpoint.checkpoint['channel_values']['knowledge_evidence']
        return evidence
    finally:
        if store is not None: await store.close()
        await database.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--focused',action='store_true',help='Additional focused policy/citation smoke; preserve first result')
    args=parser.parse_args(); prefix='live-citation-' if args.focused else 'live-'
    load_dotenv(ENV_PATH)
    selection = read(OUT/'selection.json')
    assert RerankConfig.deployed().model_dump() == selection['deployment_profile']
    assert os.getenv('RAG_RETRIEVAL_MODE', 'dense') == 'dense'
    report = {'passed': False, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'compatibility': asyncio.run(compatibility()), 'business_action_requested': False}
    marker = OUT/(prefix+'request.json')
    # A timeout must not create another run or silently replay model calls.
    if marker.exists():
        body = read(marker)
    else:
        body = {'client_request_id': 'step24-'+uuid4().hex,
                'message': '经营者在自动续费前是否需要提醒消费者？请根据公开资料说明。' if args.focused
                           else '请检索公开知识库，说明哪些商品不适用七日无理由退货。'}
        write(marker, body)
    with httpx.Client(base_url='http://127.0.0.1:8000', trust_env=False, timeout=180) as client:
        with authenticated_client(client, 'customer-a') as user:
            response = client.post('/api/chat', json=body)
            response.raise_for_status(); result = response.json()
            write(OUT/(prefix+'response.json'), result)
            evidence = asyncio.run(checkpoint_evidence(result['session_id'], user['id']))
            assert 'knowledge_rag' in result['agent_steps'] and result['compliance_passed'] is True
            assert evidence['rerank_configuration'] == selection['deployment_profile']
            assert evidence['retrieval_mode'] == 'local_bge_m3_pgvector'
            assert evidence['status'] in {'answered', 'clarify'}
            if evidence['status'] == 'answered': assert result['citations']
            assert not result.get('action_request') and not result.get('action_execution')
            record = client.get('/api/runs/'+result['run_id']); record.raise_for_status()
            calls = record.json()['model_calls']
            assert any(c['kind']=='reranker' and c['status']=='succeeded' for c in calls)
            assert any(c['kind']=='chat' and c['status']=='succeeded' for c in calls)
            for citation in result['citations']:
                detail = client.get('/api/knowledge/documents/'+citation['document_id']+'/chunks/'+citation['chunk_id']); detail.raise_for_status()
                assert detail.json()['content']
            report['chat'] = {'run_id': result['run_id'], 'session_id': result['session_id'],
                              'status': evidence['status'], 'agent_steps': result['agent_steps'],
                              'rerank_configuration': evidence['rerank_configuration'],
                              'citation_count': len(result['citations']),
                              'observed_calls': [{k: c.get(k) for k in ['kind','model','status','duration_ms','error_code']} for c in calls],
                              'new_readonly_conversation_only': True}
        ready = client.get('/health/ready'); ready.raise_for_status(); assert ready.json()['ready']
    report['passed'] = True
    write(OUT/(prefix+'integration.json'), report)
    print(json.dumps({'passed': True, 'answer_status': report['chat']['status'],
                      'citations': report['chat']['citation_count'], 'default_mode': 'dense'}))


if __name__ == '__main__':
    main()
