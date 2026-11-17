import { test, expect, type Page } from '@playwright/test';
import { createServer, type ServerResponse } from 'node:http';
import { mockSession } from './auth-helper';

const runId = 'durable-browser-run';
const sessionId = 'durable-browser-session';
const frame = (id: number, event: object) => `id: ${id}\nevent: message\ndata: ${JSON.stringify({ ...event, run_id: runId })}\n\n`;
const first = frame(1, { type: 'session', session_id: sessionId, persistence: 'postgresql' })
  + frame(2, { type: 'node_start', node: 'intent_router', model: 'fixture' });
const result = { type: 'complete', session_id: sessionId, persistence: 'postgresql', compliance_passed: true, response: '持久化订阅已恢复', execution_trace: [] };
const rest = frame(3, { type: 'node_end', node: 'intent_router', status: 'done' }) + frame(4, result);
async function setup(page: Page) {
  await mockSession(page);
  await page.route('**/health', r => r.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', r => r.fulfill({ status: 404 }));
}

test('a broken SSE connection resumes with Last-Event-ID without another POST', async ({ page }) => {
  await setup(page);let posts = 0;let reads = 0;const cursors: string[] = [];
  const server = createServer((request, response) => {
    if (request.method === 'OPTIONS') { response.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Authorization, Last-Event-ID' });response.end();return; }
    cursors.push(String(request.headers['last-event-id'] || '0'));reads++;
    response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': '*' });
    if (reads === 1) { response.write(first);setTimeout(() => response.destroy(), 250); }
    else response.end(rest);
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing mock port');
  await page.route('**/api/runs', r => { posts++;return r.fulfill({ status: 202, json: { run_id: runId, session_id: sessionId, status: 'queued' } }); });
  await page.route('**/api/runs/*/events', r => r.continue({ url: `http://127.0.0.1:${address.port}/events` }));
  await page.route('**/api/history/**', r => r.fulfill({ json: { messages: [{ role: 'user', content: 'saved' }] } }));
  try {
    await page.goto('/');await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
    await page.getByRole('textbox', { name: '输入消息' }).fill('检查连接恢复');await page.getByRole('button', { name: '发送消息' }).click();
    await expect(page.getByText(result.response, { exact: true })).toBeVisible();
    expect(posts).toBe(1);expect(reads).toBe(2);expect(cursors).toEqual(['0', '2']);
  } finally { server.closeAllConnections();await new Promise<void>(resolve => server.close(() => resolve())); }
});

test('refresh while running restores progress using only GET subscriptions', async ({ page }) => {
  await setup(page);let posts = 0;let resumes = 0;let reads = 0;let finished = false;
  const connections = new Set<ServerResponse>();
  const server = createServer((request, response) => {
    if (request.method === 'OPTIONS') { response.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Authorization, Last-Event-ID' });response.end();return; }
    reads++;response.writeHead(200, { 'Content-Type': 'text/event-stream', 'Access-Control-Allow-Origin': '*' });response.write(first);
    if (finished) response.end(rest);else { connections.add(response);response.on('close', () => connections.delete(response)); }
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Missing mock port');
  await page.route('**/api/runs', r => { posts++;return r.fulfill({ status: 202, json: { run_id: runId, session_id: sessionId, status: 'queued' } }); });
  await page.route('**/runs/resume', r => { resumes++;return r.fulfill({ status: 500 }); });
  await page.route('**/api/runs/*/events', r => r.continue({ url: `http://127.0.0.1:${address.port}/events` }));
  await page.route('**/api/sessions?*', r => r.fulfill({ json: { sessions: posts ? [{ id: sessionId, title: '检查刷新恢复', status: 'active' }] : [] } }));
  await page.route('**/api/history/**', r => r.fulfill({ json: { persistence: 'postgresql', can_continue: finished, can_resume: false,
    active_run_id: finished ? null : runId, messages: [{ id: 'user-1', role: 'user', content: '检查刷新恢复' },
      ...(finished ? [{ id: 'answer-1', role: 'assistant', content: result.response, metadata: result }] : [])] } }));
  try {
    await page.goto('/');await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
    await page.getByRole('textbox', { name: '输入消息' }).fill('检查刷新恢复');await page.getByRole('button', { name: '发送消息' }).click();
    await expect(page.locator('[data-node="intent_router"]')).toHaveAttribute('data-status', 'running');
    await page.reload();await expect.poll(() => reads).toBeGreaterThanOrEqual(2);
    await expect(page.locator('[data-node="intent_router"]')).toHaveAttribute('data-status', 'running');
    expect(posts).toBe(1);expect(resumes).toBe(0);
    finished = true;for (const response of connections) response.end(rest);
    await expect(page.getByText(result.response, { exact: true })).toBeVisible();
    await expect(page.getByRole('article', { name: '你的消息' })).toHaveCount(1);
    expect(posts).toBe(1);expect(resumes).toBe(0);
  } finally { server.closeAllConnections();await new Promise<void>(resolve => server.close(() => resolve())); }
});

test('lost submission response is recovered by saved request key without resending', async ({ page }) => {
  await setup(page);let posts = 0;let lookups = 0;
  await page.addInitScript(() => localStorage.setItem('smart-cs.workspace.v2:saved-user', JSON.stringify({ userId: 'saved-user', activeId: 'pending', conversations: [{
    id: 'pending', title: '已接受的消息', createdAt: 1, updatedAt: 1, draft: '', messages: [
      { id: 'u', role: 'user', content: '已接受的消息' }, { id: 'a', role: 'assistant', content: '', status: 'pending', replyTo: 'u', clientRequestId: 'saved-request-key-20' },
    ],
  }] })));
  await page.route('**/api/runs', r => { posts++;return r.fulfill({ status: 500 }); });
  await page.route('**/api/runs/by-request/*', r => { lookups++;return r.fulfill({ json: { run_id: runId, session_id: sessionId, status: 'succeeded' } }); });
  await page.route('**/api/runs/*/events', r => r.fulfill({ contentType: 'text/event-stream', body: frame(4, result) }));
  await page.route('**/api/history/**', r => r.fulfill({ json: { persistence: 'postgresql', can_continue: true, messages: [
    { id: 'user-1', role: 'user', content: '已接受的消息' }, { id: 'answer-1', role: 'assistant', content: result.response, metadata: result },
  ] } }));
  await page.goto('/');await expect(page.getByText(result.response, { exact: true })).toBeVisible();
  expect(posts).toBe(0);expect(lookups).toBe(1);
});
