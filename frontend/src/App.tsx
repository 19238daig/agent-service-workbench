import CitationBlock from './product/CitationBlock';
import ActionCard from './product/ActionCard';
import TaskCard from './product/TaskCard';
import TicketsDesk from './product/TicketsDesk';
import KnowledgeDesk from './product/KnowledgeDesk';
import PerformanceDesk from './product/PerformanceDesk';
import SelfServiceDialog, { serviceIssueFromChat, type ServiceIssue } from './product/SelfServiceDialog';
import { useView } from './product/useView';
import type { ActionRequest } from './product/types';
import { apiFetch, logoutAccount, isCurrentAccount, type User } from './auth';
import { safeCitationUrl } from './workflow';
import { applyHistory } from './history';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowRight, ArrowUp, BookOpen, Check, ChevronRight, ClipboardCheck, Copy, FileText,
  Headphones, Menu, MessageSquare, Plus, RefreshCw, Search, ShieldCheck, Trash2, UserRound, X, GitBranch } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { loadWorkspace, newConversation, workspaceKey, uid, type Conversation, type Message } from './workspace';
import WorkflowPanel, { WorkflowOverview } from './WorkflowPanel';
import { streamChat, subscribeRun, RunFailure, defaultNodes, type Profile, type Step, type Trace, type WorkflowEvent } from './workflow';

const topics = [
  { icon: RefreshCw, title: '退货规则查询', question: '哪些商品不适用七日无理由退货？请说明资料来源。' },
  { icon: BookOpen, title: '自动续费提醒', question: '根据消费者权益保护法实施条例，自动续费前应如何提醒？' },
  { icon: ClipboardCheck, title: '查询服务工单', question: '请帮我查询已有服务工单的处理进度。' },
];
const intentNames: Record<string, string> = { knowledge_rag: '知识答复', ticket_handler: '工单服务', compliance_checker: '内容审查' };

function PendingReply({ trace }: { trace?: Trace }) {
  const [longWait, setLongWait] = useState(false);
  useEffect(() => { const timer = setTimeout(() => setLongWait(true), 15000); return () => clearTimeout(timer); }, []);
  const running = trace?.steps.find(s => s.status === 'running');
  return <div className="pending-reply" role="status"><span className="thinking-dots" aria-hidden="true"><i /><i /><i /></span><span>{running ? `${defaultNodes.find(n => n.id === running.node)?.label || '工作流'}正在执行…` : longWait ? '仍在处理你的问题，请稍候…' : '正在等待执行事件…'}</span></div>;
}

export default function App({ user }: { user: User }) {
  const { view, navigate } = useView(user.role);
  const [focusedTicket, setFocusedTicket] = useState('');
  const [ticketViewVersion, setTicketViewVersion] = useState(0);
  const [selfService, setSelfService] = useState<ServiceIssue>();
  const [requestedResume, setRequestedResume] = useState<{ action: ActionRequest; key: string }>();
  const [workspace, setWorkspace] = useState(() => loadWorkspace(user.id));
  const [search, setSearch] = useState('');
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [connection, setConnection] = useState<'checking' | 'online' | 'offline' | 'unconfigured'>('checking');
  const [storageWarning, setStorageWarning] = useState(false);
  const [copiedId, setCopiedId] = useState('');
  const [toast, setToast] = useState('');
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [serverOffset, setServerOffset] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [isMobile, setIsMobile] = useState(() => matchMedia('(max-width: 800px)').matches);
  const [workflowDocked, setWorkflowDocked] = useState(() => matchMedia('(min-width: 1180px)').matches);
  const [workflowOpen, setWorkflowOpen] = useState(false);
  const [profile, setProfile] = useState<Profile>();
  const [selectedReply, setSelectedReply] = useState('');
  const workflowDialogRef = useRef<HTMLDialogElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const pending = useRef(new Set<string>());
  const checkedSessions = useRef(new Set<string>());
  const activeIdRef = useRef(workspace.activeId);
  activeIdRef.current = workspace.activeId;
  const active = workspace.conversations.find(c => c.id === workspace.activeId)!;
  const isPending = active.messages.some(m => m.status === 'pending');
  const tracedReply = active.messages.find(m => m.id === selectedReply && m.role === 'assistant') || [...active.messages].reverse().find(m => m.role === 'assistant');
  const tracedQuestion = active.messages.find(m => m.id === tracedReply?.replyTo)?.content;

  const updateConversation = useCallback((id: string, change: (c: Conversation) => Conversation) => {
    setWorkspace(w => ({ ...w, conversations: w.conversations.map(c => c.id === id ? change(c) : c) }));
  }, []);

  useEffect(() => {
    if (!isCurrentAccount(user.id)) return;
    try { localStorage.setItem(workspaceKey(user.id), JSON.stringify(workspace)); setStorageWarning(false); }
    catch { setStorageWarning(true); }
  }, [workspace]);

  const checkConnection = useCallback(async () => {
    try {
      const response = await fetch('/health', { signal: AbortSignal.timeout(6000) });
      if (!response.ok) throw new Error();
      const health = await response.json();
      setConnection(health.chat_ready ? 'online' : 'unconfigured');
    } catch { setConnection('offline'); }
  }, []);
  useEffect(() => { void checkConnection(); const timer = setInterval(checkConnection, 20000); return () => clearInterval(timer); }, [checkConnection]);
  useEffect(() => {
    const controller = new AbortController();
    void apiFetch('/api/workflow', { signal: controller.signal }).then(r => r.ok ? r.json() : null)
      .then(p => { if (p && Array.isArray(p.nodes)) setProfile(p); }).catch(() => {});
    return () => controller.abort();
  }, [connection]);
  useEffect(() => {
    const media = matchMedia('(min-width: 1180px)');
    const update = () => setWorkflowDocked(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (workflowOpen && !workflowDocked) workflowDialogRef.current?.showModal();
    else workflowDialogRef.current?.close();
  }, [workflowOpen, workflowDocked]);
  useEffect(() => { setSelectedReply(''); }, [workspace.activeId]);

  useEffect(() => { if (view !== 'chat') setWorkflowOpen(false); }, [view]);

  function showWorkflow(replyId?: string) {
    if (replyId) setSelectedReply(replyId);
    if (!workflowDocked) setWorkflowOpen(true);
  }

  useEffect(() => {
    if (!inputRef.current) return;
    inputRef.current.style.height = 'auto';
    inputRef.current.style.height = `${Math.min(inputRef.current.scrollHeight, 160)}px`;
  }, [active.draft, workspace.activeId]);
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [workspace.activeId, active.messages]);
  useEffect(() => { if (toast) { const timer = setTimeout(() => setToast(''), 3500); return () => clearTimeout(timer); } }, [toast]);
  useEffect(() => { if (copiedId) { const timer = setTimeout(() => setCopiedId(''), 2000); return () => clearTimeout(timer); } }, [copiedId]);
  useEffect(() => {
    if (deleteId) dialogRef.current?.showModal(); else dialogRef.current?.close();
  }, [deleteId]);
  useEffect(() => {
    const media = matchMedia('(max-width: 800px)');
    const update = () => setIsMobile(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  useEffect(() => {
    if (!sidebarOpen || !isMobile) return;
    sidebarRef.current?.querySelector<HTMLButtonElement>('.mobile-close')?.focus();
    const trap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab' || !sidebarRef.current) return;
      const items = [...sidebarRef.current.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input')].filter(el => el.offsetParent !== null);
      if (e.shiftKey && document.activeElement === items[0]) { e.preventDefault(); items.at(-1)?.focus(); }
      else if (!e.shiftKey && document.activeElement === items.at(-1)) { e.preventDefault(); items[0]?.focus(); }
    };
    window.addEventListener('keydown', trap);
    return () => window.removeEventListener('keydown', trap);
  }, [sidebarOpen, isMobile]);
  useEffect(() => {
    if (!sidebarOpen) return;
    const close = (e: KeyboardEvent) => { if (e.key === 'Escape') setSidebarOpen(false); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [sidebarOpen]);

  const syncSessions = useCallback(async (offset = 0) => {
    try {
      const response = await apiFetch(`/api/sessions?limit=50&offset=${offset}`);
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (!Array.isArray(data.sessions)) return;
      setServerOffset(data.next_offset ?? null);
      setWorkspace(w => {
        const rows: Conversation[] = data.sessions.filter((r: { status: string }) => r.status === 'active').map((r: { id: string; title: string; updated_at: string }) => {
          const existing = w.conversations.find(c => c.sessionId === r.id);
          return existing ? { ...existing, title: r.title } : { ...newConversation(), sessionId: r.id, title: r.title, updatedAt: Date.parse(r.updated_at) || 0 };
        });
        const selected = w.conversations.find(c => c.id === w.activeId)!;
        return { ...w, activeId: offset === 0 && !selected.messages.length && !selected.draft && !selected.sessionId && rows.length ? rows[0].id : w.activeId,
          conversations: [...rows, ...w.conversations.filter(c => !rows.some(r => r.id === c.id))] };
      });
    } catch { setToast('服务器历史暂时无法同步，可稍后刷新历史。'); }
  }, []);
  useEffect(() => { void syncSessions(); }, [syncSessions]);

  async function refreshHistory(older = false) {
    if (!active.sessionId || isPending) return;
    setHistoryLoading(true);
    try {
      const response = await apiFetch(`/api/history/${encodeURIComponent(active.sessionId)}?limit=100${older && active.beforeSequence ? `&before=${active.beforeSequence}` : ''}`);
      if (!response.ok) throw new Error();
      const data = await response.json();
      updateConversation(active.id, c => applyHistory(c, data, older));
    } catch { setToast('历史读取失败，请检查服务连接后刷新历史。'); }
    finally { setHistoryLoading(false); }
  }

  useEffect(() => {
    const { id, sessionId } = active;
    if (!sessionId || isPending || checkedSessions.current.has(sessionId)) return;
    const controller = new AbortController();
    void apiFetch(`/api/history/${encodeURIComponent(sessionId)}`, { signal: controller.signal })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !Array.isArray(data.messages)) return;
        checkedSessions.current.add(sessionId);
        updateConversation(id, c => c.sessionId === sessionId ? applyHistory(c, data) : c);
      }).catch(() => {});
    return () => controller.abort();
  }, [workspace.activeId, active.sessionId, isPending, updateConversation]);

  async function openSource(url: string) {
    if (!safeCitationUrl(url)) return;
    const viewer = window.open('about:blank', '_blank');
    if (viewer) viewer.opener = null;
    try {
      const [path, fragment] = url.split('#');
      const response = await apiFetch(path);
      if (!response.ok) throw new Error(response.status === 404 ? '文件不存在或当前账号无权查看。' : '暂时无法打开来源文件。');
      const objectUrl = URL.createObjectURL(await response.blob());
      if (viewer) viewer.location.href = objectUrl + (fragment ? `#${fragment}` : '');
      else { URL.revokeObjectURL(objectUrl); throw new Error('请允许打开新窗口后重试。'); }
      setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
    } catch (error) { viewer?.close(); setToast(error instanceof Error ? error.message : '文件打开失败。'); }
  }

  function createConversation() {
    navigate('chat');
    if (!active.messages.length && !active.draft.trim()) { setSidebarOpen(false); inputRef.current?.focus(); return; }
    const conversation = newConversation();
    setWorkspace(w => ({ ...w, activeId: conversation.id, conversations: [conversation, ...w.conversations] }));
    setSidebarOpen(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  const receiveRunEvent = useCallback((conversationId: string, replyId: string, event: WorkflowEvent) => {
    updateConversation(conversationId, c => ({ ...c,
      ...(event.type === 'session' || event.type === 'queued' ? { sessionId: event.session_id || c.sessionId, activeRunId: event.run_id,
        persistence: event.persistence || c.persistence, canResume: false, canContinue: false, expired: false } : {}),
      messages: c.messages.map(m => {
        if (m.id !== replyId) return m;
        const result = { ...m, runId: event.run_id || m.runId, lastEventId: event.event_id ?? m.lastEventId };
        if ((event.type !== 'node_start' && event.type !== 'node_end') || !event.node) return result;
        const steps = m.trace?.steps || [];
        const step: Step = { node: event.node, model: event.model, elapsed_ms: event.elapsed_ms,
          status: event.type === 'node_start' ? 'running' : event.status || 'done', duration_ms: event.duration_ms, summary: event.summary };
        const runningIndex = steps.map((value, index) => value.node === event.node && value.status === 'running' ? index : -1).filter(index => index >= 0).at(-1);
        const next = event.type === 'node_end' && runningIndex !== undefined ? steps.map((value, index) => index === runningIndex ? step : value) : [...steps, step];
        return { ...result, trace: { status: 'running' as const, steps: next } };
      }),
    }));
  }, [updateConversation]);

  const finishRun = useCallback((conversationId: string, replyId: string, data: WorkflowEvent) => {
    if (typeof data.response !== 'string' || !data.response.trim() || !data.session_id) throw new Error('服务未返回有效回复，请刷新历史查询运行。');
    checkedSessions.current.delete(data.session_id);
    updateConversation(conversationId, c => ({ ...c, sessionId: data.session_id, activeRunId: undefined, actionRequest: data.action_request?.action_id ? data.action_request : undefined, persistence: data.persistence || c.persistence,
      canResume: false, canContinue: true, expired: false, updatedAt: Date.now(),
      messages: c.messages.map(m => m.id === replyId ? { ...m, status: 'done', content: data.response!, error: undefined,
        runId: data.run_id || m.runId, actionRequest: data.action_request, execution: data.action_execution, taskPlan: data.task_plan, intent: data.intent, reviewed: data.compliance_passed === true, citations: data.compliance_passed ? data.citations : [],
        trace: data.execution_trace?.length ? { status: 'done', steps: data.execution_trace, duration_ms: data.duration_ms } : undefined } : m),
    }));
  }, [updateConversation]);

  const failRun = useCallback((conversationId: string, replyId: string, error: unknown) => {
    const reason = error instanceof Error ? error.message : '连接暂时不可用，请刷新历史查询已有运行。';
    updateConversation(conversationId, c => ({ ...c,
      activeRunId: error instanceof RunFailure && !['SUBSCRIPTION_DISCONNECTED', 'RUN_UNAVAILABLE'].includes(error.code) ? undefined : c.activeRunId,
      messages: c.messages.map(m => m.id === replyId ? { ...m, status: 'error', error: reason,
        trace: m.trace ? { ...m.trace, status: 'error', steps: m.trace.steps.map(x => x.status === 'running' ? { ...x, status: 'error' } : x) } : undefined } : m),
    }));
  }, [updateConversation]);

  // A reload can happen after SQL accepted the POST but before its response arrived.
  // Look up the saved request key with GET; never re-send it automatically on mount.
  const missingRun = active.messages.find(m => m.role === 'assistant' && m.clientRequestId && !m.runId && m.status === 'error');
  useEffect(() => {
    if (!missingRun?.clientRequestId) return;
    const controller = new AbortController();
    void apiFetch(`/api/runs/by-request/${encodeURIComponent(missingRun.clientRequestId)}`, { signal: controller.signal, cache: 'no-store' })
      .then(r => { if (r.status === 404) return { notFound: true };if (!r.ok) throw new Error('查询失败');return r.json(); }).then(row => {
        if (!row || controller.signal.aborted) return;
        if (row.notFound) {
          updateConversation(active.id, c => ({ ...c, messages: c.messages.map(m => m.id === missingRun.id ? { ...m, error: '未找到已提交的运行，可点击重试发送原请求。' } : m) }));return;
        }
        updateConversation(active.id, c => ({ ...c, sessionId: row.session_id, activeRunId: row.run_id, expired: false,
          messages: c.messages.map(m => m.id === missingRun.id ? { ...m, runId: row.run_id, status: 'pending', error: undefined, trace: { status: 'running', steps: [] } } : m) }));
      }).catch(() => { if (!controller.signal.aborted) updateConversation(active.id, c => ({ ...c, messages: c.messages.map(m => m.id === missingRun.id ? { ...m, error: '暂时无法核对后台状态，请检查连接或刷新历史。' } : m) })); });
    return () => controller.abort();
  }, [active.id, missingRun?.id, missingRun?.clientRequestId, updateConversation]);

  const restoredReply = active.messages.find(m => m.runId === active.activeRunId && m.status === 'pending');
  useEffect(() => {
    const { id, activeRunId, sessionId } = active;
    if (!activeRunId || !restoredReply || pending.current.has(id)) return;
    const controller = new AbortController();const replyId = restoredReply.id;
    pending.current.add(id);
    void subscribeRun(activeRunId, event => { if (!controller.signal.aborted) receiveRunEvent(id, replyId, event); }, controller.signal)
      .then(data => { if (!controller.signal.aborted) finishRun(id, replyId, data); })
      .catch(error => { if (!controller.signal.aborted) failRun(id, replyId, error); })
      .finally(() => {
        if (!controller.signal.aborted) pending.current.delete(id);
        if (sessionId) checkedSessions.current.delete(sessionId);
      });
    return () => { controller.abort();pending.current.delete(id); };
  }, [active.id, active.activeRunId, restoredReply?.id, receiveRunEvent, finishRun, failRun]);

  function startDraft(text: string) {
    setWorkspace(w => {
      const c = newConversation();c.draft = text;c.title = '工单处理申请';
      return { ...w, activeId: c.id, conversations: [c, ...w.conversations] };
    });navigate('chat');
  }
  function continueAction(action: ActionRequest) {
    checkedSessions.current.delete(action.session_id);
    setWorkspace(w => {
      const existing = w.conversations.find(c => c.sessionId === action.session_id);
      if (existing) return { ...w, activeId: existing.id };
      const c = newConversation();c.sessionId = action.session_id;c.title = String(action.parameters.summary || '工单办理进度');
      return { ...w, activeId: c.id, conversations: [c, ...w.conversations] };
    });setRequestedResume({ action, key: uid() });navigate('chat');
  }
  useEffect(() => {
    if (!requestedResume || active.sessionId !== requestedResume.action.session_id || !active.messages.some(m => m.role === 'user') || isPending) return;
    const request = requestedResume;setRequestedResume(undefined);
    void sendMessage(undefined, true, { clientRequestId: request.key });
  }, [requestedResume, active.id, active.sessionId, active.messages.length, isPending]);

  async function sendMessage(retry?: Message, resume = false, options?: { content?: string; clientRequestId?: string }) {
    const conversation = active;
    if (pending.current.has(conversation.id)) return;
    let attach: string | undefined;
    if (retry?.runId) {
      try {
        const response = await apiFetch(`/api/runs/${encodeURIComponent(retry.runId)}`, { cache: 'no-store' });
        if (!response.ok) throw new Error('暂时无法查询原运行，请刷新历史。');
        const row = await response.json();
        if (['queued', 'running', 'cancel_requested', 'succeeded', 'blocked'].includes(row.run_status || row.status)) attach = retry.runId;
        else resume = true; // This is an explicit user recovery action, not a reconnect.
      } catch (error) { setToast(error instanceof Error ? error.message : '暂时无法查询运行。');return; }
    } else resume = resume || !!(retry && conversation.persistence && conversation.sessionId && !conversation.expired && !retry.clientRequestId);
    if (!resume && !attach && conversation.canResume) { setToast('请先恢复上轮处理，或新建对话。');return; }
    const previous = resume ? [...conversation.messages].reverse().find(m => m.role === 'user') : retry && conversation.messages.find(m => m.id === retry.replyTo);
    const content = (options?.content ?? previous?.content ?? conversation.draft).trim();
    if (!content || connection !== 'online' || pending.current.has(conversation.id)) return;
    pending.current.add(conversation.id);
    const userId = previous?.id ?? uid();const replyId = retry?.id ?? uid();
    const clientRequestId = options?.clientRequestId || (resume ? uid() : retry?.clientRequestId || replyId);
    const requestSessionId = !resume && retry?.requestSessionId !== undefined ? retry.requestSessionId : conversation.expired ? null : conversation.sessionId ?? null;
    setSelectedReply(replyId);
    const initialTrace: Trace = { status: 'running', steps: [] };
    updateConversation(conversation.id, c => ({ ...c, actionRequest: resume ? c.actionRequest : undefined, draft: retry ? c.draft : '', title: c.messages.length ? c.title : content.slice(0, 24), updatedAt: Date.now(),
      messages: retry ? c.messages.map(m => m.id === replyId ? { ...m, content: '', error: undefined, status: 'pending', trace: initialTrace, clientRequestId, requestSessionId } : m)
        : [...c.messages, ...(!resume ? [{ id: userId, role: 'user' as const, content }] : []), { id: replyId, role: 'assistant', content: '', status: 'pending', replyTo: userId, trace: initialTrace, clientRequestId, requestSessionId }],
    }));
    try {
      const receive = (event: WorkflowEvent) => receiveRunEvent(conversation.id, replyId, event);
      const data = attach ? await subscribeRun(attach, receive) : await streamChat(resume ? { client_request_id: clientRequestId } : { message: content, session_id: requestSessionId, client_request_id: clientRequestId }, receive, resume ? conversation.sessionId : undefined);
      finishRun(conversation.id, replyId, data);void checkConnection();
    } catch (error) { failRun(conversation.id, replyId, error); }
    finally {
      pending.current.delete(conversation.id);
      if (conversation.sessionId) checkedSessions.current.delete(conversation.sessionId);
      if (activeIdRef.current === conversation.id) inputRef.current?.focus();
    }
  }

  function submitMessage() {
    if (isPending || connection !== 'online') return;
    const issue = user.role === 'customer' && !active.actionRequest && !active.canResume ? serviceIssueFromChat(active.draft) : undefined;
    if (issue) setSelfService(issue);
    else void sendMessage();
  }

  async function copyMessage(message: Message) {
    try { await navigator.clipboard.writeText(message.content); setCopiedId(message.id); }
    catch { setToast('未能复制，请选择回复文字后手动复制。'); }
  }

  function deleteConversation() {
    if (!deleteId || pending.current.has(deleteId)) return;
    setWorkspace(w => {
      const remaining = w.conversations.filter(c => c.id !== deleteId);
      const conversations = remaining.length ? remaining : [newConversation()];
      return { ...w, conversations, activeId: w.activeId === deleteId ? conversations[0].id : w.activeId };
    });
    setDeleteId(null);
  }

  const connectionText = { checking: '连接中', online: '服务已连接', offline: '服务未连接', unconfigured: '需要配置模型' }[connection];
  const conversations = [...workspace.conversations].sort((a, b) => b.updatedAt - a.updatedAt).filter(c => c.title.toLowerCase().includes(search.toLowerCase()));

  return <div className="app-shell">
    <a className="skip-link" href={view === 'chat' ? '#message-input' : '#product-main'}>跳到消息输入框</a>
    {sidebarOpen && <button className="sidebar-backdrop" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)} />}
    <aside ref={sidebarRef} className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`} aria-label="会话列表">
      <div className="brand"><span className="brand-mark"><Headphones size={22} aria-hidden="true" /></span><div><strong>智能客服</strong><span>对话工作台</span></div><button className="icon-button mobile-close" aria-label="关闭会话列表" onClick={() => setSidebarOpen(false)}><X size={19} /></button></div>
      <nav className="desk-nav" aria-label="工作区导航">
        <button aria-current={view === 'chat' ? 'page' : undefined} onClick={() => { navigate('chat');setSidebarOpen(false); }}><MessageSquare size={16} aria-hidden="true" />客服对话</button>
        <button aria-current={view === 'tickets' ? 'page' : undefined} onClick={() => { navigate('tickets');setSidebarOpen(false); }}><ClipboardCheck size={16} aria-hidden="true" />我的工单</button>
        {user.role === 'customer' && <button onClick={() => { setSidebarOpen(false); setSelfService({}); }}><BookOpen size={16} aria-hidden="true" />售后自助处理</button>}
        {user.role !== 'customer' && <button aria-current={view === 'support' ? 'page' : undefined} onClick={() => { navigate('support');setSidebarOpen(false); }}><Headphones size={16} aria-hidden="true" />客服工作台</button>}
        {user.role === 'admin' && <button aria-current={view === 'knowledge' ? 'page' : undefined} onClick={() => { navigate('knowledge');setSidebarOpen(false); }}><BookOpen size={16} aria-hidden="true" />知识管理</button>}
        {user.role === 'admin' && <button aria-current={view === 'performance' ? 'page' : undefined} onClick={() => { navigate('performance');setSidebarOpen(false); }}><GitBranch size={16} aria-hidden="true" />运行观测</button>}
      </nav>
      <button className="new-conversation" onClick={createConversation}><Plus size={19} aria-hidden="true" />新建对话<span className="new-hint">＋</span></button>
      <label className="search-box"><Search size={16} aria-hidden="true" /><input aria-label="搜索对话" placeholder="搜索对话" value={search} onChange={e => setSearch(e.target.value)} /></label>
      <div className="history-heading">最近对话<span>{workspace.conversations.filter(c => c.messages.length).length}</span></div>
      <nav className="conversation-list" aria-label="最近对话">
        {conversations.map(c => <div key={c.id} className={`conversation-item ${c.id === active.id ? 'is-active' : ''}`}>
          <button className="conversation-select" aria-current={c.id === active.id ? 'page' : undefined} title={c.title} onClick={() => { setWorkspace(w => ({ ...w, activeId: c.id })); navigate('chat');setSidebarOpen(false); }}><MessageSquare size={16} aria-hidden="true" /><span>{c.title}</span>{c.messages.some(m => m.status === 'pending') && <span className="pending-indicator" aria-label="正在回复" />}</button>
          <button className="icon-button delete-conversation" disabled={c.messages.some(m => m.status === 'pending')} aria-label={`删除对话：${c.title}`} title="删除对话" onClick={() => setDeleteId(c.id)}><Trash2 size={14} /></button>
        </div>)}
        {!conversations.length && <p className="no-results">没有找到相关对话</p>}
      </nav>
      <div className="history-controls"><button onClick={() => { checkedSessions.current.clear(); void syncSessions(); void refreshHistory(); }}>刷新历史</button>{serverOffset !== null && <button onClick={() => void syncSessions(serverOffset)}>更多会话</button>}</div>
      <div className="sidebar-footer"><div className="workspace-label"><span className="workspace-avatar">本</span><div><strong>{user.display_name}</strong><span>{{ customer: '用户', support: '客服', admin: '管理员' }[user.role]} · 当前账号的会话</span></div></div><button className="account-logout" onClick={() => { void logoutAccount().catch(error => setToast(error.message)); }}>退出登录</button><a href="/docs" target="_blank" rel="noreferrer"><FileText size={15} aria-hidden="true" />接口文档<ArrowRight size={14} aria-hidden="true" /></a></div>
    </aside>

    {view === 'chat' && <main className="main-panel" inert={sidebarOpen && isMobile ? true : undefined}>
      <header className="topbar"><div className="page-location"><button className="icon-button mobile-menu" aria-label="打开会话列表" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(true)}><Menu size={20} /></button><span className="location-parent">工作区</span><ChevronRight size={14} className="location-parent" aria-hidden="true" /><strong>客服对话</strong><span className="demo-label">四 Agent 协作</span></div><div className="topbar-actions">{!workflowDocked && <button className="workflow-toggle" onClick={() => showWorkflow()} aria-haspopup="dialog"><GitBranch size={15} />协作过程{isPending && <span className="pending-indicator" />}</button>}<button className={`connection-status connection-${connection}`} onClick={() => void checkConnection()} title="点击重新检查服务连接"><span aria-hidden="true" />{connectionText}</button></div></header>
      {storageWarning && <div className="notice" role="alert">浏览器草稿无法保存；已提交的消息可从服务器历史恢复。</div>}
      {connection === 'offline' && <div className="notice notice-error" role="status">本地服务未连接。请启动智能客服服务后<button onClick={() => void checkConnection()}>重新检查</button>。</div>}
      {connection === 'unconfigured' && <div className="notice" role="status">聊天服务需要先完成模型配置，配置就绪后即可发送消息。</div>}
      <div className="workbench"><div className="chat-column"><div className="chat-scroll" ref={logRef}>
        {!active.messages.length ? <section className="welcome">
          <div className="welcome-mark"><Headphones size={31} strokeWidth={1.7} aria-hidden="true" /><span /></div>
          <p className="welcome-label">四个 Agent，按需协作</p>
          <h1>你好，有什么可以帮你？</h1>
          <p className="welcome-description">咨询业务政策、核验订单条件，或查询服务工单。<br className="desktop-break" />发送后可查看意图判断、处理分支与合规审查。</p>
          {!workflowDocked && <WorkflowOverview onOpen={() => showWorkflow()} />}
          <div className="suggestions">{topics.map(topic => <button key={topic.title} onClick={() => { updateConversation(active.id, c => ({ ...c, draft: topic.question })); inputRef.current?.focus(); }}><topic.icon size={21} aria-hidden="true" /><strong>{topic.title}</strong><span>{topic.question}</span><ArrowRight size={17} className="suggestion-arrow" aria-hidden="true" /></button>)}</div>
          <p className="welcome-note"><ShieldCheck size={14} aria-hidden="true" />回复会经过自动内容检查</p>
        </section> : <div className="message-list" role="log" aria-label="聊天记录" aria-live="polite" aria-relevant="additions text">
          <div className="conversation-start">本次对话开始</div>
          {active.hasMore && <button className="history-more" disabled={historyLoading} onClick={() => void refreshHistory(true)}>加载更早消息</button>}
          {active.expired && <div className="session-notice">{active.localOnlyHistory ? '服务器没有此旧版消息记录，当前展示浏览器保留的旧记录。继续发送将新建会话。' : active.persistence ? '此会话的流程或授权上下文已变化。历史已保留，继续发送将新建会话。' : '历史消息已保留在此浏览器。服务记录已重置，继续发送将建立新的服务会话。'}</div>}
          {active.messages.map(message => <article key={message.id} className={`message message-${message.role}`} aria-label={message.role === 'user' ? '你的消息' : '客服回复'}>
            <div className="message-avatar" aria-hidden="true">{message.role === 'assistant' ? <Headphones size={19} /> : <UserRound size={18} />}</div>
            <div className="message-content"><div className="message-byline"><strong>{message.role === 'user' ? '你' : '智能客服'}</strong>{message.intent && <span>{intentNames[message.intent] || '客服答复'}</span>}</div>
              {message.status === 'pending' ? <PendingReply trace={message.trace} /> : message.status === 'error' ? <div className="message-error" role="alert"><p>{message.error}</p><button onClick={() => void sendMessage(message)} disabled={isPending || connection !== 'online'}><RefreshCw size={14} aria-hidden="true" />重试</button></div>
                : <div className={`message-text ${message.role === 'assistant' ? 'markdown-body' : ''}`}>{message.role === 'assistant' ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener">{children}</a>, img: ({ alt }) => <span>[{alt || '图片'}]</span> }}>{message.content}</ReactMarkdown> : message.content}</div>}
              {message.role === 'assistant' && message.reviewed && message.status === 'done' && Array.isArray(message.citations) && message.citations.some(c => safeCitationUrl(c.url)) && <div className="source-citations" aria-label="知识来源"><span>参考原文件</span>{message.citations.filter(c => safeCitationUrl(c.url)).map(c => <button key={c.number} type="button" onClick={() => { void openSource(c.url); }} title={`${c.heading || ''} · ${c.version || ''}`}>[{c.number}] {c.source}{c.page ? ` · 第 ${c.page} 页` : c.heading ? ` · ${c.heading}` : ' · HTML'}</button>)}</div>}
              {message.role === 'assistant' && message.reviewed && message.status === 'done' && message.citations?.map(c => <CitationBlock key={c.number} citation={c} />)}
              {message.role === 'assistant' && message.status !== 'pending' && message.status !== 'error' && <div className="message-actions"><button className="copy-button" onClick={() => void copyMessage(message)} aria-label={copiedId === message.id ? '已复制回复' : '复制回复'}>{copiedId === message.id ? <Check size={14} /> : <Copy size={14} />}<span>{copiedId === message.id ? '已复制' : '复制'}</span></button><span className={`review-status ${message.reviewed ? '' : 'review-held'}`}><ShieldCheck size={13} aria-hidden="true" />{message.reviewed ? '已通过自动检查' : '需要进一步核实'}</span></div>}
              {message.execution?.ticket_id && message.status === 'done' && <div className="execution-receipt" role="status"><Check size={16} aria-hidden="true" /><strong>办理已提交</strong><button onClick={() => { setFocusedTicket(message.execution!.ticket_id!);navigate('tickets'); }}>{message.execution.ticket_id} · 查看工单</button>{message.execution.requested_amount && <small>已登记申请金额 {message.execution.requested_amount} 元；此结果不代表资金已退回。</small>}</div>}
              {message.role === 'assistant' && message.status === 'done' && <TaskCard plan={message.taskPlan} canFill={!isPending && message.id === [...active.messages].reverse().find(m => m.role === 'assistant')?.id} onFill={value => void sendMessage(undefined, false, { content: value })} />}
              {message.role === 'assistant' && <button className={`trace-link ${tracedReply?.id === message.id ? 'trace-selected' : ''}`} onClick={() => showWorkflow(message.id)}><GitBranch size={14} />{message.status === 'pending' ? '查看实时协作' : message.trace ? '查看本轮协作' : '查看协作流程'}{message.trace && message.status !== 'pending' && <span>{message.trace.steps.length} 个执行节点</span>}</button>}
            </div>
          </article>)}
        </div>}
      </div>
      {active.actionRequest?.action_id && !isPending && <div className="chat-action"><ActionCard key={active.actionRequest.action_id} actionId={active.actionRequest.action_id} onContinue={continueAction} /></div>}
      {active.canResume && !active.actionRequest?.action_id && !isPending && <div className="recovery-notice" role="status"><span>上轮处理尚未完成，可从已保存的进度继续。</span><button onClick={() => void sendMessage(undefined, true)} disabled={connection !== 'online'}><RefreshCw size={14} />恢复上轮处理</button><button onClick={createConversation}>新建对话</button></div>}
      <div className="composer-area"><div className={`composer ${isPending ? 'composer-busy' : ''}`}><form onSubmit={e => { e.preventDefault(); submitMessage(); }}>
        <label className="sr-only" htmlFor="message-input">输入消息</label>
        <textarea id="message-input" ref={inputRef} rows={1} maxLength={8000} placeholder={isPending ? '可以先写下你的下一个问题…' : '输入你的问题，例如：我想了解退款政策…'} value={active.draft} onChange={e => updateConversation(active.id, c => ({ ...c, draft: e.target.value }))} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); submitMessage(); } }} aria-describedby="composer-hint" />
        <div className="composer-toolbar"><span className="composer-caption"><MessageSquare size={14} aria-hidden="true" />{isPending ? '正在等待客服回复' : '描述得越具体，越容易解决'}</span><div className="composer-controls">{active.activeRunId && <button className="desk-text" type="button" onClick={() => { void apiFetch(`/api/runs/${encodeURIComponent(active.activeRunId!)}/cancel`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(r => { if (!r.ok) throw new Error();setToast('已请求停止后续处理，已提交结果保留。'); }).catch(() => setToast('停止请求未确认，请刷新历史核实。')); }}>停止处理</button>}{active.draft.length > 7000 && <span className="character-count">{active.draft.length}/8000</span>}<button className="send-button" type="submit" disabled={!active.draft.trim() || isPending || connection !== 'online'} aria-label="发送消息" title="发送消息"><ArrowUp size={20} aria-hidden="true" /></button></div></div>
      </form></div><div className="composer-footer" id="composer-hint"><span>服务器保存已提交消息 · 内容由智能客服生成</span><span>Enter 发送<span className="keyboard-divider">/</span>Shift + Enter 换行</span></div></div>
      </div>{workflowDocked && <aside className="workflow-docked"><WorkflowPanel profile={profile} trace={tracedReply?.trace} taskPlan={tracedReply?.taskPlan} action={tracedReply?.actionRequest} execution={tracedReply?.execution} historical={!!tracedReply && !tracedReply.trace} title={tracedQuestion} /></aside>}</div>
    </main>}
    {view !== 'chat' && <button className="product-mobile-menu" aria-label="打开会话列表" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(true)}><Menu size={20} /></button>}
    {view === 'tickets' && <TicketsDesk mobile={isMobile} key={'tickets:' + focusedTicket + ':' + ticketViewVersion} initialTicket={focusedTicket} blocked={sidebarOpen && isMobile}
      onDraft={startDraft} onContinue={continueAction}
      onSelfService={user.role === 'customer' ? ticket => {
        const issue = (ticket.summary + '。' + ticket.details).slice(0, 500);
        const options = ticket.type === 'general' ? serviceIssueFromChat('找' + issue) : undefined;
        setSelfService({ ...options, kind: options?.kind || (ticket.type === 'complaint' && /破损|损坏/.test(issue) ? 'damage' : 'other'), issue, ticket, autoLookup: false });
      } : undefined} />}
    {selfService && <SelfServiceDialog userId={user.id} initial={selfService} onClose={() => setSelfService(undefined)}
      onTicket={ticket => {
        if (selfService.issue === active.draft.trim() && !selfService.ticket) updateConversation(active.id, c => ({ ...c, draft: '' }));
        setFocusedTicket(ticket); setTicketViewVersion(v => v + 1); navigate('tickets'); setSelfService(undefined);
      }} />}
    {view === 'support' && user.role !== 'customer' && <TicketsDesk mobile={isMobile} key="support" support blocked={sidebarOpen && isMobile} onDraft={startDraft} onContinue={continueAction} />}
    {view === 'knowledge' && user.role === 'admin' && <KnowledgeDesk user={user} blocked={sidebarOpen && isMobile} />}
    {view === 'performance' && user.role === 'admin' && <PerformanceDesk blocked={sidebarOpen && isMobile} />}
    {view === 'chat' && !workflowDocked && <dialog ref={workflowDialogRef} className="workflow-dialog" onCancel={e => { e.preventDefault(); setWorkflowOpen(false); }} aria-label="协作过程"><WorkflowPanel profile={profile} trace={tracedReply?.trace} taskPlan={tracedReply?.taskPlan} action={tracedReply?.actionRequest} execution={tracedReply?.execution} historical={!!tracedReply && !tracedReply.trace} title={tracedQuestion} onClose={() => setWorkflowOpen(false)} /></dialog>}
    {toast && <div className="toast" role="status">{toast}</div>}
    <dialog ref={dialogRef} className="delete-dialog" onCancel={e => { e.preventDefault(); setDeleteId(null); }} aria-labelledby="delete-title"><div className="dialog-icon"><Trash2 size={22} aria-hidden="true" /></div><h2 id="delete-title">删除这段对话？</h2><p>仅移除此浏览器的记录。服务器历史仍保留，可刷新历史取回。</p><div className="dialog-actions"><button autoFocus onClick={() => setDeleteId(null)}>保留对话</button><button className="danger-button" onClick={deleteConversation}>删除对话</button></div></dialog>
  </div>;
}
