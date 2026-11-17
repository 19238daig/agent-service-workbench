"""Small, task-specific model comparison through the configured gateway."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time

from dotenv import dotenv_values
import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI

from smart_cs.agents.compliance_checker import ComplianceCheckerAgent
from smart_cs.agents.intent_router import IntentRouterAgent
from smart_cs.agents.knowledge_rag import KnowledgeRAGAgent
from smart_cs.agents.ticket_handler import TicketHandlerAgent
from smart_cs.memory.long_term import LongTermMemory
from smart_cs.settings import PROJECT_ROOT


async def main():
    config = dotenv_values(PROJECT_ROOT / '.env')
    results = []
    semaphore = asyncio.Semaphore(3)
    history = [HumanMessage(content='帮我创建退款工单'), AIMessage(content='已创建退款工单，工单号TK-20261113-ABC123。')]
    jobs = []
    for model in ['glm-5.3-flash', 'deepseek-v4.1-flash', 'gemini-3.8-flash']:
        jobs += [('intent_router', model, 'policy'), ('intent_router', model, 'followup')]
    for model in ['glm-5.3-flash', 'deepseek-v4.1-flash', 'kimi-k3']:
        jobs.append(('ticket_handler', model, 'query'))
    for model in ['deepseek-v4.1-flash', 'gemini-3.8-flash', 'kimi-k3']:
        jobs.append(('knowledge_rag', model, 'grounded_answer'))
    for model in ['glm-5.3', 'kimi-k3', 'gemini-3.8-flash']:
        jobs += [('compliance_checker', model, 'unsafe'), ('compliance_checker', model, 'allowed')]

    async def run(role, model, case):
        async with semaphore:
            started = time.monotonic()
            item = {'role': role, 'model': model, 'case': case}
            try:
                with httpx.Client(trust_env=False) as sync_client:
                    async with httpx.AsyncClient(trust_env=False) as async_client:
                        llm = ChatOpenAI(model=model, api_key=config['OPENAI_API_KEY'], base_url=config['OPENAI_BASE_URL'],
                                         temperature=0, timeout=45, max_retries=0,
                                         http_client=sync_client, http_async_client=async_client)
                        if role == 'intent_router':
                            agent = IntentRouterAgent(llm)
                            query = '购买后第3天能否退款，退款多久能到账？' if case == 'policy' else '刚才那张工单现在是什么状态？'
                            result = await agent.classify(query, [] if case == 'policy' else history)
                            output = asdict(result)
                            expected = 'knowledge_rag' if case == 'policy' else 'ticket_handler'
                            passed = result.suggested_agent == expected and result.confidence >= .5
                            if case == 'followup':
                                passed = passed and result.entities.get('ticket_id') == 'TK-20261113-ABC123'
                        elif role == 'ticket_handler':
                            output = await TicketHandlerAgent(llm).analyze_request('查询刚才那张工单的状态，不要新建工单。', history, {'entities': {'ticket_id': 'TK-20261113-ABC123'}})
                            passed = output.get('action') == 'query' and output.get('ticket_id') == 'TK-20261113-ABC123'
                        elif role == 'knowledge_rag':
                            agent = KnowledgeRAGAgent(llm, LongTermMemory(PROJECT_ROOT / '.local' / 'benchmark-index'))
                            output = await agent.generate_answer('购买第3天能否退款，退款多久能到账？', [
                                {'content': '退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。退款将在3-5个工作日内原路退回。', 'source': 'refund_policy.md'},
                            ])
                            passed = bool(output) and '退款' in output and 'refund_policy.md' in output and ('3' in output or '三' in output)
                        else:
                            text = '购买这款理财产品，到期您一定可以拿回本金和利息。' if case == 'unsafe' else '退款政策：购买后7天内可申请无理由退款，退款将在3-5个工作日内原路退回。'
                            result = await ComplianceCheckerAgent(llm).llm_check(text)
                            output = asdict(result)
                            passed = result.passed is (case == 'allowed')
                        item.update(passed=bool(passed), output=output)
            except Exception as error:
                # Provider exception text can contain credentials; persist only its type.
                item.update(passed=False, error=type(error).__name__)
            item['seconds'] = round(time.monotonic() - started, 2)
            results.append(item)
            report = PROJECT_ROOT / '.local' / 'agent-model-benchmark.json'
            report.write_text(json.dumps({'scope': '18 small synthetic examples; not a general benchmark or a price comparison', 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({key: value for key, value in item.items() if key != 'output'}, ensure_ascii=False), flush=True)

    await asyncio.gather(*(run(*job) for job in jobs))


if __name__ == '__main__':
    asyncio.run(main())
