import type { Trace, Citation } from './workflow';
import type { ActionRequest, Execution, TaskPlan } from './product/types';

export type Message = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status?: 'pending' | 'done' | 'error';
  error?: string;
  replyTo?: string;
  intent?: string;
  reviewed?: boolean;
  trace?: Trace;
  citations?: Citation[];
  runId?: string;
  lastEventId?: number;
  clientRequestId?: string;
  requestSessionId?: string | null;
  actionRequest?: ActionRequest;
  execution?: Execution;
  taskPlan?: TaskPlan;
};

export type Conversation = {
  id: string;
  title: string;
  sessionId?: string;
  createdAt: number;
  updatedAt: number;
  messages: Message[];
  draft: string;
  expired?: boolean;
  persistence?: string;
  canResume?: boolean;
  canContinue?: boolean;
  recoveryReason?: string;
  hasMore?: boolean;
  beforeSequence?: number;
  localOnlyHistory?: boolean;
  activeRunId?: string;
  actionRequest?: ActionRequest;
};

export type Workspace = { userId: string; activeId: string; conversations: Conversation[] };
export const workspaceKey = (userId: string) => `smart-cs.workspace.v2:${userId}`;
export const uid = () => crypto.randomUUID();

export function newConversation(): Conversation {
  return { id: uid(), title: '新对话', createdAt: Date.now(), updatedAt: Date.now(), messages: [], draft: '' };
}

export function loadWorkspace(userId: string): Workspace {
  try {
    const stored = JSON.parse(localStorage.getItem(workspaceKey(userId)) || 'null');
    if (stored && stored.userId === userId && Array.isArray(stored.conversations)) {
      const conversations: Conversation[] = stored.conversations
        .filter((c: Conversation) => c && typeof c.id === 'string' && typeof c.title === 'string' && Array.isArray(c.messages))
        .map((c: Conversation) => ({
          ...c,
          draft: typeof c.draft === 'string' ? c.draft : '',
          messages: c.messages
            .filter(m => m && typeof m.id === 'string' && typeof m.content === 'string' && ['user', 'assistant'].includes(m.role))
            .map(m => m.status === 'pending' ? { ...m, status: 'error', error: m.runId || m.clientRequestId ? '上次连接已断开，正在核对后台运行状态。' : '上次请求已中断，你可以重试。',
              trace: m.trace ? { ...m.trace, status: 'error', steps: m.trace.steps.map(s => s.status === 'running' ? { ...s, status: 'error' } : s) } : undefined } : m),
        }));
      if (conversations.length) {
        return { userId, conversations, activeId: conversations.some(c => c.id === stored.activeId) ? stored.activeId : conversations[0].id };
      }
    }
  } catch { /* Invalid or unavailable browser storage should not prevent chatting. */ }
  const conversation = newConversation();
  return { userId, activeId: conversation.id, conversations: [conversation] };
}
