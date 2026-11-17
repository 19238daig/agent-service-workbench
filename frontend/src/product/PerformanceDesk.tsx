import { Activity, RefreshCw } from 'lucide-react';
import { useResource } from './api';

type Latency = { samples: number; p50: number | null; p95: number | null };
type Report = { runs: number; observed_attempts: number; retries: number; cache_hits: number; cache_misses: number;
  known_reported_tokens: number; unknown_usage_calls: number; final_reply_ms: Latency; first_execution_progress_ms: Latency;
  failures: Record<string, number>; run_failures?: Record<string, number>; nodes: { node: string; latency_ms: Latency }[];
  models: { role: string; model: string; kind: string; attempts: number; failed: number; latency_ms: Latency; queue_ms: number; known_input_tokens?: number; known_output_tokens?: number; unknown_usage_calls?: number }[] };
const names: Record<string,string> = { intent_router: '意图路由', knowledge_rag: '知识检索', ticket_handler: '工单处理', compliance_checker: '合规审查', compliance_check: '合规审查', supervisor_route: '任务分派', synthesize: '回复汇总', unknown: '其他', action_review: '动作审查', approval_gate: '确认与审批', execute_action: '提交办理', execution_review: '结果核验', execution_synthesize: '执行结果汇总', task_after_review: '审查后分派', task_collect: '汇集结果', task_dispatch: '任务调度', task_initialize: '准备任务', task_planner: '任务规划' };
const ms = (value?: number | null) => value == null ? '未记录' : `${(value / 1000).toFixed(2)} 秒`;

export default function PerformanceDesk({ blocked = false }: { blocked?: boolean }) {
  const source = useResource<Report>('/api/admin/performance'); const data = source.data;
  const maximum = Math.max(1, ...(data?.nodes.map(n => n.latency_ms.p95 || 0) || []));
  return <main className="desk-main performance-main" id="product-main" inert={blocked || undefined}>
    <header className="topbar"><span>工作区 <span aria-hidden="true">›</span> 运行观测</span><span className="environment-badge">管理员</span></header>
    <div className="desk-page performance-page">
      <div className="desk-page-heading"><div><span className="desk-eyebrow"><Activity size={16} aria-hidden="true" />模型运行</span><h1>运行观测</h1><p>最近200轮的实际记录。旧记录缺少的指标显示为未记录。</p></div><button className="desk-secondary" onClick={source.refresh} disabled={source.loading}><RefreshCw size={16} aria-hidden="true" />刷新记录</button></div>
      {source.error && <p className="desk-error" role="alert">{source.error}</p>}{source.loading && !data && <p role="status">正在读取运行记录…</p>}
      {data && <>
        <dl className="performance-stats"><div><dt>首个执行进度 · p95</dt><dd>{ms(data.first_execution_progress_ms.p95)}</dd><small>{data.first_execution_progress_ms.samples}轮有记录，不是首字时间</small></div><div><dt>本轮处理耗时 · p95</dt><dd>{ms(data.final_reply_ms.p95)}</dd><small>{data.final_reply_ms.samples}轮，含中断与失败</small></div><div><dt>证据缓存命中 / 未命中</dt><dd>{data.cache_hits} / {data.cache_misses}</dd><small>答案与业务办理结果不缓存</small></div><div><dt>已报告 Token 小计</dt><dd>{data.known_reported_tokens.toLocaleString()}</dd><small>{data.unknown_usage_calls}次用量不完整</small></div></dl>
        <section className="desk-card"><h2>节点耗时</h2><p>按节点聚合p95，不能相加推算整轮耗时。</p><div className="performance-bars">{data.nodes.map(n => <div key={n.node}><span>{names[n.node] || n.node}</span><span className="performance-track"><i style={{ width: `${(n.latency_ms.p95 || 0) / maximum * 100}%` }} /></span><strong>{ms(n.latency_ms.p95)}</strong></div>)}</div>{!data.nodes.length && <p>尚无节点记录。</p>}</section>
        <section className="desk-card"><h2>模型调用</h2><p>{data.runs}轮 · {data.observed_attempts}次请求 · {data.retries}次显式重试。输入/输出为已报告Token小计。</p><div className="performance-table"><table><thead><tr><th>职责 / 模型</th><th>次数</th><th>失败</th><th>p50</th><th>p95</th><th>已报告输入 / 输出</th><th>排队总计</th></tr></thead><tbody>{data.models.map(m => <tr key={`${m.role}-${m.model}-${m.kind}`}><td><strong>{names[m.role] || m.role}</strong><small>{m.model} · {m.kind}</small></td><td>{m.attempts}</td><td>{m.failed}</td><td>{ms(m.latency_ms.p50)}</td><td>{ms(m.latency_ms.p95)}</td><td>{m.kind === 'chat' ? <>{m.known_input_tokens ?? '未记录'} / {m.known_output_tokens ?? '未记录'}{!!m.unknown_usage_calls && <small>{m.unknown_usage_calls}次用量不完整</small>}</> : '不适用'}</td><td>{ms(m.queue_ms)}</td></tr>)}</tbody></table></div></section>
        <section className="desk-card"><h2>错误分布</h2>{Object.keys(data.failures).length ? <ul>{Object.entries(data.failures).map(([code, count]) => <li key={code}>{code}：{count}次</li>)}</ul> : <p>当前范围内没有已记录的模型失败。</p>}</section>
        <section className="desk-card"><h2>运行中断</h2>{Object.keys(data.run_failures || {}).length ? <ul>{Object.entries(data.run_failures || {}).map(([code,count]) => <li key={code}>{code}：{count}轮</li>)}</ul> : <p>当前范围内没有已记录的运行中断。</p>}</section>
      </>}
    </div>
  </main>;
}
