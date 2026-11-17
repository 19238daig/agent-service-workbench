import { apiFetch } from './auth';
import type { ActionRequest, Execution, TaskPlan } from './product/types';
export type Citation = { number: number; document_id: string; chunk_id?: string; source: string; page?: number; heading?: string; version?: string; url: string; source_url?: string };
export const safeCitationUrl = (url: unknown) => typeof url === 'string' && /^\/api\/knowledge\/documents\/[a-zA-Z0-9_-]+\/file(?:#page=\d+)?$/.test(url);
export type Step = {
  node: string;
  status: 'running' | 'done' | 'blocked' | 'error';
  model?: string;
  duration_ms?: number;
  elapsed_ms?: number;
  summary?: Record<string, unknown>;
};
export type Trace = { steps: Step[]; status: 'running' | 'done' | 'error'; duration_ms?: number };
export type WorkflowNode = { id: string; label: string; description: string; role?: string; model: string };
export type Profile = { ready: boolean; nodes: WorkflowNode[]; edges: { source: string; target: string; conditional: boolean }[] };
export type WorkflowEvent = Partial<Step> & {
  type: 'session' | 'node_start' | 'node_end' | 'complete' | 'error' | 'queued' | 'running' | 'model_call_started' | 'model_call_finished' | 'cancellation_requested' | 'subscription_error' | 'stream_end';
  run_id?: string; event_id?: number; code?: string;
  session_id?: string;
  message?: string;
  response?: string;
  intent?: string;
  compliance_passed?: boolean;
  execution_trace?: Step[];
  citations?: Citation[];
  persistence?: string;
  action_request?: ActionRequest;
  action_execution?: Execution;
  task_plan?: TaskPlan;
};

export const defaultNodes: WorkflowNode[] = [
  { id: 'intent_router', label: '意图路由 Agent', role: 'intent_router', model: '', description: '识别需求与实体，提出路由建议' },
  { id: 'supervisor_route', label: 'Supervisor 分派', model: '', description: '按意图与置信度选择分支' },
  { id: 'knowledge_rag', label: '知识检索 Agent', role: 'knowledge_rag', model: '', description: '检索、重排并生成有依据的答复' },
  { id: 'ticket_handler', label: '工单处理 Agent', role: 'ticket_handler', model: '', description: '核对订单、提出工单草稿；确认与审批后由服务器办理' },
  { id: 'compliance_check', label: '合规审查 Agent', role: 'compliance_checker', model: '', description: '规则检查与模型审查' },
  { id: 'synthesize', label: 'Supervisor 汇总', model: '', description: '返回审查后的最终答复' },
];

export const routeNames: Record<string, string> = { knowledge_rag: '知识检索', ticket_handler: '工单处理', compliance_checker: '合规咨询', clarification: '先澄清需求' };
const categories: Record<string, string> = { consultation: '业务咨询', complaint: '投诉', transaction: '业务办理', account: '账户服务', compliance: '安全与合规', unknown: '暂未明确' };
export const seconds = (ms?: number) => ms === undefined ? '' : `${(ms / 1000).toFixed(ms < 1000 ? 2 : 1)} 秒`;

export function facts(step?: Step): string[] {
  const s = step?.summary;
  if (!s) return [];
  switch (step?.node) {
    case 'intent_router': return [categories[String(s.primary)] || String(s.primary || '暂未明确'),
      ...(typeof s.confidence === 'number' ? [`模型置信度 ${Math.round(s.confidence * 100)}%`] : [])];
    case 'supervisor_route': return [routeNames[String(s.route)] || '路由未确定', ...(s.clarification ? ['意图不明确，先补充信息'] : [])];
    case 'knowledge_rag': return [`检索 ${s.retrieved_count ?? 0} 条，采用 ${s.used_count ?? 0} 条`,
      ...(s.embedding_model ? [String(s.embedding_model)] : []),
      ...(s.reranker_model ? [String(s.reranker_model)] : []),
      ...(Array.isArray(s.sources) ? s.sources.map(String) : [])];
    case 'ticket_handler': return [({ create: '创建工单', query: '查询工单', clarify: '补充操作信息', order_query: '只读查询订单', refund_eligibility: '核验退款申请条件' }[String(s.action)] || '核实工单操作'),
      ...(s.write_blocked === true ? ['待人工登记，尚未创建'] : s.action === 'create' && s.found === false ? ['未创建，请核实申请信息'] : []),
      ...(s.order_id ? [String(s.order_id)] : []),
      ...(s.decision ? [({ allowed: '可登记申请', denied: '条件不满足', requires_review: '需要人工核实', needs_information: '需要补充事实' }[String(s.decision)] || '待核实')] : []),
      ...(s.maximum_refund_amount ? [`当前申请上限：${s.maximum_refund_amount} 元`] : []),
      ...(s.ticket_id ? [String(s.ticket_id), s.found ? `状态：${({ created: '已创建', processing: '处理中', resolved: '已解决' }[String(s.status)] || s.status || '已找到')}` : '未找到此工单'] : [])];
    case 'compliance_check': return [s.passed ? '审查通过' : '审查未通过', `风险：${({ low: '低', medium: '中', high: '高', critical: '严重' }[String(s.risk_level)] || '未知')}`,
      ...(Number(s.violation_count) > 0 ? [`发现 ${s.violation_count} 项问题，原始内容未展示`] : [])];
    case 'synthesize': return [s.released ? '已返回审查后的答复' : '已返回安全提示'];
    default: return [];
  }
}

/** A run is created once; every later connection is a GET with a cursor. */
export class RunFailure extends Error {
  constructor(message: string, public code = 'RUN_FAILED', public runId?: string) { super(message); }
}
export type RunDescriptor = { run_id: string; session_id: string; status: string; result_event?: WorkflowEvent };

async function checkReply(response: Response, runId?: string) {
  if (response.ok) return;
  const error = await response.json().catch(() => null);
  throw new RunFailure(error?.error?.message || '运行接口暂时不可用，请检查服务连接。', error?.error?.code || 'RUN_UNAVAILABLE', runId);
}

export async function subscribeRun(runId: string, onEvent: (event: WorkflowEvent) => void, signal?: AbortSignal, after = 0): Promise<WorkflowEvent> {
  let cursor = after;
  for (let attempt = 0; attempt < 8; attempt++) {
    signal?.throwIfAborted();
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    try {
      const response = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/events`, {
        headers: { 'Last-Event-ID': String(cursor) }, cache: 'no-store',
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(70000)]) : AbortSignal.timeout(70000),
      });
      await checkReply(response, runId);
      if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new RunFailure('运行事件格式无效，请重启服务。', 'INVALID_STREAM', runId);
      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let complete: WorkflowEvent | undefined;
      const consume = (frame: string) => {
        const lines = frame.split('\n');
        const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!data) return;
        const rawId = lines.find(line => line.startsWith('id:'))?.slice(3).trim();
        const id = rawId === undefined ? undefined : Number(rawId);
        if (id !== undefined && (!Number.isSafeInteger(id) || id < 1)) throw new RunFailure('运行事件序号无效。', 'INVALID_STREAM', runId);
        if (id !== undefined && id <= cursor) return;
        const event: WorkflowEvent = { ...JSON.parse(data), run_id: runId, event_id: id };
        onEvent(event);
        if (id !== undefined) cursor = id;
        if (event.type === 'error' || event.type === 'subscription_error') throw new RunFailure(event.message || '运行中断，请核实已有结果后决定是否恢复。', event.code, runId);
        if (event.type === 'complete') complete = event;
      };
      while (!complete) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let match: RegExpExecArray | null;
        while ((match = /\r?\n\r?\n/.exec(buffer))) {
          consume(buffer.slice(0, match.index).replace(/\r\n/g, '\n'));
          buffer = buffer.slice(match.index + match[0].length);
        }
        if (done) break;
      }
      if (complete) return complete;
      const result = await apiFetch(`/api/runs/${encodeURIComponent(runId)}`, { cache: 'no-store', signal });
      await checkReply(result, runId);
      const row = await result.json();
      if (row.result_event) {
        onEvent(row.result_event);
        if (row.result_event.type === 'error') throw new RunFailure(row.result_event.message, row.result_event.code, runId);
        return row.result_event;
      }
    } catch (error) {
      signal?.throwIfAborted();
      if (error instanceof RunFailure || error instanceof DOMException && error.name === 'AbortError') throw error;
      // A network failure retries only GET. A new POST is never sent here.
      if (attempt === 7) throw new RunFailure('订阅连接暂时不可用，后台仍会处理；刷新历史可重新连接。', 'SUBSCRIPTION_DISCONNECTED', runId);
    } finally {
      if (reader) { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    }
    await new Promise<void>((resolve, reject) => {
      const done = () => { signal?.removeEventListener('abort', cancel); resolve(); };
      const timer = setTimeout(done, Math.min(500 * 2 ** attempt, 5000));
      const cancel = () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); reject(new DOMException('Subscription closed', 'AbortError')); };
      signal?.addEventListener('abort', cancel, { once: true });
    });
  }
  throw new RunFailure('请刷新历史重新连接已有运行。', 'SUBSCRIPTION_DISCONNECTED', runId);
}

export async function streamChat(body: object, onEvent: (event: WorkflowEvent) => void, resumeSession?: string): Promise<WorkflowEvent> {
  const payload = { client_request_id: crypto.randomUUID(), ...body };
  const path = resumeSession ? `/api/sessions/${encodeURIComponent(resumeSession)}/runs/resume` : '/api/runs';
  let descriptor: RunDescriptor | undefined;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await apiFetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), signal: AbortSignal.timeout(15000) });
      await checkReply(response);
      descriptor = await response.json(); break;
    } catch (error) {
      if (error instanceof RunFailure || error instanceof DOMException && error.name === 'AbortError' || attempt === 1) throw error;
      // A lost submission response retries the identical idempotency key/payload.
    }
  }
  if (!descriptor?.run_id || !descriptor.session_id) throw new RunFailure('服务未返回有效的运行标识。', 'INVALID_RUN_DESCRIPTOR');
  onEvent({ type: 'session', run_id: descriptor.run_id, session_id: descriptor.session_id, persistence: 'postgresql' });
  return subscribeRun(descriptor.run_id, onEvent);
}
