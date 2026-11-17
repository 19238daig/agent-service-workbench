"""Public workflow descriptions and a strict projection of execution events.

Never serialize graph state wholesale: it contains prompts, history and unaudited
drafts. This module deliberately publishes only selected operational fields.
"""
from __future__ import annotations
import re

NODES = [
    {'id':'task_initialize','label':'Supervisor 任务计划','description':'编译有限任务、依赖与槽位，绑定当前用户和上下文'},
    {'id':'task_dispatch','label':'Supervisor 调度','description':'选择前置任务已完成的下一项，缺信息先询问并限制总步数'},
    {'id':'task_collect','label':'Supervisor 收集结果','description':'保存任务结果与进度，前置失败时停止后续办理'},
    {'id':'task_after_review','label':'任务审查结果','description':'审查失败时关闭计划，等待确认时保留真实任务状态'},
    {"id": "intent_router", "role": "intent_router", "label": "意图路由 Agent", "description": "识别需求、提取实体，给出路由建议"},
    {"id": "supervisor_route", "label": "Supervisor 分派", "description": "按意图与置信度选择处理分支；低置信度先澄清"},
    {"id": "knowledge_rag", "role": "knowledge_rag", "label": "知识检索 Agent", "description": "改写查询、检索资料、重排并生成有依据的答复"},
    {"id": "ticket_handler", "role": "ticket_handler", "label": "工单处理 Agent", "description": "核验订单、查询工单并提出草稿；确认及必要审批后由服务器办理"},
    {'id':'action_review','model_role':'compliance_checker','label':'合规 Agent · 动作审查','description':'绑定动作参数与事实版本，审查风险并提出后续审批要求'},
    {"id": "compliance_check", "role": "compliance_checker", "label": "合规审查 Agent", "description": "规则与模型核验出站全文；动作风险另经同一 Agent 的动作审查节点"},
    {"id": "synthesize", "label": "Supervisor 汇总", "description": "审查完成后返回最终答复"},
    {'id':'approval_gate','label':'确认与人工审批','description':'保存已审草稿并暂停，核验确认和审批后恢复；当前未执行业务写入'},
    {'id':'execute_action','label':'事务执行','description':'复核审批并以动作ID提交一次工单及事件，保存真实结果'},
    {'id':'execution_review','model_role':'compliance_checker','label':'合规 Agent · 结果审查','description':'核对已提交记录后审查办理回执，失败不撤销已提交事实'},
    {'id':'execution_synthesize','label':'办理结果汇总','description':'发布审查后的结果，执行记录独立可查'},
]
NODE_IDS = {node["id"] for node in NODES}
ROLES = {node["id"]: node.get("role") or node.get('model_role') for node in NODES}


def workflow_profile(graph):
    models = (getattr(graph, "config", None) or {}).get("metadata", {}).get("workflow_models", {})
    active=set(graph.get_graph().nodes) if graph is not None else NODE_IDS-{'action_review','approval_gate','execute_action','execution_review','execution_synthesize'}
    return {
        "ready": graph is not None,
        "nodes": [{**node, "model": models.get(node.get("role") or node.get('model_role'), "")} for node in NODES if node['id'] in active],
        "edges": [
            {"source": edge.source, "target": edge.target, "conditional": edge.conditional}
            for edge in graph.get_graph().edges
            if edge.source in NODE_IDS and edge.target in NODE_IDS
        ] if graph is not None else [],
    }


def node_summary(node: str, output: dict) -> dict:
    """Small, typed facts only; no free-form draft/reasoning/model output."""
    if node in {'task_initialize','task_dispatch','task_collect','task_after_review'}:
        from smart_cs.schemas.tasks import public_plan
        plan=public_plan(output.get('task_plan'))
        return {k:plan.get(k) for k in ['plan_id','status','completed_task_ids','missing_fields','steps_used','max_steps']}
    if node in {'execute_action','execution_review','execution_synthesize'}:
        return {'action_id':output.get('action_execution',{}).get('action_id'),'committed':bool(output.get('action_execution')),
            'content_passed':output.get('compliance_passed',False)}
    if node == "intent_router":
        details = output.get("intent_details", {})
        return {"primary": details.get("primary"),
                "confidence": details.get("confidence"), "suggested_agent": details.get("suggested_agent")}
    if node == "supervisor_route":
        route = output.get("intent", "unknown")
        return {"route": route, "clarification": route == "clarification"}
    if node == "knowledge_rag":
        evidence = output.get("knowledge_evidence", {})
        return {"retrieved_count": evidence.get("retrieved_count", 0),
                "used_count": evidence.get("used_count", 0),
                'answer_status':evidence.get('status','unknown'),
                'reason_code':evidence.get('reason_code',''),
                'support_check':evidence.get('support_check',''),
                "sources": [str(s)[:160] for s in evidence.get("sources", [])[:5]],
                'embedding_model':evidence.get('embedding_model',''),'reranker_model':evidence.get('reranker_model',''),
                'retrieval_mode':evidence.get('retrieval_mode',''),
                'citations':[{'source':c['source'],'page':c.get('page'),'heading':c.get('heading','')} for c in evidence.get('citations',[])[:4]]}
    if node == "ticket_handler":
        evidence = output.get("ticket_evidence", {})
        summary = {key: evidence[key] for key in ("action", "found", "status") if key in evidence}
        for key in ['action_id','plan_status','reason_code']:
            if key in evidence:summary[key]=evidence[key]
        if evidence.get('write_blocked') is True:summary['write_blocked']=True
        ticket_id = evidence.get("ticket_id")
        if isinstance(ticket_id, str) and re.fullmatch(r"TK-\d{8}-[0-9A-F]{6}", ticket_id):
            summary["ticket_id"] = ticket_id
        if evidence.get('found') and evidence.get('action') in {'order_query','refund_eligibility'}:
            order_id=evidence.get('order_id')
            if isinstance(order_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',order_id):summary['order_id']=order_id
            if evidence.get('decision') in {'allowed','denied','requires_review','needs_information'}:summary['decision']=evidence['decision']
            amount=evidence.get('maximum_refund_amount')
            if isinstance(amount,str) and re.fullmatch(r'(?:0|[1-9][0-9]{0,9})\.[0-9]{2}',amount):summary['maximum_refund_amount']=amount
        return summary
    if node=='action_review':
        review=output.get('action_review',{})
        return {k:review.get(k) for k in ['decision','risk_level','required_approval','action_id','reason_codes','execution_authorized']}
    if node == "compliance_check":
        review = output.get("sub_results", {}).get("compliance", {})
        # Violation text may quote a rejected draft, so publish counts only.
        return {"passed": review.get("passed", False), "risk_level": review.get("risk_level", "unknown"),
                "violation_count": len(review.get("violations", []))}
    if node == "synthesize":
        return {"released": output.get("compliance_passed", False),
                "response_chars": len(output.get("final_response", ""))}
    if node=='approval_gate':
        return {k:output.get('action_request',{}).get(k) for k in ['action_id','status','execution_enabled','executed']}
    return {}
