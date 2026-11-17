import { useState } from 'react';
import { label, type TaskPlan } from './types';
const fields: Record<string, string> = { order_id: '订单号', requested_amount: '申请金额', ticket_id: '工单号', reason: '处理原因', details: '问题描述', summary: '申请摘要', type: '申请类型', priority: '优先级' };
export default function TaskCard({ plan, canFill, onFill }: { plan?: TaskPlan; canFill: boolean; onFill: (value: string) => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  if (!plan?.tasks?.length) return null;
  const missing = [...new Set(plan.missing_fields || [])];
  return <section className="task-card" aria-label="本轮任务计划"><h3>本轮任务 <span>{plan.tasks.filter(t => t.status === 'completed').length}/{plan.tasks.length}</span></h3>
    <ol>{plan.tasks.map(t => <li key={t.id}><span>{label(t.kind)}</span><span className="desk-status">{label(t.status)}</span></li>)}</ol>
    {canFill && plan.status === 'needs_information' && !!missing.length && <form onSubmit={e => { e.preventDefault();onFill(missing.map(key => `${fields[key] || '补充信息'}：${values[key] || ''}`).join('；')); }}>
      <p>补齐以下信息后继续原任务。</p>{missing.map(key => <label className="desk-field" key={key}>{fields[key] || '补充信息'}<input required maxLength={key === 'details' ? 2000 : 500} value={values[key] || ''} onChange={e => setValues(v => ({ ...v, [key]: e.target.value }))} /></label>)}
      <button className="desk-primary" type="submit">补充并继续</button>
    </form>}
  </section>;
}
