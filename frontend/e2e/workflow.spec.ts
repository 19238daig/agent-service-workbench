import { mockRunSubmission } from './run-helper';
import { mockSession } from './auth-helper';
import { test, expect, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => { await mockSession(page); });
import { createServer } from 'node:http';
import { defaultNodes } from '../src/workflow';

const steps = [
  { node: 'intent_router', model: 'router-model', summary: { primary: 'consultation', confidence: 0.96 } },
  { node: 'supervisor_route', summary: { route: 'knowledge_rag' } },
  { node: 'knowledge_rag', model: 'knowledge-model', summary: { retrieved_count: 3, used_count: 1, sources: ['refund_policy.md'] } },
  { node: 'compliance_check', model: 'review-model', summary: { passed: true, risk_level: 'low', violation_count: 0 } },
  { node: 'synthesize', summary: { released: true } },
].map(s => ({ ...s, status: 'done', duration_ms: 1200 }));
const frame = (event: object) => `data: ${JSON.stringify(event)}\r\n\r\n`;
const finalEvent = { type: 'complete', response: '7天内可申请退款。', session_id: 'stream-session', intent: 'knowledge_rag', compliance_passed: true, execution_trace: steps, duration_ms: 6000 };
const rest = steps.map((s, i) => (i ? frame({ type: 'node_start', node: s.node, model: 'model' in s ? s.model : '' }) : '') + frame({ type: 'node_end', ...s })).join('') + frame(finalEvent);

async function setup(page: Page) {
  await mockRunSubmission(page);
  await page.route('**/health', route => route.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/history/**', route => route.fulfill({ json: { messages: [{ role: 'user', content: '已保存' }] } }));
  await page.route('**/api/workflow', route => route.fulfill({ json: { ready: true, nodes: defaultNodes.map(n => ({ ...n, model: n.id === 'intent_router' ? 'router-model' : `${n.id}-model` })), edges: [] } }));
}

test('shows real incremental progress, the selected branch, model and persisted trace', async ({ page }) => {
  await setup(page);
  let release!: () => void;
  const finish = new Promise<void>(resolve => { release = resolve; });
  const server = createServer((_request, response) => {
    if (_request.method === 'OPTIONS') { response.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Authorization, Last-Event-ID' });response.end();return; }
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-cache' });
    response.write(frame({ type: 'session', session_id: 'stream-session' }));
    response.write(frame({ type: 'node_start', node: 'intent_router', model: 'router-model' }));
    void finish.then(() => response.end(rest));
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No mock stream port');
  await page.route('**/api/runs/*/events', route => route.continue({ url: `http://127.0.0.1:${address.port}/stream` }));
  try {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    const panel = page.getByRole('region', { name: 'Agent 协作过程' });
    await expect(panel.getByRole('heading', { name: '四 Agent 协作' })).toBeVisible();
    await expect(panel.locator('.agent-node')).toHaveCount(4);
    await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
    await page.getByRole('textbox', { name: '输入消息' }).fill('退款政策是什么？');
    await page.getByRole('button', { name: '发送消息' }).click();
    await expect(panel.locator('[data-node="intent_router"]')).toHaveAttribute('data-status', 'running');
    await expect(panel.getByText('router-model', { exact: true })).toBeVisible();
    await expect(page.locator('.markdown-body')).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath('workflow-running.png'), fullPage: true });
    release();
    await expect(page.getByText('7天内可申请退款。', { exact: true })).toBeVisible();
    await expect(panel.locator('[data-node="ticket_handler"]')).toHaveAttribute('data-status', 'skipped');
    await expect(panel.getByText('模型置信度 96%', { exact: true })).toBeVisible();
    await expect(panel.getByText('refund_policy.md', { exact: true })).toBeVisible();
    await expect(panel.getByText('审查通过', { exact: true })).toBeVisible();
    await page.reload();
    await expect(panel.locator('[data-node="knowledge_rag"]')).toHaveAttribute('data-status', 'done');
    await expect(panel.locator('[data-node="ticket_handler"]')).toHaveAttribute('data-status', 'skipped');
    await page.screenshot({ path: test.info().outputPath('workflow-completed.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '协作过程', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '协作过程' })).toBeVisible();
    await expect(panel.getByText('refund_policy.md', { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath('workflow-mobile.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog', { name: '协作过程' })).not.toBeVisible();
  } finally {
    release(); server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
});

test('an incomplete stream preserves the failed node and never creates a final reply', async ({ page }) => {
  await setup(page);
  await page.route('**/api/runs/*/events', route => route.fulfill({ contentType: 'text/event-stream', body: frame({ type: 'node_start', node: 'intent_router', model: 'router-model' }) }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('退款政策');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('button', { name: '重试', exact: true })).toBeVisible();
  const panel = page.getByRole('region', { name: 'Agent 协作过程' });
  await expect(panel.locator('[data-node="intent_router"]')).toHaveAttribute('data-status', 'error');
  await expect(page.locator('.markdown-body')).toHaveCount(0);
  await page.reload();
  await expect(panel.locator('[data-node="intent_router"]')).toHaveAttribute('data-status', 'error');
});

test('a rejected reply shows the blocked review and bypassed branches', async ({ page }) => {
  await setup(page);
  const trace = [steps[0], { ...steps[1], summary: { route: 'clarification' } },
    { ...steps[3], status: 'blocked', summary: { passed: false, risk_level: 'high', violation_count: 1 } },
    { ...steps[4], summary: { released: false } }];
  const safeReply = '这条回复需要进一步核实。请通过官方渠道联系人工客服处理。';
  const body = trace.map(s => frame({ type: 'node_start', node: s.node }) + frame({ type: 'node_end', ...s })).join('')
    + frame({ ...finalEvent, intent: 'clarification', compliance_passed: false, response: safeReply, execution_trace: trace });
  await page.route('**/api/runs/*/events', route => route.fulfill({ contentType: 'text/event-stream', body }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('处理一下');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText(safeReply, { exact: true })).toBeVisible();
  const panel = page.getByRole('region', { name: 'Agent 协作过程' });
  await expect(panel.locator('[data-node="compliance_check"]')).toHaveAttribute('data-status', 'blocked');
  await expect(panel.locator('.node-skipped')).toHaveCount(2);
  await expect(panel.getByText('审查未通过', { exact: true })).toBeVisible();
  await expect(panel.getByText('已返回安全提示', { exact: true })).toBeVisible();
});

test('order eligibility shows the database cap and read-only business branch', async ({ page }) => {
  await setup(page);
  const trace = [steps[0], { ...steps[1], summary: { route: 'ticket_handler' } },
    { node: 'ticket_handler', status: 'done', model: 'ticket-model', duration_ms: 900,
      summary: { action: 'refund_eligibility', found: true, order_id: 'ORDER-A-006', decision: 'allowed', maximum_refund_amount: '350.00' } },
    steps[3], steps[4]];
  const body = trace.map(step => frame({ type: 'node_start', node: step.node }) + frame({ type: 'node_end', ...step })).join('')
    + frame({ ...finalEvent, intent: 'ticket_handler', response: '实付 500.00 元，当前申请上限 350.00 元；仅查询，不执行退款。', execution_trace: trace });
  await page.route('**/api/runs/*/events', route => route.fulfill({ contentType: 'text/event-stream', body }));
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('核验订单 ORDER-A-006 的申请条件');
  await page.getByRole('button', { name: '发送消息' }).click();
  const panel = page.getByRole('region', { name: 'Agent 协作过程' });
  await expect(panel.locator('[data-node="ticket_handler"]')).toHaveAttribute('data-status', 'done');
  await expect(panel.getByText('核验退款申请条件', { exact: true })).toBeVisible();
  await expect(panel.getByText('ORDER-A-006', { exact: true })).toBeVisible();
  await expect(panel.getByText('当前申请上限：350.00 元', { exact: true })).toBeVisible();
  await expect(panel.locator('[data-node="knowledge_rag"]')).toHaveAttribute('data-status', 'skipped');
});

test('controlled ticket writes are shown as not created', async ({ page }) => {
  await setup(page);
  const trace = [steps[0], { ...steps[1], summary: { route: 'ticket_handler' } },
    { node: 'ticket_handler', status: 'done', duration_ms: 500, summary: { action: 'create', found: false, write_blocked: true } }, steps[3], steps[4]];
  const body = trace.map(step => frame({ type: 'node_start', node: step.node }) + frame({ type: 'node_end', ...step })).join('')
    + frame({ ...finalEvent, intent: 'ticket_handler', response: '工单尚未创建，请确认人工登记。', execution_trace: trace });
  await page.route('**/api/runs/*/events', route => route.fulfill({ contentType: 'text/event-stream', body }));
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('请登记售后申请');
  await page.getByRole('button', { name: '发送消息' }).click();
  const panel = page.getByRole('region', { name: 'Agent 协作过程' });
  await expect(panel.getByText('待人工登记，尚未创建', { exact: true })).toBeVisible();
  await expect(page.getByText('工单尚未创建，请确认人工登记。', { exact: true })).toBeVisible();
});
