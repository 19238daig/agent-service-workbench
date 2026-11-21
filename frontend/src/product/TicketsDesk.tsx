import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft, ArrowRight, ArrowUpRight, CircleCheck, CircleHelp, CircleMinus,
  ClipboardCheck, ClipboardList, Clock3, Hourglass, RefreshCw,
} from 'lucide-react';
import { post, useCommand, useResource } from './api';
import { date, label, type ActionRequest, type Ticket, type TicketEvent } from './types';
import ActionCard from './ActionCard';

const PAGE_SIZE = 20;
const ticketStatusIcons = {
  created: Clock3,
  processing: Hourglass,
  pending_review: ClipboardCheck,
  escalated: ArrowUpRight,
  resolved: CircleCheck,
  closed: CircleMinus,
};
const ticketStatuses = Object.keys(ticketStatusIcons);

function TicketStatus({ status }: { status: string }) {
  const knownStatus = Object.hasOwn(ticketStatusIcons, status) ? status as keyof typeof ticketStatusIcons : undefined;
  const Icon = knownStatus ? ticketStatusIcons[knownStatus] : CircleHelp;
  return <span className="ticket-status" data-status={knownStatus}>
    <Icon size={14} aria-hidden="true" />{label(status)}
  </span>;
}

function TicketDetail({ id, onDraft, onChanged, onSelfService, onBack }: {
  id: string;
  onDraft: (text: string) => void;
  onChanged: () => void;
  onSelfService?: (ticket: Ticket) => void;
  onBack?: () => void;
}) {
  const url = '/api/tickets/' + encodeURIComponent(id);
  const detail = useResource<{ ticket: Ticket }>(url);
  const [eventOffset, setEventOffset] = useState(0);
  const events = useResource<{ events: TicketEvent[]; has_more: boolean }>(`${url}/events?limit=${PAGE_SIZE}&offset=${eventOffset}`);
  const ticket = detail.data?.ticket;
  const staff = useResource<{ staff: { id: string; display_name: string }[] }>(ticket?.can_assign ? '/api/support/staff' : null);
  const [assignee, setAssignee] = useState<string>();
  const [assignmentReason, setAssignmentReason] = useState('');
  const [target, setTarget] = useState('');
  const [transitionReason, setTransitionReason] = useState('');
  const [notice, setNotice] = useState('');
  const command = useCommand();
  const heading = useRef<HTMLHeadingElement>(null);
  const mobileDetail = !!onBack;
  const currentAssignee = ticket?.assignee_id || '';
  const selectedAssignee = assignee ?? currentAssignee;
  const assignedStaff = staff.data?.staff.find(person => person.id === currentAssignee);
  const activeTarget = ticket?.allowed_transitions.includes(target) ? target : '';

  useEffect(() => {
    if (mobileDetail) heading.current?.focus();
  }, [mobileDetail]);

  function refresh() {
    detail.refresh();
    events.refresh();
    onChanged();
  }

  async function assign() {
    if (!ticket || !staff.data || selectedAssignee === currentAssignee || !assignmentReason.trim()) return;
    setNotice('');
    await command.run(async () => {
      await post(url + '/assignment', {
        assignee_id: selectedAssignee || null,
        expected_version: ticket.version,
        reason: assignmentReason.trim(),
      });
      setAssignee(undefined);
      setAssignmentReason('');
      setNotice('分派已更新。');
    });
    refresh();
  }

  async function transition() {
    if (!ticket || !activeTarget || !transitionReason.trim()) return;
    setNotice('');
    if (ticket.source === 'approved_action') {
      onDraft(`我要办理已有服务工单的状态变更：工单号 ${id}，目标状态 ${activeTarget}（${label(activeTarget)}），当前版本 ${ticket.version}，处理原因：${transitionReason.trim()}。请为这项办理请求生成待本人确认和独立审批的草稿。`);
      return;
    }
    await command.run(async () => {
      await post(url + '/transitions', {
        target_status: activeTarget,
        expected_version: ticket.version,
        reason: transitionReason.trim(),
      });
      setTarget('');
      setTransitionReason('');
      setNotice('工单状态已更新。');
    });
    refresh();
  }

  return <section className="desk-card ticket-detail" aria-label="工单详情">
    {onBack && <button type="button" className="desk-text ticket-back" onClick={onBack}>
      <ArrowLeft size={16} aria-hidden="true" />返回工单列表
    </button>}
    <header className="desk-card-heading">
      <ClipboardList size={18} aria-hidden="true" />
      <h2 ref={heading} tabIndex={-1}>工单详情</h2>
      <button type="button" className="desk-icon" onClick={refresh} disabled={command.busy || detail.loading}
        aria-label="刷新工单详情"><RefreshCw size={16} aria-hidden="true" /></button>
    </header>
    {detail.loading && !ticket && <p role="status">正在读取工单…</p>}
    {detail.error && <p className="desk-error" role="alert">{detail.error}</p>}
    {notice && <p className="desk-success" role="status">{notice}</p>}
    {command.error && <p className="desk-error" role="alert">{command.error}</p>}
    {ticket && <>
      <div className="desk-line"><code>{ticket.ticket_id}</code><TicketStatus status={ticket.status} /></div>
      <h3 className="ticket-summary">{ticket.summary}</h3>
      <p className="desk-body">{ticket.details || '未补充描述'}</p>
      <dl className="desk-facts">
        <div><dt>类型 / 优先级</dt><dd>{label(ticket.type)} / {label(ticket.priority)}</dd></div>
        <div><dt>订单号</dt><dd>{ticket.order_id || '—'}</dd></div>
        <div><dt>处理客服</dt><dd>{assignedStaff?.display_name || (currentAssignee ? '已分派' : '暂未分派')}</dd></div>
        {ticket.requested_amount && <div><dt>申请金额</dt><dd>{ticket.requested_amount} 元</dd></div>}
        <div><dt>最近更新</dt><dd><time dateTime={ticket.updated_at}>{date(ticket.updated_at)}</time></dd></div>
        <div><dt>当前版本</dt><dd>{ticket.version}</dd></div>
      </dl>
      {ticket.can_assign && onSelfService && ticket.status !== 'escalated' &&
        <button type="button" className="desk-secondary" onClick={() => onSelfService(ticket)}>自助处理或转人工</button>}
      {ticket.can_assign && <form className="desk-form-section" onSubmit={e => { e.preventDefault(); void assign(); }}>
        <h3>选择处理客服</h3>
        <label className="desk-field">分派给
          <select value={selectedAssignee} onChange={e => setAssignee(e.target.value)} disabled={command.busy || !staff.data}>
            <option value="">{currentAssignee ? '撤销分派' : '暂不分派'}</option>
            {currentAssignee && !assignedStaff && <option value={currentAssignee}>已分派的客服（当前）</option>}
            {staff.data?.staff.map(person => <option key={person.id} value={person.id}>
              {person.display_name}{currentAssignee === person.id ? '（当前）' : ''}
            </option>)}
          </select>
        </label>
        {staff.error && <p className="desk-error" role="alert">{staff.error}</p>}
        <label className="desk-field">分派说明
          <input required maxLength={500} value={assignmentReason} onChange={e => setAssignmentReason(e.target.value)} disabled={command.busy} />
        </label>
        <button className="desk-secondary" disabled={command.busy || !staff.data || !!staff.error || !assignmentReason.trim() || selectedAssignee === currentAssignee}>确认分派</button>
      </form>}
      {!!ticket.allowed_transitions.length && <form className="desk-form-section" onSubmit={e => { e.preventDefault(); void transition(); }}>
        <h3>处理工单</h3>
        <label className="desk-field">目标状态
          <select required value={activeTarget} onChange={e => setTarget(e.target.value)} disabled={command.busy}>
            <option value="">选择下一状态</option>
            {ticket.allowed_transitions.map(status => <option key={status} value={status}>{label(status)}</option>)}
          </select>
        </label>
        <label className="desk-field">处理原因
          <textarea required maxLength={500} value={transitionReason} rows={2} onChange={e => setTransitionReason(e.target.value)} disabled={command.busy} />
        </label>
        {ticket.source === 'approved_action' && <p className="desk-hint">此工单由受保护申请创建。状态变更将在对话中生成草稿，经确认及独立审批后办理。</p>}
        <button className="desk-primary" disabled={!activeTarget || !transitionReason.trim() || command.busy}>
          {ticket.source === 'approved_action' ? '生成处理草稿' : '确认更新状态'}<ArrowRight size={16} aria-hidden="true" />
        </button>
      </form>}
      <section className="desk-form-section" aria-label="工单处理记录">
        <h3>处理记录</h3>
        {events.error && <p className="desk-error" role="alert">{events.error}</p>}
        {events.loading && !events.data && <p role="status">正在读取处理记录…</p>}
        {events.data?.events.length === 0 && <p className="desk-hint">暂无处理记录。</p>}
        <ol className="desk-timeline">{events.data?.events.map(event => <li key={event.id}>
          <div className="desk-line"><TicketStatus status={event.to_status} /><time dateTime={event.created_at}>{date(event.created_at)}</time></div>
          <p>{event.note || '已记录状态变化'}</p>
          <small>{{ customer: '用户', support: '客服', admin: '管理员' }[event.actor_role || ''] || '历史记录'}{event.ticket_version ? ` · 版本 ${event.ticket_version}` : ''}</small>
        </li>)}</ol>
        {(eventOffset > 0 || events.data?.has_more) && <div className="desk-pages">
          <button disabled={!eventOffset} onClick={() => setEventOffset(n => Math.max(0, n - PAGE_SIZE))}>上一页记录</button>
          <button disabled={!events.data?.has_more} onClick={() => setEventOffset(n => n + PAGE_SIZE)}>下一页记录</button>
        </div>}
      </section>
    </>}
  </section>;
}

export default function TicketsDesk({ support = false, mobile, onDraft, onContinue, initialTicket, blocked, onSelfService }: {
  support?: boolean;
  mobile: boolean;
  initialTicket?: string;
  blocked?: boolean;
  onDraft: (text: string) => void;
  onContinue: (action: ActionRequest) => void;
  onSelfService?: (ticket: Ticket) => void;
}) {
  const [tab, setTab] = useState('tickets');
  const [filter, setFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(initialTicket || '');
  const [detailOpen, setDetailOpen] = useState(!!initialTicket);
  const [actionId, setActionId] = useState('');
  const listHeading = useRef<HTMLHeadingElement>(null);
  const selectedRow = useRef<HTMLButtonElement>(null);
  const restoreListFocus = useRef(false);
  const mobileDetail = mobile && tab === 'tickets' && detailOpen && !!selected;
  const tickets = useResource<{ tickets: Ticket[]; total: number; has_more: boolean }>(
    `${support ? '/api/support/tickets' : '/api/tickets'}?limit=${PAGE_SIZE}&offset=${offset}${filter ? '&status=' + filter : ''}`,
  );
  const approvals = useResource<{ items: ActionRequest[] }>(
    support ? `/api/staff/action-requests?limit=${PAGE_SIZE}&offset=${offset}` : '/api/action-requests?limit=20', 5000,
  );
  const pending = approvals.data?.items.filter(item => ['awaiting_user', 'awaiting_staff', 'approved', 'ready'].includes(item.status)) || [];

  useEffect(() => {
    if (!mobileDetail && restoreListFocus.current) {
      restoreListFocus.current = false;
      (selectedRow.current || listHeading.current)?.focus();
    }
  }, [mobileDetail]);

  function refresh() {
    tickets.refresh();
    approvals.refresh();
  }

  function changePage(nextOffset: number) {
    setOffset(nextOffset);
    setSelected('');
    setDetailOpen(false);
  }

  function changeFilter(status: string) {
    if (status === filter) return;
    setFilter(status);
    changePage(0);
  }

  function backToList() {
    restoreListFocus.current = true;
    setDetailOpen(false);
  }

  return <main className="main-panel desk-main tickets-main" id="product-main" inert={blocked || undefined}>
    <header className="topbar">
      <div className="page-location"><span>工作区</span><strong>{support ? '客服工作台' : '我的工单'}</strong></div>
      {!mobileDetail && <button className="desk-secondary" onClick={refresh}><RefreshCw size={15} aria-hidden="true" />刷新列表</button>}
    </header>
    <div className="desk-page">
      {!mobileDetail && <>
        <div className="desk-page-heading"><div>
          <h1>{support ? '处理分派与审批' : '查看你的工单进度'}</h1>
          <p>{support ? '查看分派给你的工单，并独立审批其他人的申请。' : '查看工单状态、处理客服和处理记录。'}</p>
        </div></div>
        {support && <div className="desk-tabs" role="tablist" aria-label="客服工作区">
          <button role="tab" aria-selected={tab === 'tickets'} onClick={() => { setTab('tickets'); setOffset(0); }}>待我处理</button>
          <button role="tab" aria-selected={tab === 'approvals'} onClick={() => { setTab('approvals'); setOffset(0); }}>待审批申请</button>
        </div>}
        {!support && !!pending.length && <section className="desk-pending" aria-label="待确认的申请">
          <h2>待确认 / 待审批</h2>
          {pending.map(item => <button key={item.action_id} onClick={() => setActionId(item.action_id)}>
            <span>{String(item.parameters.summary || label(item.tool))}</span><span className="desk-status">{label(item.status)}</span>
          </button>)}
          {actionId && <ActionCard key={actionId} actionId={actionId} onContinue={onContinue} onChange={refresh} />}
        </section>}
      </>}
      {support && tab === 'approvals' ? <div className="desk-split">
        <section className="desk-card">
          <h2>待独立审批</h2>
          {approvals.loading && <p role="status">正在读取…</p>}
          {approvals.error && <p className="desk-error" role="alert">{approvals.error}</p>}
          {!pending.length && !approvals.loading && <p className="desk-empty">暂无待审批申请。</p>}
          <div className="desk-list">{pending.map(item => <button key={item.action_id} className={actionId === item.action_id ? 'selected' : ''} onClick={() => setActionId(item.action_id)}>
            <strong>{label(item.tool)}</strong><span>{String(item.parameters.summary || item.parameters.ticket_id || '查看申请内容')}</span><small>有效期至 {date(item.expires_at)}</small>
          </button>)}</div>
          <div className="desk-pages">
            <button disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - PAGE_SIZE))}>上一页</button>
            <button disabled={(approvals.data?.items.length || 0) < PAGE_SIZE} onClick={() => setOffset(n => n + PAGE_SIZE)}>下一页</button>
          </div>
        </section>
        {actionId ? <ActionCard key={actionId} actionId={actionId} staff onChange={refresh} /> : <div className="desk-empty desk-card">选择一项申请查看完整内容。</div>}
      </div> : <>
        {!mobileDetail && <div className="ticket-toolbar">
          <div className="ticket-filters" role="group" aria-label="筛选工单状态">
            <button type="button" aria-pressed={!filter} onClick={() => changeFilter('')}>全部</button>
            {ticketStatuses.map(status => <button key={status} type="button" aria-pressed={filter === status} onClick={() => changeFilter(status)}>{label(status)}</button>)}
          </div>
          <span role="status">{tickets.data ? `共 ${tickets.data.total} 张工单` : tickets.loading ? '正在读取工单…' : ''}</span>
        </div>}
        <div className={mobileDetail ? undefined : 'desk-split ticket-split'}>
          {!mobileDetail && <section className="desk-card" aria-labelledby="ticket-list-title">
            <h2 ref={listHeading} id="ticket-list-title" tabIndex={-1}>{support ? '分派给我的工单' : '我的工单列表'}</h2>
            {tickets.error && <div className="ticket-empty">
              <p className="desk-error" role="alert">{tickets.error}</p>
              <button type="button" className="desk-secondary" onClick={tickets.refresh}>重试读取工单</button>
            </div>}
            {tickets.data?.tickets.length === 0 && !tickets.error && <div className="ticket-empty">
              <p>{filter ? `暂无「${label(filter)}」的工单。` : '暂无工单。'}</p>
              {filter && <button type="button" className="desk-secondary" onClick={() => changeFilter('')}>查看全部工单</button>}
            </div>}
            <div className="desk-list ticket-list">{tickets.data?.tickets.map(ticket => {
              const current = selected === ticket.ticket_id;
              return <button key={ticket.ticket_id} type="button" ref={current ? selectedRow : undefined}
                aria-current={current || undefined} className={current ? 'selected' : ''}
                onClick={() => { setSelected(ticket.ticket_id); setDetailOpen(true); }}>
                <div className="desk-line"><code>{ticket.ticket_id}</code><TicketStatus status={ticket.status} /></div>
                <strong>{ticket.summary}</strong>
                <small>{label(ticket.type)} · <time dateTime={ticket.updated_at}>{date(ticket.updated_at)}</time></small>
              </button>;
            })}</div>
            {(offset > 0 || tickets.data?.has_more) && <div className="desk-pages">
              <button disabled={!offset || tickets.loading} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}>上一页</button>
              <button disabled={!tickets.data?.has_more || tickets.loading} onClick={() => changePage(offset + PAGE_SIZE)}>下一页</button>
            </div>}
          </section>}
          {selected && (!mobile || mobileDetail)
            ? <TicketDetail key={selected} id={selected} onDraft={onDraft} onChanged={tickets.refresh} onSelfService={onSelfService} onBack={mobileDetail ? backToList : undefined} />
            : !mobile && <div className="desk-empty desk-card">选择一张工单查看详情和处理记录。</div>}
        </div>
      </>}
    </div>
  </main>;
}
