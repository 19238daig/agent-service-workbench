export type ActionRequest = { action_id: string; session_id: string; turn_id: string; tool: string; parameters: Record<string, unknown>;
  parameter_hash: string; status: string; required_approval: 'user' | 'staff'; expires_at: string; executed?: boolean;
  approvals: { stage: string; decision: string; principal_role: string; expires_at: string }[] };
export type Execution = { action_id?: string; ticket_id?: string; ticket_status?: string; status?: string; funds_refunded?: boolean; requested_amount?: string };
export type TaskPlan = { status?: string; tasks?: { id: string; kind: string; status: string; depends_on: string[] }[];
  missing_fields?: string[]; steps_used?: number; max_steps?: number };
export type Ticket = { ticket_id: string; summary: string; details: string; type: string; priority: string; status: string; order_id?: string;
  requested_amount?: string; version: number; assignee_id?: string; allowed_transitions: string[]; can_assign: boolean; source: string; updated_at: string };
export type TicketEvent = { id: string; event_type: string; from_status?: string; to_status: string; actor_role?: string; created_at: string; note: string; ticket_version?: number };
export type Job = { id: string; kind: string; status: string; stage: string; progress: number; total: number; attempt: number; max_attempts: number;
  payload_hash: string; error_code?: string; result?: { document_id?: string; index_id?: string; chunks?: number; active?: boolean }; created_at: string };
export type Document = { id: string; title: string; source: string; version: string; status: string; access_scope: string; domain: string;
  content_hash: string; chunks: number; publisher: string; license: string; source_url?: string; effective_from: string; file_url: string;
  preview?: { id: string; content: string; page?: number; heading?: string }[] };
export type IndexVersion = { id: string; active: boolean; documents?: number; chunks?: number; protocol?: string; created_at: string;
  model_identity: { embedding?: string; dimensions?: number } };
export const statuses: Record<string, string> = {
  awaiting_user: '待本人确认', awaiting_staff: '待独立客服审批', approved: '已确认，可继续办理', ready: '可继续办理', executed: '已办理',
  rejected: '已拒绝', cancelled: '已取消', expired: '已过期', stale: '内容已变化', created: '待处理', processing: '处理中', pending_review: '待复核',
  resolved: '已解决', closed: '已关闭', escalated: '已升级', queued: '排队中', running: '执行中', retry_wait: '等待重试', failed: '失败',
  succeeded: '已完成', cancel_requested: '正在停止', draft: '草稿', published: '已发布', withdrawn: '已撤回', completed: '已完成',
  needs_information: '待补充', waiting_approval: '待确认 / 审批', blocked: '已停止', pending: '待执行', interrupted: '已中断',
};
export const names: Record<string, string> = { ticket_create: '登记服务工单', ticket_transition: '变更工单状态', policy_lookup: '查找政策证据',
  order_query: '核对订单', order_eligibility: '核验申请条件', ticket_query: '查询工单', refund: '退款申请', general: '服务咨询', complaint: '投诉',
  low: '低', medium: '普通', high: '高', urgent: '紧急', public: '公开', private: '仅本人', support: '客服可见', admin: '管理员可见',
  ingest: '文档解析入库', index: '构建向量索引', retrieval_evaluation: '离线检索评测' };
export const label = (value?: string) => value ? statuses[value] || names[value] || value : '—';
export const date = (value?: string) => value ? new Date(value).toLocaleString('zh-CN') : '—';
