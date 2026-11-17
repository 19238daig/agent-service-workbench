import { label, type TaskPlan, type ActionRequest, type Execution } from './product/types';
import { BookOpen, Check, ClipboardCheck, GitBranch, LoaderCircle, Minus, ShieldCheck, X, CircleAlert, ChevronDown } from 'lucide-react';
import { defaultNodes, facts, routeNames, seconds, type Profile, type Step, type Trace } from './workflow';

const icons = { intent_router: GitBranch, knowledge_rag: BookOpen, ticket_handler: ClipboardCheck, compliance_check: ShieldCheck };

export function WorkflowOverview({ onOpen }: { onOpen: () => void }) {
  return <button className="workflow-overview" onClick={onOpen} aria-label="查看四个 Agent 的协作流程">
    <span className="overview-heading"><GitBranch size={15} />四 Agent 协作<small>查看流程</small></span>
    <span className="overview-path"><span>意图路由</span><span aria-hidden="true">→</span><span className="overview-branches"><span>知识检索</span><span>工单处理</span></span><span aria-hidden="true">→</span><span>合规审查</span></span>
    <span className="overview-footnote">Supervisor 按需求分派，审查后统一回复</span>
  </button>;
}

export default function WorkflowPanel({ profile, trace, taskPlan, action, execution, historical, title, onClose }: {
  profile?: Profile; trace?: Trace; taskPlan?: TaskPlan; action?: ActionRequest; execution?: Execution; historical?: boolean; title?: string; onClose?: () => void;
}) {
  const nodes = profile?.nodes || defaultNodes;
  const route = String(trace?.steps.find(s => s.node === 'supervisor_route')?.summary?.route || '');
  const activeStep = trace?.steps.find(s => s.status === 'running');
  const ended = trace && trace.status !== 'running';
  const stepByNode = (id: string) => trace?.steps.filter(s => s.node === id).at(-1);
  function nodeCard(id: keyof typeof icons, branch = false) {
    const node = nodes.find(n => n.id === id) || defaultNodes.find(n => n.id === id)!;
    const step = stepByNode(id);
    const unused = !step && !!route && ['knowledge_rag', 'ticket_handler'].includes(id) && route !== id;
    const state = step?.status || (unused ? 'skipped' : ended ? 'not-run' : 'waiting');
    const status = { running: '执行中', done: '已完成', blocked: '已拦截', error: '已中断', skipped: '本轮未调用', 'not-run': '未执行', waiting: trace ? '等待中' : '待开始' }[state];
    const Icon = icons[id];
    const StatusIcon = state === 'running' ? LoaderCircle : state === 'done' ? Check : ['blocked', 'error'].includes(state) ? CircleAlert : Minus;
    return <section className={`agent-node node-${state} ${branch ? 'branch-node' : ''}`} data-node={id} data-status={state} aria-label={`${node.label}，${status}`}>
      <div className="agent-node-heading"><span className="agent-icon"><Icon size={17} /></span><strong>{node.label.replace(' Agent', '')}</strong><span className="agent-state"><StatusIcon size={12} />{status}</span></div>
      <p className="agent-description">{node.description}</p>
      <div className="agent-model" title={step?.model || node.model || ''}>{step?.model || node.model || (profile?.ready === false ? '模型待配置' : '模型信息加载中')}</div>
      {step && <div className="agent-result">{facts(step).map((fact, i) => <span key={i}>{fact}</span>)}</div>}
      {step?.duration_ms !== undefined && <div className="agent-duration">耗时 {seconds(step.duration_ms)}</div>}
    </section>;
  }
  function supervisor(id: string, text: string) {
    const step = stepByNode(id);
    return <div className={`supervisor-step ${step ? `node-${step.status}` : ''}`} data-node={id} data-status={step?.status || 'waiting'}><GitBranch size={13} /><span>{text}</span>{step?.status === 'running' ? <LoaderCircle size={13} className="spinning" /> : step?.duration_ms !== undefined ? <small>{seconds(step.duration_ms)}</small> : <small>确定性逻辑</small>}
      {step?.summary && <p>{facts(step).join('，')}</p>}</div>;
  }
  return <section className="workflow-panel" aria-label="Agent 协作过程">
    <header className="workflow-header"><div><span className="workflow-eyebrow"><GitBranch size={14} />执行工作流</span><h2>四 Agent 协作</h2></div>{onClose && <button className="icon-button" onClick={onClose} aria-label="关闭协作过程"><X size={19} /></button>}</header>
    <div className="workflow-context"><span className={`trace-indicator trace-${trace?.status || 'idle'}`} />{trace ? trace.status === 'running' ? (activeStep ? `${nodes.find(n => n.id === activeStep.node)?.label || '节点'}执行中` : '已发送，等待执行事件') : trace.status === 'error' ? '执行中断，可重试' : '本轮执行完成' : historical ? '此历史回复没有执行记录' : '发送问题，查看实际执行路径'}{trace?.duration_ms !== undefined && <strong>{seconds(trace.duration_ms)}</strong>}</div>
    {title && <p className="workflow-question" title={title}>{title}</p>}
    <div className="workflow-canvas">
      <div className="workflow-entry">用户消息</div><div className="flow-connector"><ChevronDown size={14} /></div>
      {nodeCard('intent_router')}
      <div className="flow-connector"><ChevronDown size={14} /></div>
      {supervisor('supervisor_route', 'Supervisor 分派')}
      <div className="branch-caption">{route ? `本轮路由：${routeNames[route] || route}` : '根据意图选择处理分支'}</div>
      <div className="workflow-branches">{nodeCard('knowledge_rag', true)}{nodeCard('ticket_handler', true)}</div>
      {!!route && ['clarification', 'compliance_checker'].includes(route) && <p className="bypass-note">{route === 'clarification' ? '澄清回复' : '安全咨询'}直接进入合规审查</p>}
      <div className="flow-connector"><ChevronDown size={14} /></div>
      {nodeCard('compliance_check')}
      <div className="flow-connector"><ChevronDown size={14} /></div>
      {supervisor('synthesize', 'Supervisor 汇总')}
    </div>
    {!!taskPlan?.tasks?.length && <section className="workflow-detail"><h3>本轮任务计划</h3><ol>{taskPlan.tasks.map(task => <li key={task.id}><span>{label(task.kind)}</span><strong>{label(task.status)}</strong></li>)}</ol></section>}
    {(action || execution) && <section className="workflow-detail"><h3>确认与办理</h3><p>{execution?.ticket_id ? `办理回执：${execution.ticket_id}` : action ? label(action.status) : '尚无办理回执'}</p><small>确认、审批与提交结果以申请卡片实时状态为准。</small></section>}
    {!!trace?.steps.length && <details className="workflow-detail"><summary>查看实际执行顺序（{trace.steps.length} 个节点）</summary><ol>{trace.steps.map((step, i) => <li key={`${i}-${step.node}`}><span>{nodes.find(n => n.id === step.node)?.label || ({ task_planner: '任务规划', task_dispatch: '任务分派', action_review: '动作审查', approval_gate: '确认与审批检查', execute_action: '提交办理', execution_review: '结果核验' } as Record<string, string>)[step.node] || step.node}</span><strong>{({ done: '已完成', running: '执行中', blocked: '已拦截', error: '已中断' })[step.status]}</strong></li>)}</ol></details>}
    <p className="workflow-footer">显示真实节点事件和结果摘要。知识与工单按需调用；Supervisor 负责分派与汇总。</p>
  </section>;
}
