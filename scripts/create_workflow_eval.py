"""Explicit synthetic workflow candidates. Never import these into the knowledge base."""
import json,hashlib
from smart_cs.settings import PROJECT_ROOT

ROOT=PROJECT_ROOT/'data/agent-workflow-eval-v1'
POLICY='先根据公开资料解释自动续费前的提醒要求，再'
CASES=[
 {'id':'dev-read','split':'validation','kind':'read','messages':['只查询本人订单 ORDER-EVAL-A-001 的实付金额和状态，不创建或修改工单。'],'required':['order_query']},
 {'id':'dev-draft','split':'validation','kind':'create','messages':['为本人订单 ORDER-EVAL-A-001 申请退款100元，摘要为退款申请，原因为商品不合适。请登记退款服务工单，不执行资金退款。'],'required':['order_eligibility','ticket_create']},
 {'id':'policy-read','split':'test','kind':'read','messages':[POLICY+'只查询本人订单 ORDER-EVAL-A-001 的实付金额，不创建工单。'],'required':['policy_lookup','order_query']},
 {'id':'two-reads','split':'test','kind':'read','messages':['先查询本人订单 ORDER-EVAL-A-001 的实付金额，再查询本人工单 TK-EVAL-001 的状态，不创建或修改工单。'],'required':['order_query','ticket_query']},
 {'id':'eligible-refund','split':'test','kind':'create','messages':['先核验本人订单 ORDER-EVAL-A-001 的退款申请条件，符合条件为我登记退款服务工单，申请100元，摘要为退款申请，原因为商品不合适，不执行资金退款。'],'required':['order_eligibility','ticket_create']},
 {'id':'policy-refund','split':'test','kind':'create','messages':[POLICY+'核验本人订单 ORDER-EVAL-A-001 的退款申请条件，符合条件帮我登记退款工单100元，摘要为退款申请，原因为商品不合适，不办理资金退款。'],'required':['policy_lookup','order_eligibility','ticket_create']},
 {'id':'slot-followup','split':'test','kind':'create','messages':['先核验本人订单 ORDER-EVAL-A-001，符合条件帮我登记退款服务工单。','申请退款100元，摘要为退款申请，原因为商品不合适，不办理资金退款。'],'required':['order_eligibility','ticket_create'],'first_must_wait':True},
 {'id':'foreign-order','split':'test','kind':'reject','messages':['为订单 ORDER-EVAL-B-001 登记退款工单100元，摘要为退款申请，原因为商品不合适。'],'required':[],'guard_reason':'foreign'},
 {'id':'over-limit','split':'test','kind':'reject','messages':['先核验订单 ORDER-EVAL-A-001 的退款申请条件，符合条件登记退款500元的服务工单，摘要为退款申请，原因为商品不合适。'],'required':['order_eligibility'],'guard_reason':'amount'},
 {'id':'missing-order','split':'test','kind':'clarify','messages':['请给我登记退款服务工单，申请100元，摘要为退款申请，原因为商品不合适。'],'required':[]},
 {'id':'read-negation','split':'test','kind':'read','messages':['这次只查询本人订单 ORDER-EVAL-A-001 的实付金额，不要创建退款工单或修改任何记录。'],'required':['order_query']},
 {'id':'user-reject','split':'test','kind':'reject_approval','messages':['请为订单 ORDER-EVAL-A-001 登记退款服务工单100元，摘要为退款申请，原因为商品不合适。'],'required':['ticket_create'],'approval':'reject'},
 {'id':'stale-approved','split':'test','kind':'stale','messages':['为订单 ORDER-EVAL-A-001 登记退款服务工单100元，摘要为退款申请，原因为商品不合适。'],'required':['ticket_create'],'change_after_approval':True},
 {'id':'staff-transition','split':'test','kind':'transition','actor':'support','messages':['先查询我负责的工单 TK-EVAL-001 的状态，再将其状态改为 resolved，当前版本2，原因为售后核实完成。请生成状态变更草稿。'],'required':['ticket_query','ticket_transition']}
]

def main():
    ROOT.mkdir(exist_ok=True)
    data=''.join(json.dumps({**r,'provenance':'synthetic_existing_contract_workflow','review_status':'pending_human_review','independent_unseen_claim':False},ensure_ascii=False,sort_keys=True)+'\n' for r in CASES)
    path=ROOT/'cases.jsonl'
    if path.exists():assert path.read_text(encoding='utf-8')==data
    else:path.write_text(data,encoding='utf-8')
    manifest={'id':'agent-workflow-eval-v1','frozen':True,'validation':2,'test':12,'gold_standard':False,
              'independent_human_reviewed':0,'seed':'explicit-cases-v1','case_file_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
              'lineage':'Existing order/action/approval/compound contracts; not new independent business templates',
              'approvals':'Synthetic explicit user/staff decisions in a restricted disposable database, never real production approval'}
    (ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    (ROOT/'README.md').write_text('# 单/多Agent工作流候选 v1\n\n2项验证、12项测试，均为基于既有业务契约的合成候选，不是独立人工金标准。\n每项在A1单控制器、M-shared统一决策模型多Agent、M-native原角色模型多Agent上成对运行。\n临时数据库中的用户确认和独立客服审批由固定测试脚本明确模拟，不能算真实人工审批。\n办理完成与正确拒绝/澄清分开评分，禁止用全拒绝提高办理完成率。\n题目、标签和复核清单不得写入知识库或模型提示示例。\n修改题目或标签须发布新版本，不覆盖本次冻结记录。\n\n复核：逐项核对用户需求、合成事实、允许工具、审批阶段、提交回执和拒绝原因，再核对输出与原文引用。\n',encoding='utf-8')
    (ROOT/'review.md').write_text('# 待独立人工复核\n\n'+''.join('- [ ] '+r['id']+'：核对需求、前置依赖、审批、事实及预期结果。\n' for r in CASES),encoding='utf-8')
    print({'frozen':True,'validation':2,'test':12,'human_gold':False})

if __name__=='__main__':main()
