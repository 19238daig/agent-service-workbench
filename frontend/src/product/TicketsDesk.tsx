import { useState } from 'react';
import { RefreshCw, ClipboardList, ArrowRight } from 'lucide-react';
import { post, useCommand, useResource } from './api';
import { date, label, type ActionRequest, type Ticket, type TicketEvent } from './types';
import ActionCard from './ActionCard';

function TicketDetail({ id, onDraft, onChanged, onSelfService }: {
  id: string; onDraft: (text: string) => void; onChanged: () => void; onSelfService?: (ticket: Ticket) => void;
}) {
  const detail = useResource<{ ticket: Ticket }>(`/api/tickets/${encodeURIComponent(id)}`);
  const [eventOffset, setEventOffset] = useState(0);const events = useResource<{ events: TicketEvent[]; has_more: boolean }>(`/api/tickets/${encodeURIComponent(id)}/events?limit=20&offset=${eventOffset}`);
  const staff = useResource<{ staff: { id: string; display_name: string }[] }>(detail.data?.ticket.can_assign ? '/api/support/staff' : null);
  const [assignee, setAssignee] = useState('');const [reason, setReason] = useState('');const [target, setTarget] = useState('');const [notice, setNotice] = useState('');const command = useCommand();
  const ticket = detail.data?.ticket;
  function refresh() { detail.refresh();events.refresh();onChanged(); }
  async function assign() {
    if (!ticket) return;
    await command.run(async () => { await post(`/api/tickets/${encodeURIComponent(id)}/assignment`, { assignee_id: assignee || null, expected_version: ticket.version, reason });setNotice('分派已更新。'); });refresh();
  }
  async function transition() {
    if (!ticket) return;
    if (ticket.source === 'approved_action') {
      onDraft(`我要办理已有服务工单的状态变更：工单号 ${id}，目标状态 ${target}（${label(target)}），当前版本 ${ticket.version}，处理原因：${reason}。请为这项办理请求生成待本人确认和独立审批的草稿。`);return;
    }
    await command.run(async () => { await post(`/api/tickets/${encodeURIComponent(id)}/transitions`, { target_status: target, expected_version: ticket.version, reason });setNotice('工单状态已更新。'); });refresh();
  }
  return <section className="desk-card ticket-detail" aria-label="工单详情">
    <header className="desk-card-heading"><ClipboardList size={18} aria-hidden="true" /><h2>工单详情</h2><button className="desk-icon" onClick={refresh} aria-label="刷新工单详情"><RefreshCw size={16} /></button></header>
    {detail.loading && !ticket && <p role="status">正在读取工单…</p>}{detail.error && <p className="desk-error" role="alert">{detail.error}</p>}
    {ticket && <><div className="desk-line"><code>{ticket.ticket_id}</code><span className="desk-status">{label(ticket.status)}</span></div><h3>{ticket.summary}</h3><p className="desk-body">{ticket.details || '未补充描述'}</p>
      <dl className="desk-facts"><div><dt>类型 / 优先级</dt><dd>{label(ticket.type)} / {label(ticket.priority)}</dd></div><div><dt>订单号</dt><dd>{ticket.order_id || '—'}</dd></div>
        {ticket.requested_amount && <div><dt>申请金额</dt><dd>{ticket.requested_amount} 元</dd></div>}<div><dt>最近更新</dt><dd>{date(ticket.updated_at)}</dd></div><div><dt>当前版本</dt><dd>{ticket.version}</dd></div></dl>
      {ticket.can_assign && onSelfService && ticket.status !== 'escalated' &&
        <button type="button" className="desk-secondary" onClick={() => onSelfService(ticket)}>自助处理或转人工</button>}
      {ticket.can_assign && <form className="desk-form-section" onSubmit={e => { e.preventDefault();void assign(); }}><h3>选择处理客服</h3>
        <label className="desk-field">分派给<select value={assignee} onChange={e => setAssignee(e.target.value)} disabled={command.busy}><option value="">暂不分派 / 撤销分派</option>{staff.data?.staff.map(person => <option key={person.id} value={person.id}>{person.display_name}{ticket.assignee_id === person.id ? '（当前）' : ''}</option>)}</select></label>
        {staff.error && <p className="desk-error" role="alert">{staff.error}</p>}<label className="desk-field">分派说明<input required maxLength={500} value={reason} onChange={e => setReason(e.target.value)} disabled={command.busy} /></label>
        <button className="desk-secondary" disabled={command.busy || !!staff.error || !reason.trim() || (assignee || null) === (ticket.assignee_id || null)}>确认分派</button></form>}
      {!!ticket.allowed_transitions.length && <form className="desk-form-section" onSubmit={e => { e.preventDefault();void transition(); }}><h3>处理工单</h3>
        <label className="desk-field">目标状态<select required value={target} onChange={e => setTarget(e.target.value)} disabled={command.busy}><option value="">选择下一状态</option>{ticket.allowed_transitions.map(s => <option key={s} value={s}>{label(s)}</option>)}</select></label>
        <label className="desk-field">处理原因<textarea required maxLength={500} value={reason} rows={2} onChange={e => setReason(e.target.value)} disabled={command.busy} /></label>
        {ticket.source === 'approved_action' && <p className="desk-hint">此工单由受保护申请创建。状态变更将在对话中生成草稿，经确认及独立审批后办理。</p>}
        <button className="desk-primary" disabled={!target || !reason.trim() || command.busy}>{ticket.source === 'approved_action' ? '生成处理草稿' : '确认更新状态'}<ArrowRight size={16} aria-hidden="true" /></button>
      </form>}
      <section className="desk-form-section" aria-label="工单处理记录"><h3>处理记录</h3>{events.error && <p className="desk-error" role="alert">{events.error}</p>}
        <ol className="desk-timeline">{events.data?.events.map(event => <li key={event.id}><div className="desk-line"><strong>{label(event.to_status)}</strong><time>{date(event.created_at)}</time></div><p>{event.note || '已记录状态变化'}</p><small>{{ customer: '用户', support: '客服', admin: '管理员' }[event.actor_role || ''] || '历史记录'}{event.ticket_version ? ` · 版本 ${event.ticket_version}` : ''}</small></li>)}</ol>
        <div className="desk-pages"><button disabled={!eventOffset} onClick={() => setEventOffset(n => Math.max(0, n - 20))}>上一页记录</button><button disabled={!events.data?.has_more} onClick={() => setEventOffset(n => n + 20)}>下一页记录</button></div>
      </section></>}
    {notice && <p className="desk-success" role="status">{notice}</p>}{command.error && <p className="desk-error" role="alert">{command.error}</p>}
  </section>;
}

export default function TicketsDesk({ support = false, onDraft, onContinue, initialTicket, blocked, onSelfService }: {
  support?: boolean; initialTicket?: string; blocked?: boolean; onDraft: (text: string) => void;
  onContinue: (action: ActionRequest) => void; onSelfService?: (ticket: Ticket) => void;
}) {
  const [tab, setTab] = useState('tickets');const [filter, setFilter] = useState('');const [offset, setOffset] = useState(0);const [selected, setSelected] = useState(initialTicket || '');const [actionId, setActionId] = useState('');
  const tickets = useResource<{ tickets: Ticket[]; total: number; has_more: boolean }>(`${support ? '/api/support/tickets' : '/api/tickets'}?limit=20&offset=${offset}${filter ? '&status=' + filter : ''}`);
  const approvals = useResource<{ items: ActionRequest[] }>(support ? `/api/staff/action-requests?limit=20&offset=${offset}` : '/api/action-requests?limit=20', 5000);
  const pending = approvals.data?.items.filter(item => ['awaiting_user', 'awaiting_staff', 'approved', 'ready'].includes(item.status)) || [];
  function refresh() { tickets.refresh();approvals.refresh(); }
  return <main className="main-panel desk-main" id="product-main" inert={blocked || undefined}><header className="topbar"><div className="page-location"><span>工作区</span><strong>{support ? '客服工作台' : '我的工单'}</strong></div><button className="desk-secondary" onClick={refresh}><RefreshCw size={15} aria-hidden="true" />刷新列表</button></header>
    <div className="desk-page"><div className="desk-page-heading"><div><p className="desk-eyebrow">{support ? '人工服务' : '服务记录'}</p><h1>{support ? '处理分派与审批' : '查看你的申请进度'}</h1><p>{support ? '查看分派给你的工单，并独立审批其他人的申请。' : '跟踪工单、选择客服，查看已确认的办理结果。'}</p></div></div>
      {support && <div className="desk-tabs" role="tablist" aria-label="客服工作区"><button role="tab" aria-selected={tab === 'tickets'} onClick={() => { setTab('tickets');setOffset(0); }}>待我处理</button><button role="tab" aria-selected={tab === 'approvals'} onClick={() => { setTab('approvals');setOffset(0); }}>待审批申请</button></div>}
      {!support && !!pending.length && <section className="desk-pending" aria-label="待确认的申请"><h2>待确认 / 待审批</h2>{pending.map(item => <button key={item.action_id} onClick={() => setActionId(item.action_id)}><span>{String(item.parameters.summary || label(item.tool))}</span><span className="desk-status">{label(item.status)}</span></button>)}{actionId && <ActionCard key={actionId} actionId={actionId} onContinue={onContinue} onChange={refresh} />}</section>}
      {support && tab === 'approvals' ? <div className="desk-split"><section className="desk-card"><h2>待独立审批</h2>{approvals.loading && <p role="status">正在读取…</p>}{approvals.error && <p className="desk-error" role="alert">{approvals.error}</p>}{!pending.length && !approvals.loading && <p className="desk-empty">暂无待审批申请。</p>}
        <div className="desk-list">{pending.map(item => <button key={item.action_id} className={actionId === item.action_id ? 'selected' : ''} onClick={() => setActionId(item.action_id)}><strong>{label(item.tool)}</strong><span>{String(item.parameters.summary || item.parameters.ticket_id || '查看申请内容')}</span><small>有效期至 {date(item.expires_at)}</small></button>)}</div>
        <div className="desk-pages"><button disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><button disabled={(approvals.data?.items.length || 0) < 20} onClick={() => setOffset(n => n + 20)}>下一页</button></div></section>
        {actionId ? <ActionCard key={actionId} actionId={actionId} staff onChange={refresh} /> : <div className="desk-empty desk-card">选择一项申请查看完整内容。</div>}</div>
        : <><div className="desk-toolbar"><label>工单状态<select aria-label="筛选工单状态" value={filter} onChange={e => { setFilter(e.target.value);setOffset(0); }}><option value="">全部状态</option>{['created', 'processing', 'pending_review', 'escalated', 'resolved', 'closed'].map(s => <option key={s} value={s}>{label(s)}</option>)}</select></label><span>{tickets.data ? `共 ${tickets.data.total} 张工单` : '正在读取工单'}</span></div>
          <div className="desk-split"><section className="desk-card"><h2>{support ? '分派给我的工单' : '我的工单列表'}</h2>{tickets.error && <p className="desk-error" role="alert">{tickets.error}</p>}{tickets.loading && !tickets.data && <p role="status">正在读取…</p>}{!tickets.loading && !tickets.data?.tickets.length && <p className="desk-empty">暂无符合条件的工单。</p>}
            <div className="desk-list">{tickets.data?.tickets.map(ticket => <button key={ticket.ticket_id} className={selected === ticket.ticket_id ? 'selected' : ''} onClick={() => setSelected(ticket.ticket_id)}><div className="desk-line"><code>{ticket.ticket_id}</code><span className="desk-status">{label(ticket.status)}</span></div><strong>{ticket.summary}</strong><small>{label(ticket.type)} · {date(ticket.updated_at)}</small></button>)}</div>
            <div className="desk-pages"><button disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><button disabled={!tickets.data?.has_more} onClick={() => setOffset(n => n + 20)}>下一页</button></div></section>
            {selected ? <TicketDetail key={selected} id={selected} onDraft={onDraft} onChanged={tickets.refresh} onSelfService={onSelfService} /> : <div className="desk-empty desk-card">选择一张工单查看状态和处理记录。</div>}
          </div></>}
    </div></main>;
}
