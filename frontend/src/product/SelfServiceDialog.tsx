import { useEffect, useId, useRef, useState } from 'react';
import { BookOpen, X } from 'lucide-react';
import { apiFetch } from '../auth';
import { post, request, useCommand } from './api';
import type { Ticket } from './types';

type Kind = 'manual' | 'damage' | 'compensation' | 'other';
export type ServiceIssue = { kind?: Kind; issue?: string; model_number?: string; order_id?: string; ticket?: Ticket; autoLookup?: boolean };
type Receipt = { ticket_id: string; ticket_status: string; assigned_to_staff: boolean };
type CaseResult = {
  id: string;
  request: { kind: Kind; issue: string; model_number?: string | null; order_id?: string | null };
  status: 'manual_available' | 'handoff_available' | 'expired' | 'resolved' | 'handoff';
  manual?: { title: string; version: string; source: string; publisher: string; file_url: string };
  receipt?: Receipt;
};

const manualWords = /说明书|使用手册|用户手册/;
const riskWords = /破损|损坏|损毁|赔偿|赔付|补偿|退款|退货|漏电|冒烟|受伤/;
export function serviceIssueFromChat(message: string): ServiceIssue | undefined {
  const issue = message.trim();
  if (issue.length < 2 || issue.length > 500) return;
  const order_id = /\bORDER-[A-Za-z0-9_-]+\b/i.exec(issue)?.[0].toUpperCase();
  if (/转人工|交给人工|找人工客服/.test(issue)) {
    const kind = /赔偿|赔付|补偿/.test(issue) ? 'compensation' : /破损|损坏|损毁/.test(issue) ? 'damage' : 'other';
    return { kind, issue, order_id };
  }
  if (manualWords.test(issue) && /缺|没有|找|补发|发一份|下载|提供|在哪/.test(issue) && !riskWords.test(issue)) {
    const model_number = /型号(?:是|为)?[：:\s]*([A-Za-z0-9][A-Za-z0-9._-]{0,63})/i.exec(issue)?.[1].replace(/\.$/, '');
    return { kind: 'manual', issue, order_id, model_number, autoLookup: !!model_number };
  }
}

export default function SelfServiceDialog({ userId, initial, onClose, onTicket }: {
  userId: string;
  initial: ServiceIssue;
  onClose: () => void;
  onTicket: (ticket: string) => void;
}) {
  const title = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const submitted = useRef<{ body: string; id: string } | undefined>(undefined);
  const [kind, setKind] = useState<Kind>(initial.kind || 'manual');
  const [issue, setIssue] = useState(initial.issue || '');
  const [model, setModel] = useState(initial.model_number || '');
  const [order, setOrder] = useState(initial.order_id || '');
  const [result, setResult] = useState<CaseResult>();
  const [fileViewed, setFileViewed] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const command = useCommand();
  const storageKey = 'smartcs.self-service:' + userId + ':' + (initial.ticket?.ticket_id || 'new');
  const [loading, setLoading] = useState(!initial.issue);
  const [previousId, setPreviousId] = useState('');
  const automaticLookupStarted = useRef(false);

  useEffect(() => {
    const popup = dialog.current;
    popup?.showModal();
    return () => popup?.close();
  }, []);

  useEffect(() => {
    if (initial.issue) return;
    const controller = new AbortController();
    let id = '';
    try { id = sessionStorage.getItem(storageKey) || ''; } catch { /* storage optional */ }
    if (!id) { setLoading(false); return; }
    setPreviousId(id);
    void request<CaseResult>('/api/self-service/cases/' + encodeURIComponent(id), { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setResult(value); })
      .catch(() => { if (!controller.signal.aborted) command.setError('暂时无法读取上次结果，可刷新核对或重新开始。'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [initial.issue, storageKey]);

  function remember(id: string) {
    try {
      if (id) sessionStorage.setItem(storageKey, id);
      else sessionStorage.removeItem(storageKey);
    } catch { /* storage optional */ }
    setPreviousId(id);
  }

  async function start() {
    await command.run(async () => {
      const body = {
        kind, issue: issue.trim(), model_number: kind === 'manual' ? model.trim() : null,
        order_id: initial.ticket?.order_id || order.trim() || null, ticket_id: initial.ticket?.ticket_id || null,
      };
      const serial = JSON.stringify(body);
      if (submitted.current?.body !== serial) submitted.current = { body: serial, id: crypto.randomUUID() };
      remember(submitted.current.id);
      const value = await post<CaseResult>('/api/self-service/cases', { ...body, request_id: submitted.current.id });
      setFileViewed(false); setConfirmed(false); setResult(value);
    });
  }

  useEffect(() => {
    if (!initial.autoLookup || automaticLookupStarted.current) return;
    automaticLookupStarted.current = true;
    void start();
  }, [initial.autoLookup]);

  async function decide(decision: 'resolved' | 'handoff') {
    if (!result) return;
    await command.run(async () => {
      const value = await post<CaseResult>('/api/self-service/cases/' + result.id + '/decision', { decision });
      setResult(value);
    });
  }

  async function refresh() {
    const id = result?.id || previousId;
    if (id) await command.run(async () => setResult(await request<CaseResult>('/api/self-service/cases/' + encodeURIComponent(id))));
  }

  async function download() {
    const manual = result?.manual;
    if (!manual || !/^\/api\/knowledge\/documents\/[A-Za-z0-9_-]+\/file$/.test(manual.file_url)) return;
    await command.run(async () => {
      const response = await apiFetch(manual.file_url, { cache: 'no-store', signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw new Error('说明书暂不可用，请刷新核对或转人工。');
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = manual.source.replace(/[/\\]/g, '_');
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
      setFileViewed(true);
    });
  }

  function startOver() {
    remember('');
    command.setError('');
    if (result && !result.receipt) {
      setKind(result.request.kind); setIssue(result.request.issue);
      setModel(result.request.model_number || ''); setOrder(result.request.order_id || '');
    } else if (result?.receipt) {
      setKind('manual'); setIssue(''); setModel(''); setOrder('');
    }
    submitted.current = undefined;
    setFileViewed(false); setConfirmed(false); setResult(undefined);
  }

  const receipt = result?.receipt;
  return <dialog ref={dialog} className="self-service-dialog" aria-labelledby={title}
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="self-service-content">
      <header className="desk-card-heading">
        <BookOpen size={20} aria-hidden="true" /><h2 id={title}>售后自助处理</h2>
        <button type="button" className="desk-icon" aria-label="关闭自助处理" onClick={onClose}><X size={19} /></button>
      </header>
      {initial.ticket && <p className="desk-meta">关联工单 {initial.ticket.ticket_id}</p>}
      {loading ? <p role="status">正在核对上次处理结果…</p> : receipt ? <>
        <p className="desk-success" role="status">{result?.status === 'resolved' ? '已记录为自助解决。'
          : receipt.assigned_to_staff ? '已转人工，客服可查看问题摘要和待核实事项。' : '已记录人工处理请求，等待分派客服。'}</p>
        <div className="desk-actions">
          <button className="desk-primary" onClick={() => onTicket(receipt.ticket_id)}>查看工单 {receipt.ticket_id}</button>
          {!initial.ticket && <button className="desk-secondary" onClick={startOver}>处理其他问题</button>}
        </div>
      </> : result ? <>
        <p className="desk-hint">{result.request.issue}</p>
        {result.manual ? <>
          <h3>{result.manual.title}</h3>
          <p className="desk-meta">型号 {result.request.model_number} · 版本 {result.manual.version}{result.manual.publisher && ' · ' + result.manual.publisher}</p>
          <p className="desk-hint">下载后先核对型号和内容。确认问题解决后，系统会记录工单结果。</p>
          <button className="desk-secondary" disabled={command.busy} onClick={() => void download()}>下载说明书</button>
          <label className="desk-check"><input type="checkbox" checked={confirmed} disabled={!fileViewed || command.busy}
            onChange={e => setConfirmed(e.target.checked)} />我已核对说明书，问题已解决</label>
        </> : <p role="status">{result.status === 'expired' ? '本次查找已过期。你可以重新查找或交给人工核实。'
          : result.request.kind === 'manual' ? '这次没有可用于自助处理的匹配说明书，可把已知情况交给客服。' : '这个问题需要人工核实。提交后会把情况和待核实事项一起记录到工单。'}</p>}
        <div className="desk-actions">
          {result.manual && <button className="desk-primary" type="button" disabled={command.busy || !fileViewed || !confirmed}
            onClick={() => void decide('resolved')}>确认已解决</button>}
          <button type="button" className={result.manual ? 'desk-secondary' : 'desk-primary'} disabled={command.busy}
            onClick={() => void decide('handoff')}>转人工处理</button>
          <button type="button" className="desk-text" disabled={command.busy} onClick={startOver}>重新填写</button>
        </div>
      </> : <form onSubmit={event => { event.preventDefault(); void start(); }}>
        <label className="desk-field">问题类型<select value={kind} disabled={command.busy} onChange={e => setKind(e.target.value as Kind)}>
          <option value="manual">查找商品说明书</option><option value="damage">商品或包裹破损</option>
          <option value="compensation">赔偿问题</option><option value="other">其他需要人工核实的问题</option>
        </select></label>
        {kind === 'manual' && <label className="desk-field">商品型号<input value={model} required maxLength={64} pattern="[A-Za-z0-9][A-Za-z0-9._\-]{0,63}"
          disabled={command.busy} onChange={e => setModel(e.target.value)} /><small>填写包装或铭牌上的完整型号。</small></label>}
        {!initial.ticket && <label className="desk-field">本人订单号（选填）<input value={order} maxLength={64} disabled={command.busy} onChange={e => setOrder(e.target.value)} /></label>}
        <label className="desk-field">问题描述<textarea value={issue} required minLength={2} maxLength={500} rows={3}
          disabled={command.busy} onChange={e => setIssue(e.target.value)} /></label>
        <button className="desk-primary" disabled={command.busy}>{kind === 'manual' ? '查找说明书' : '核对转人工信息'}</button>
      </form>}
      {command.busy && <p role="status" className="desk-meta">正在处理，请稍候…</p>}
      {command.error && <p role="alert" className="desk-error">{command.error}</p>}
      {command.error && previousId && <button className="desk-secondary" disabled={command.busy} onClick={() => void refresh()}>刷新处理结果</button>}
    </div>
  </dialog>;
}
