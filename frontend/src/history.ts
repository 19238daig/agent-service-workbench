import type { Conversation, Message } from './workspace';
import type { WorkflowEvent } from './workflow';
import type { ActionRequest } from './product/types';

export type ServerHistory = {
  persistence?: string; can_resume?: boolean; can_continue?: boolean; recovery_reason?: string;
  active_run_id?: string; run_status?: string;
  action_request?: ActionRequest;
  has_more?: boolean; before_sequence?: number;
  messages: { id: string; role: 'user' | 'assistant'; content: string; metadata?: WorkflowEvent }[];
};

export function applyHistory(c: Conversation, data: ServerHistory, prepend = false): Conversation {
  if (!data.persistence) return data.messages.length ? c : { ...c, sessionId: undefined, expired: true };
  const localOnlyHistory = !data.active_run_id && !data.can_continue && !data.can_resume && !data.messages.length && !!c.messages.length;
  let previous = '';
  const messages: Message[] = data.messages.map(m => {
    if (m.role === 'user') previous = m.id;
    const meta: Partial<WorkflowEvent> = m.metadata || {};
    const stableId = m.role === 'assistant' && meta.run_id ? c.messages.find(existing => existing.role === 'assistant' && existing.runId === meta.run_id)?.id || m.id : m.id;
    return { id: stableId, role: m.role, content: m.content, status: 'done', replyTo: m.role === 'assistant' ? previous : undefined, runId: meta.run_id,
      intent: meta.intent, reviewed: meta.compliance_passed, citations: meta.compliance_passed ? meta.citations : [],
      actionRequest: meta.action_request, execution: meta.action_execution, taskPlan: meta.task_plan,
      trace: meta.execution_trace?.length ? { status: 'done', steps: meta.execution_trace } : undefined };
  });
  if (data.active_run_id && !prepend) {
    const pending = c.messages.find(m => m.runId === data.active_run_id);
    messages.push({ id: pending?.id || `run-${data.active_run_id}`, role: 'assistant', content: '', status: 'pending',
      replyTo: previous, runId: data.active_run_id, lastEventId: 0, trace: { status: 'running', steps: [] } });
  }
  return { ...c, persistence: data.persistence, canResume: data.can_resume, canContinue: data.can_continue,
    recoveryReason: data.recovery_reason, hasMore: data.has_more, beforeSequence: data.before_sequence,
    activeRunId: data.active_run_id || undefined,
    actionRequest: data.action_request?.action_id ? data.action_request : undefined,
    expired: localOnlyHistory || !data.active_run_id && !data.can_continue && !data.can_resume && !!data.recovery_reason && !data.recovery_reason.startsWith('APPROVAL_'),
    localOnlyHistory,
    messages: localOnlyHistory ? c.messages : prepend ? [...messages, ...c.messages.filter(m => !messages.some(x => x.id === m.id))] : messages };
}
