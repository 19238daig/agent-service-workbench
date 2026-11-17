import { useState } from 'react';
import { CheckCircle2, RefreshCw, ShieldCheck } from 'lucide-react';
import { post, useCommand, useResource } from './api';
import { date, label, type ActionRequest } from './types';

const fields: Record<string, string> = { type: '申请类型', order_id: '订单号', requested_amount: '申请金额（元）', priority: '优先级', summary: '申请摘要', details: '问题描述', ticket_id: '工单号', target_status: '目标状态', reason: '处理说明' };
export default function ActionCard({ actionId, staff = false, onContinue, onChange }: {
  actionId: string; staff?: boolean; onContinue?: (action: ActionRequest) => void; onChange?: () => void;
}) {
  const source = useResource<ActionRequest>(`/api/${staff ? 'staff/' : ''}action-requests/${encodeURIComponent(actionId)}`, 5000);
  const command = useCommand();const [checked, setChecked] = useState(false);const [note, setNote] = useState('');const [notice, setNotice] = useState('');
  const action = source.data;
  async function decide(decision: 'approve' | 'reject') {
    if (!action) return;
    await command.run(async () => {
      const current = await post<ActionRequest>(`/api/${staff ? 'staff/' : ''}action-requests/${encodeURIComponent(actionId)}/${staff ? 'decide' : 'confirm'}`,
        { parameter_hash: action!.parameter_hash, decision, note });
      source.setData(current);setChecked(false);onChange?.();
      setNotice(decision === 'reject' ? '已拒绝，此申请不会继续办理。' : current.status === 'awaiting_staff' ? '已确认，正在等待另一位客服审批。' : staff ? '审批已记录，申请人可继续办理。' : '确认已记录，请继续办理。');
    });
    source.refresh();
  }
  async function cancel() {
    if (!action) return;
    await command.run(async () => { const value = await post<ActionRequest>(`/api/action-requests/${encodeURIComponent(actionId)}/cancel`, { parameter_hash: action!.parameter_hash });source.setData(value);onChange?.();setNotice('申请已取消。'); });source.refresh();
  }
  return <section className="desk-card action-card" aria-label={staff ? '人工审批详情' : '申请确认'}>
    <header className="desk-card-heading"><ShieldCheck size={18} aria-hidden="true" /><h3>{staff ? '人工审批' : '申请确认'}</h3><button className="desk-icon" type="button" onClick={source.refresh} aria-label="刷新申请状态"><RefreshCw size={16} /></button></header>
    {source.loading && !action && <p role="status">正在核对申请…</p>}{source.error && <p className="desk-error" role="alert">{source.error}</p>}
    {action && <><div className="desk-line"><strong>{label(action.tool)}</strong><span className="desk-status">{label(action.status)}</span></div>
      <dl className="desk-facts">{Object.entries(action.parameters).filter(([key]) => fields[key]).map(([key, value]) => <div key={key}><dt>{fields[key]}</dt><dd>{['type', 'priority', 'target_status'].includes(key) ? label(String(value)) : String(value ?? '—')}</dd></div>)}</dl>
      <p className="desk-meta">有效期至 {date(action.expires_at)}{action.required_approval === 'staff' ? ' · 需本人确认及独立客服审批' : ' · 需本人确认'}</p>
      {action.parameters.type === 'refund' && <p className="desk-hint">此操作登记退款申请，不代表资金已退回。</p>}
      {action.status === 'executed' && <p className="desk-success"><CheckCircle2 size={16} aria-hidden="true" />已提交办理，可在工单中查看真实结果。</p>}
      {((staff && action.status === 'awaiting_staff') || (!staff && action.status === 'awaiting_user')) && <>
        <label className="desk-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} disabled={command.busy} />{staff ? '我已核对申请内容，同意独立审批' : '我已核对以上申请内容'}</label>
        <label className="desk-field">{staff ? '审批说明' : '补充说明'}<textarea value={note} maxLength={500} onChange={e => setNote(e.target.value)} rows={2} disabled={command.busy} /></label>
        <div className="desk-actions"><button className="desk-primary" disabled={!checked || command.busy || !!source.error} onClick={() => void decide('approve')}>{command.busy ? '正在提交…' : staff ? '批准申请' : '确认申请'}</button><button className="desk-secondary" disabled={command.busy || !note.trim()} onClick={() => void decide('reject')}>拒绝申请</button></div>
      </>}
      {!staff && ['approved', 'ready'].includes(action.status) && <button className="desk-primary" disabled={command.busy || !!source.error} onClick={() => onContinue?.(action)}>继续办理</button>}
      {!staff && ['awaiting_user', 'awaiting_staff', 'approved'].includes(action.status) && <button className="desk-text" disabled={command.busy} onClick={() => void cancel()}>取消申请</button>}
      {action.status === 'awaiting_staff' && !staff && <p className="desk-hint" role="status">等待独立客服审批，通过后可继续办理。</p>}
      {['expired', 'stale'].includes(action.status) && <p className="desk-hint">此草稿已失效，请在对话中重新核对信息并生成申请。</p>}
    </>}
    {notice && <p className="desk-success" role="status">{notice}</p>}{command.error && <p className="desk-error" role="alert">{command.error}</p>}
  </section>;
}
