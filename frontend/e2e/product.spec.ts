import { test, expect, type Page } from '@playwright/test';
import { mockAnswer } from './run-helper';
import { mkdir, readFile, writeFile } from 'node:fs/promises';

const action = { action_id: 'action-21', session_id: 'session-21', turn_id: 'turn-21', tool: 'ticket_create', parameters: { summary: '页面闭环验收', type: 'general', details: '请核实办理流程' }, parameter_hash: 'a'.repeat(64), status: 'awaiting_user', required_approval: 'user', expires_at: '2099-01-01T00:00:00Z', approvals: [] };
const ticket = { ticket_id: 'TK-PRODUCT-21', summary: '页面闭环验收', details: '核实办理流程', type: 'general', priority: 'medium', status: 'created', source: 'approved_action', version: 1, can_assign: true, allowed_transitions: ['closed'], updated_at: '2026-11-15T00:00:00Z' };
const document = { id: 'doc-21', title: '公开政策原件', source: 'policy.html', version: 'v21', status: 'draft', access_scope: 'private', domain: 'consumer_reference', content_hash: 'b'.repeat(64), chunks: 2, publisher: '政策发布机构', license: '公开政务资料', effective_from: '2020-01-01', file_url: '/api/admin/knowledge/documents/doc-21/file', preview: [{ id: 'chunk-21', content: '原文政策切片，用于核对来源。', heading: '政策原文' }] };

async function setup(page: Page, role = 'customer') {
  await page.route('**/api/**', route => route.fulfill({ status: 404, json: { detail: 'Not mocked' } }));
  await page.route('**/api/auth/refresh', route => route.fulfill({ json: { access_token: 'fixture-token', user: { id: 'product-user', display_name: '验收用户', role } } }));
  await page.route('**/api/sessions?*', route => route.fulfill({ json: { sessions: [], has_more: false } }));
  await page.route('**/health', route => route.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/action-requests?*', route => route.fulfill({ json: { items: [] } }));
  await page.route('**/api/tickets?*', route => route.fulfill({ json: { tickets: [ticket], total: 1, has_more: false } }));
  await page.route('**/api/tickets/TK-PRODUCT-21', route => route.fulfill({ json: { ticket } }));
  await page.route('**/api/tickets/TK-PRODUCT-21/events?*', route => route.fulfill({ json: { events: [{ id: 'event-21', to_status: 'created', note: '已登记申请', actor_role: 'customer', created_at: ticket.updated_at }], has_more: false } }));
  await page.route('**/api/support/staff', route => route.fulfill({ json: { staff: [{ id: 'staff-21', display_name: '处理客服' }] } }));
}

test('missing slots submit a normal follow-up in the original session; citation preview is escaped', async ({ page }) => {
  await setup(page);const requests: Record<string, unknown>[] = [];
  await mockAnswer(page, request => { requests.push(request.postDataJSON());return requests.length === 1 ? { session_id: 'session-21', response: '请补充订单号。', compliance_passed: true, task_plan: { status: 'needs_information', tasks: [{ id: 'one', kind: 'order_query', status: 'needs_information', depends_on: [] }], missing_fields: ['order_id'] } } : { session_id: 'session-21', response: '已核对政策。[1]', compliance_passed: true, citations: [{ number: 1, document_id: 'doc-21', chunk_id: 'chunk-21', source: 'policy.html', url: '/api/knowledge/documents/doc-21/file' }] }; });
  await page.route('**/api/knowledge/documents/doc-21/chunks/chunk-21', route => route.fulfill({ json: { content: '<script>window.hacked=true</script>政策正文', version: 'v21', currently_effective: false } }));
  await page.goto('/');await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();await page.getByRole('textbox', { name: '输入消息' }).fill('核对我的订单');await page.getByRole('button', { name: '发送消息' }).click();
  await page.getByRole('textbox', { name: '订单号', exact: true }).fill('ORDER-21');await page.getByRole('button', { name: '补充并继续' }).click();
  await expect(page.getByRole('button', { name: '[1] 核对引用正文' })).toBeVisible();expect(requests[1].session_id).toBe('session-21');expect(requests[1].message).toBe('订单号：ORDER-21');
  await page.getByRole('button', { name: '[1] 核对引用正文' }).click();await expect(page.getByText('这是历史引用，目前已不属于有效检索资料，请核对最新版本。')).toBeVisible();expect(await page.evaluate(() => (window as unknown as { hacked?: boolean }).hacked)).toBeUndefined();
});

test('confirmation requires checked consent and current hash; continue uses one durable resume', async ({ page }) => {
  await setup(page);let current = { ...action };const writes: string[] = [];
  await mockAnswer(page, () => ({ response: '请核对服务申请。', session_id: 'session-21', compliance_passed: true, action_request: action }));
  await page.route('**/api/action-requests/action-21', route => route.fulfill({ json: current }));
  await page.route('**/api/action-requests/action-21/confirm', route => { writes.push('confirm');expect(route.request().postDataJSON()).toMatchObject({ parameter_hash: action.parameter_hash, decision: 'approve' });current = { ...current, status: 'approved' };return route.fulfill({ json: current }); });
  await page.route('**/api/sessions/session-21/runs/resume', route => { writes.push('resume');expect(route.request().postDataJSON().client_request_id).toBeTruthy();return route.fulfill({ status: 202, json: { run_id: 'resumed-21', session_id: 'session-21', status: 'queued' } }); });
  await page.route('**/api/runs/resumed-21/events', route => route.fulfill({ contentType: 'text/event-stream', body: `id: 1\ndata: ${JSON.stringify({ type: 'complete', session_id: 'session-21', response: '申请已登记。', compliance_passed: true, action_execution: { ticket_id: ticket.ticket_id } })}\n\n` }));
  await page.goto('/');await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();await page.getByRole('textbox', { name: '输入消息' }).fill('登记服务工单');await page.getByRole('button', { name: '发送消息' }).click();
  const card = page.getByRole('region', { name: '申请确认', exact: true });await expect(card.getByRole('button', { name: '确认申请', exact: true })).toBeDisabled();expect(writes).toEqual([]);
  await card.getByRole('checkbox').check();await card.getByRole('button', { name: '确认申请', exact: true }).click();await expect(card.getByRole('button', { name: '继续办理', exact: true })).toBeVisible();expect(writes).toEqual(['confirm']);
  await card.getByRole('button', { name: '继续办理', exact: true }).click();await expect(page.getByRole('button', { name: 'TK-PRODUCT-21 · 查看工单' })).toBeVisible();expect(writes).toEqual(['confirm', 'resume']);
  await page.getByRole('button', { name: 'TK-PRODUCT-21 · 查看工单' }).click();await expect(page.getByRole('region', { name: '工单详情' })).toContainText(ticket.ticket_id);
});

test('ticket assignment binds its version and protected transition only prepares a chat draft', async ({ page }) => {
  await setup(page);
  let current = { ...ticket, assignee_id: 'staff-current' };
  let direct = false;
  await page.route('**/api/support/staff', route => route.fulfill({ json: { staff: [
    { id: 'staff-current', display_name: '原处理客服' }, { id: 'staff-21', display_name: '处理客服' },
  ] } }));
  await page.route('**/api/tickets/TK-PRODUCT-21', route => route.fulfill({ json: { ticket: current } }));
  await page.route('**/api/tickets/TK-PRODUCT-21/assignment', route => {
    expect(route.request().postDataJSON()).toEqual({ assignee_id: 'staff-21', expected_version: 1, reason: '请处理此申请' });
    current = { ...current, assignee_id: 'staff-21', version: 2 };
    return route.fulfill({ json: { ticket: current } });
  });
  await page.route('**/api/tickets/TK-PRODUCT-21/transitions', route => { direct = true;return route.fulfill({ status: 403 }); });
  await page.goto('/#/tickets');await page.getByRole('button', { name: /TK-PRODUCT-21/ }).click();const detail = page.getByRole('region', { name: '工单详情' });
  await expect(detail.getByLabel('分派给')).toHaveValue('staff-current');
  await expect(detail.getByRole('definition').filter({ hasText: '原处理客服' })).toBeVisible();
  await detail.getByLabel('分派给').selectOption('staff-21');
  await detail.getByLabel('分派说明').fill('请处理此申请');
  await expect(detail.getByLabel('处理原因')).toHaveValue('');
  await detail.getByRole('button', { name: '确认分派' }).click();
  await expect(detail.getByLabel('分派给')).toHaveValue('staff-21');
  await expect(detail.getByLabel('分派说明')).toHaveValue('');
  await detail.getByLabel('目标状态').selectOption('closed');await detail.getByLabel('处理原因').fill('问题已解决');await detail.getByRole('button', { name: '生成处理草稿' }).click();
  await expect(page.getByRole('textbox', { name: '输入消息' })).toHaveValue(/TK-PRODUCT-21.*closed.*当前版本 2.*问题已解决/);expect(direct).toBe(false);
});

test('independent staff approval records a decision without executing an action', async ({ page }) => {
  await setup(page, 'support');let decided = 0;let current = { ...action, required_approval: 'staff', status: 'awaiting_staff' };
  await page.route('**/api/support/tickets?*', route => route.fulfill({ json: { tickets: [], total: 0, has_more: false } }));
  await page.route('**/api/staff/action-requests?*', route => route.fulfill({ json: { items: [current] } }));
  await page.route('**/api/staff/action-requests/action-21', route => route.fulfill({ json: current }));
  await page.route('**/api/staff/action-requests/action-21/decide', route => { decided++;expect(route.request().postDataJSON().parameter_hash).toBe(action.parameter_hash);current = { ...current, status: 'approved' };return route.fulfill({ json: current }); });
  const executed: string[] = [];page.on('request', request => { if (/execute|runs\/resume/.test(request.url())) executed.push(request.url()); });
  await page.goto('/#/support');await page.getByRole('tab', { name: '待审批申请' }).click();await page.getByRole('button', { name: /登记服务工单/ }).click();const card = page.getByRole('region', { name: '人工审批详情' });
  await expect(card.getByRole('button', { name: '批准申请' })).toBeDisabled();await card.getByRole('checkbox').check();await card.getByRole('button', { name: '批准申请' }).click();await expect(card).toContainText('审批已记录，申请人可继续办理。');expect(decided).toBe(1);expect(executed).toEqual([]);
});

async function knowledge(page: Page) {
  await setup(page, 'admin');
  await page.route('**/api/admin/knowledge/documents?*', route => route.fulfill({ json: { documents: [document], total: 1, has_more: false } }));
  await page.route('**/api/admin/knowledge/documents/doc-21', route => route.fulfill({ json: document }));
  await page.route('**/api/admin/knowledge/events', route => route.fulfill({ json: { events: [] } }));
  await page.route('**/api/admin/jobs', route => route.fulfill({ json: { jobs: [] } }));
}

test('knowledge publication needs source acknowledgement and version; a stale activation shows a safe error', async ({ page }) => {
  await knowledge(page);let published = false;
  await page.route('**/api/admin/knowledge/documents/doc-21/publish', route => { expect(route.request().postDataJSON()).toEqual({ content_hash: document.content_hash, version: 'v21' });published = true;return route.fulfill({ json: { document: { ...document, status: 'published' } } }); });
  await page.route('**/api/admin/knowledge/indexes', route => route.fulfill({ json: { active_index_id: 'old', indexes: [{ id: 'old', active: true, documents: 11, chunks: 547, protocol: 'dense-exact-v1', model_identity: { embedding: 'bge-m3' } }, { id: 'new', active: false, documents: 12, chunks: 641, protocol: 'dense-exact-v1', model_identity: { embedding: 'bge-m3' } }] } }));
  await page.route('**/api/admin/knowledge/indexes/new/activate', route => { expect(route.request().postDataJSON()).toEqual({ expected_active_index_id: 'old' });return route.fulfill({ status: 409, json: { error: { code: 'INDEX_ACTIVE_CHANGED', message: '其他操作者已切换索引，请核实当前版本。' } } }); });
  await page.goto('/#/knowledge');await page.getByRole('button', { name: /公开政策原件/ }).click();const detail = page.getByRole('region', { name: '知识文档详情' });await expect(detail.getByRole('button', { name: '确认发布文档' })).toBeDisabled();await expect(detail).toContainText('原文政策切片，用于核对来源。');
  await detail.getByRole('checkbox').check();await detail.getByRole('button', { name: '确认发布文档' }).click();await expect.poll(() => published).toBe(true);
  await page.getByRole('tab', { name: '索引与记录' }).click();await page.locator('input[value="new"]').check();await page.getByRole('checkbox', { name: '我已核对目标索引的文档范围和模型' }).check();await page.getByRole('button', { name: '校验并启用所选索引' }).click();await expect(page.getByRole('alert')).toContainText('其他操作者已切换索引');
});

test('upload sends original bytes and provenance as an asynchronous private draft', async ({ page }) => {
  await knowledge(page);let submission: Record<string, unknown> | undefined;
  await page.route('**/api/admin/job-artifacts?*', route => { expect(route.request().postDataBuffer()?.toString()).toBe('<main>policy</main>');return route.fulfill({ status: 201, json: { artifact_sha256: 'c'.repeat(64), extension: '.html' } }); });
  await page.route('**/api/admin/jobs', route => { if (route.request().method() === 'GET') return route.fulfill({ json: { jobs: [] } });submission = route.request().postDataJSON();return route.fulfill({ status: 202, json: { id: 'job-21', kind: 'ingest', status: 'queued' } }); });
  await page.goto('/#/knowledge');await page.getByRole('tab', { name: '上传文件' }).click();await page.getByLabel('知识原文件').setInputFiles({ name: 'policy.html', mimeType: 'text/html', buffer: Buffer.from('<main>policy</main>') });await page.getByLabel('发布者', { exact: true }).fill('政策发布机构');await page.getByLabel('使用许可', { exact: true }).fill('公开政务资料');await page.getByRole('button', { name: '上传并后台入库' }).click();
  await expect(page.getByRole('tab', { name: '后台任务' })).toHaveAttribute('aria-selected', 'true');expect(submission).toMatchObject({ work: { kind: 'ingest', publish: false, extension: '.html', document: { access_scope: 'private', owner_id: 'product-user', publisher: '政策发布机构', license: '公开政务资料' } } });expect(submission?.idempotency_key).toBeTruthy();
});

test('customer role hides staff and knowledge; hash navigation cannot grant permissions', async ({ page }) => {
  await setup(page);await page.goto('/#/knowledge');await expect(page.getByRole('textbox', { name: '输入消息' })).toBeVisible();await expect(page.getByRole('button', { name: '客服工作台', exact: true })).toHaveCount(0);await expect(page.getByRole('button', { name: '知识管理', exact: true })).toHaveCount(0);
});

test('history synchronization preserves an expanded citation and repeated node events keep their order', async ({ page }) => {
  await setup(page);let historyCalls = 0;
  const reply = { response: '可核对的政策正文。[1]', session_id: 'stable-session', compliance_passed: true, citations: [{ number: 1, document_id: 'doc-21', chunk_id: 'chunk-21', source: 'policy.html', url: '/api/knowledge/documents/doc-21/file' }], execution_trace: [{ node: 'knowledge_rag', status: 'done' as const }, { node: 'task_dispatch', status: 'done' as const }, { node: 'knowledge_rag', status: 'done' as const }] };
  await mockAnswer(page, () => reply);
  await page.route('**/api/history/stable-session*', async route => { historyCalls++;await new Promise(resolve => setTimeout(resolve, 1000));return route.fulfill({ json: { persistence: 'postgresql', can_continue: true, messages: [{ id: 'server-user', role: 'user', content: '咨询政策' }, { id: 'server-answer', role: 'assistant', content: reply.response, metadata: { ...reply, run_id: 'fixture-run-1' } }] } }); });
  await page.route('**/api/knowledge/documents/doc-21/chunks/chunk-21', route => route.fulfill({ json: { content: '可核对的政策正文', version: 'v21', currently_effective: true } }));
  await page.goto('/');await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();await page.getByRole('textbox', { name: '输入消息' }).fill('咨询政策');await page.getByRole('button', { name: '发送消息' }).click();await page.getByRole('button', { name: '[1] 核对引用正文' }).click();await expect(page.locator('.citation-preview strong')).toBeVisible();
  await expect.poll(() => historyCalls).toBe(1);await expect(page.locator('article.message[data-message-id="server-answer"]')).toHaveCount(0);await page.waitForResponse('**/api/history/stable-session*');await expect(page.getByRole('button', { name: '[1] 收起引用正文' })).toHaveAttribute('aria-expanded', 'true');
  await page.locator('.workflow-detail summary').click();await expect(page.locator('.workflow-detail details')).toHaveCount(0);await expect(page.locator('details.workflow-detail li')).toHaveCount(3);
});

test('ticket and knowledge workbenches fit desktop tablet and mobile with usable navigation', async ({ page }) => {
  await knowledge(page);
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });await page.goto('/#/tickets');await page.getByRole('button', { name: /TK-PRODUCT-21/ }).click();await expect(page.getByRole('region', { name: '工单详情' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);await page.screenshot({ path: test.info().outputPath(`tickets-${width}.png`), fullPage: true });
    if (width <= 800) { await page.getByRole('button', { name: '打开会话列表', exact: true }).click();await page.getByRole('button', { name: '知识管理', exact: true }).click(); } else await page.getByRole('button', { name: '知识管理', exact: true }).click();
    await page.getByRole('tab', { name: '上传文件' }).click();await expect(page.getByRole('heading', { name: '上传知识原文件' })).toBeVisible();expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);await page.screenshot({ path: test.info().outputPath(`knowledge-${width}.png`), fullPage: true });
    if (width === 1440) {
      const styles = await page.locator('link[rel="stylesheet"]').evaluateAll(async links => (await Promise.all(links.map(link => fetch((link as HTMLLinkElement).href).then(r => r.text())))).join('\n'));
      const html = (await page.content()).replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '').replace(/<link[^>]*>/gi, '').replace('</head>', `<style>${styles}</style></head>`);
      await mkdir('../.local/product/visual', { recursive: true });await writeFile('../.local/product/visual/index.html', html, 'utf8');
    }
  }
});

test('ticket status badges distinguish states and filters clear the previous detail', async ({ page }) => {
  await setup(page);
  const closed = { ...ticket, ticket_id: 'TK-CLOSED', status: 'closed', can_assign: false, allowed_transitions: [] };
  const resolved = { ...ticket, ticket_id: 'TK-RESOLVED', status: 'resolved', can_assign: false };
  const allTickets = [ticket, resolved, closed];
  let failNext = false;
  await page.route('**/api/tickets?*', route => {
    const status = new URL(route.request().url()).searchParams.get('status');
    if (failNext) {
      failNext = false;
      return route.fulfill({ status: 503, json: { detail: '工单暂时无法读取。' } });
    }
    const rows = allTickets.filter(row => !status || row.status === status);
    return route.fulfill({ json: { tickets: rows, total: rows.length, has_more: false } });
  });
  await page.route('**/api/tickets/TK-CLOSED', route => route.fulfill({ json: { ticket: closed } }));
  await page.route('**/api/tickets/TK-CLOSED/events?*', route => route.fulfill({ json: {
    events: [{ id: 'closed-event', to_status: 'closed', note: '工单已归档', created_at: closed.updated_at }], has_more: false,
  } }));
  await page.goto('/#/tickets');
  const list = page.getByRole('region', { name: '我的工单列表' });
  const badges = list.locator('.ticket-status');
  await expect(badges).toHaveText(['待处理', '已解决', '已关闭']);
  const styles = await badges.evaluateAll(elements => elements.map(element => ({
    background: getComputedStyle(element).backgroundColor,
    icon: element.querySelector('svg')?.innerHTML,
  })));
  expect(new Set(styles.map(style => style.background)).size).toBe(3);
  expect(new Set(styles.map(style => style.icon)).size).toBe(3);
  await list.getByRole('button', { name: /TK-PRODUCT-21/ }).click();
  const detail = page.getByRole('region', { name: '工单详情' });
  await expect(detail.locator('.ticket-status[data-status="created"]').first()).toBeVisible();

  const filters = page.getByRole('group', { name: '筛选工单状态' });
  await filters.getByRole('button', { name: '已关闭', exact: true }).click();
  await expect(filters.getByRole('button', { name: '已关闭', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(badges).toHaveText(['已关闭']);
  await expect(detail).toHaveCount(0);
  await list.getByRole('button', { name: /TK-CLOSED/ }).click();
  await expect(detail.locator('.ticket-status[data-status="closed"]')).toHaveCount(2);
  await expect(detail.getByRole('button', { name: '确认分派' })).toHaveCount(0);

  await filters.getByRole('button', { name: '待复核' }).click();
  await expect(list.getByText('暂无「待复核」的工单。')).toBeVisible();
  await list.getByRole('button', { name: '查看全部工单' }).click();
  await expect(badges).toHaveCount(3);
  failNext = true;
  await page.getByRole('button', { name: '刷新列表' }).click();
  await expect(list.getByRole('alert')).toContainText('工单暂时无法读取');
  await expect(list.getByText('暂无工单。')).toHaveCount(0);
  await list.getByRole('button', { name: '重试读取工单' }).click();
  await expect(badges).toHaveCount(3);
});

test('mobile ticket details return to the same filtered item', async ({ page }) => {
  await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/#/tickets');
  const filters = page.getByRole('group', { name: '筛选工单状态' });
  await filters.getByRole('button', { name: '待处理', exact: true }).click();
  const row = page.getByRole('button', { name: /TK-PRODUCT-21/ });
  await row.focus();
  await row.press('Enter');
  const detail = page.getByRole('region', { name: '工单详情' });
  await expect(detail.getByRole('heading', { name: '工单详情' })).toBeFocused();
  await expect(row).toHaveCount(0);
  await detail.getByRole('button', { name: '返回工单列表' }).click();
  await expect(row).toBeFocused();
  await expect(filters.getByRole('button', { name: '待处理', exact: true })).toHaveAttribute('aria-pressed', 'true');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
