"""Conservative rewrite trigger; ambiguous history still uses the existing model."""
import re
from smart_cs.performance.policy import ServingPolicy
from smart_cs.performance.runtime import event
from smart_cs.observability.context import run_context


def needs_rewrite(query,history,*,local=True):
    if not local:return True
    if not history:return False
    policy=ServingPolicy.deployed()
    if not policy.enabled or not policy.rewrite_on_demand:return True
    reference=bool(re.search(r'这个|那个|这些|那些|刚才|上述|上面|之前|前面|它|该项|该订单|该工单|该规定|继续|还有呢',query))
    topic=bool(re.search(r'退款|退货|七日|自动续费|订单|工单|开户|证件|投诉|隐私|密码|宏操作|知识库|消费者|经营者|ticket|refund|subscription|knowledge base',query,re.I))
    needed=reference or len(query.strip())<12 or not topic
    if not needed:event(run_context.get(),'rewrite_skip',reason='self_contained_current_question')
    return needed
