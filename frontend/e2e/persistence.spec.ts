import { test, expect } from '@playwright/test';
import { mockSession } from './auth-helper';

test('server history restores with empty browser storage and recovery never duplicates input', async ({ page }) => {
  await mockSession(page);
  await page.route('**/health', r => r.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', r => r.fulfill({ status: 404 }));
  await page.route('**/api/sessions?*', r => r.fulfill({ json: { sessions: [{ id: 'owned-server-session', title: '服务器中保存的工单咨询', status: 'active', updated_at: new Date().toISOString() }] } }));
  let finished = false; let resumes = 0; let chats = 0;
  const history = () => ({ persistence: 'postgresql', can_resume: !finished, can_continue: finished,
    messages: [{ id: 'original-user', role: 'user', content: '请查一下我的工单进度。' },
      ...(finished ? [{ id: 'published-answer', role: 'assistant', content: '工单正在处理中。', metadata: { compliance_passed: true } }] : [])] });
  await page.route('**/api/history/**', r => r.fulfill({ json: history() }));
  await page.route('**/api/runs', r => { chats++; return r.fulfill({ status: 500 }); });
  await page.route('**/api/sessions/owned-server-session/runs/resume', r => {
    resumes++; expect(r.request().postDataJSON().client_request_id).toBeTruthy(); finished = true;
    return r.fulfill({ status: 202, json: { run_id: 'resumed-run', session_id: 'owned-server-session', status: 'queued' } });
  });
  await page.route('**/api/runs/resumed-run/events', r => {
    return r.fulfill({ contentType: 'text/event-stream', body: [
      { type: 'session', session_id: 'owned-server-session', persistence: 'postgresql' },
      { type: 'complete', session_id: 'owned-server-session', persistence: 'postgresql', response: '工单正在处理中。', compliance_passed: true },
    ].map(e => 'data: '+JSON.stringify(e)+'\n\n').join('') });
  });
  await page.goto('/');
  await expect(page.getByRole('log').getByText('请查一下我的工单进度。', { exact: true })).toHaveCount(1);
  await expect(page.getByRole('button', { name: '恢复上轮处理', exact: true })).toBeVisible();
  expect(resumes).toBe(0); expect(chats).toBe(0);
  await page.screenshot({ path: test.info().outputPath('recovery-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: '恢复上轮处理', exact: true }).click();
  await expect(page.getByText('工单正在处理中。', { exact: true })).toBeVisible();
  await expect(page.getByRole('log').getByText('请查一下我的工单进度。', { exact: true })).toHaveCount(1);
  expect(resumes).toBe(1); expect(chats).toBe(0);
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByText('工单正在处理中。', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '恢复上轮处理', exact: true })).toHaveCount(0);
  expect(resumes).toBe(1);
  await page.screenshot({ path: test.info().outputPath('server-history-mobile.png'), fullPage: true });
});

test('incompatible graph preserves server history and requires a new session', async ({ page }) => {
  await mockSession(page);
  await page.route('**/health', r => r.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', r => r.fulfill({ status: 404 }));
  await page.route('**/api/sessions?*', r => r.fulfill({ json: { sessions: [{ id: 'legacy', title: '旧版咨询', status: 'active' }] } }));
  await page.route('**/api/history/**', r => r.fulfill({ json: { persistence: 'postgresql', can_resume: false, can_continue: false,
    recovery_reason: 'GRAPH_VERSION_INCOMPATIBLE', messages: [{ id: 'old-user', role: 'user', content: '旧会话仍可查看' }] } }));
  await page.goto('/');
  await expect(page.getByText('旧会话仍可查看', { exact: true })).toBeVisible();
  await expect(page.getByText('此会话的流程或授权上下文已变化。历史已保留，继续发送将新建会话。')).toBeVisible();
  await expect(page.getByRole('button', { name: '恢复上轮处理' })).toHaveCount(0);
});

test('legacy browser-only messages remain labelled when PostgreSQL has no old text', async ({ page }) => {
  await mockSession(page);
  await page.addInitScript(() => localStorage.setItem('smart-cs.workspace.v2:saved-user', JSON.stringify({ userId: 'saved-user', activeId: 'old',
    conversations: [{ id: 'old', sessionId: 'legacy-empty', title: '本地旧对话', createdAt: 1, updatedAt: 1, draft: '',
      messages: [{ id: 'cached', role: 'user', content: '只在浏览器保留的旧消息' }] }] })));
  await page.route('**/health', r => r.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', r => r.fulfill({ status: 404 }));
  await page.route('**/api/history/**', r => r.fulfill({ json: { persistence: 'postgresql', can_resume: false, can_continue: false, messages: [] } }));
  await page.goto('/');
  await expect(page.getByText('只在浏览器保留的旧消息', { exact: true })).toBeVisible();
  await expect(page.getByText('服务器没有此旧版消息记录，当前展示浏览器保留的旧记录。继续发送将新建会话。')).toBeVisible();
  await expect(page.getByRole('button', { name: '恢复上轮处理' })).toHaveCount(0);
});
