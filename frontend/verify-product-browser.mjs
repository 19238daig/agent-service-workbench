/** Real local UI acceptance. Credentials remain in memory; no traces or network logs. */
import { chromium } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';

const output = '../.local/product';await mkdir(output, { recursive: true });
const accounts = JSON.parse(await readFile('../.local/auth/bootstrap-accounts.json', 'utf8')).accounts;
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const stage = process.argv[2] || 'tickets';const report = { passed: false, stage, real_api: true, real_browser: true, real_model: stage === 'tickets', runs: [] };
const actors = [];const stamp = randomUUID().slice(0, 8);
function progress(message) { console.log(JSON.stringify({ stage, message })); }
async function login(name) {
  const account = accounts.find(x => x.login_name === name);if (!account) throw new Error('Missing local account');
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:8000', viewport: { width: 1440, height: 1000 } });
  // Start on a new draft; do not open or expire historical business requests.
  await context.addInitScript(({ id, key }) => { if (localStorage.getItem('product-qa-initialized')) return;localStorage.setItem('product-qa-initialized', '1');const c = { id: key, title: '第21步页面验收', createdAt: Date.now(), updatedAt: Date.now(), messages: [], draft: '第21步页面验收' };localStorage.setItem(`smart-cs.workspace.v2:${id}`, JSON.stringify({ userId: id, activeId: key, conversations: [c] })); }, { id: account.user_id, key: randomUUID() });
  const page = await context.newPage();page.setDefaultTimeout(30000);await page.goto('/');await page.getByLabel('账号', { exact: true }).fill(name);await page.getByLabel('密码', { exact: true }).fill(account.password);
  const response = page.waitForResponse('**/api/auth/login');await page.getByRole('button', { name: '登录工作台' }).click();const value = await (await response).json();if (!value.access_token) throw new Error('Local login failed');
  const actor = { page, context, headers: { Authorization: `Bearer ${value.access_token}` }, user: value.user };actors.push(actor);page.on('response', async response => { if (new URL(response.url()).pathname === '/api/auth/refresh' && response.ok()) { const refreshed = await response.json().catch(() => null);if (refreshed?.access_token) actor.headers.Authorization = `Bearer ${refreshed.access_token}`; } });await page.getByText('服务已连接', { exact: true }).waitFor();return actor;
}
async function get(actor, path) { const r = await actor.page.request.get(path, { headers: actor.headers });if (!r.ok()) throw new Error(`GET ${path.split('?')[0]} ${r.status()}`);return r.json(); }
async function runAfter(actor, callback, resume = false) {
  const submission = actor.page.waitForResponse(r => r.request().method() === 'POST' && (resume ? /\/runs\/resume$/.test(new URL(r.url()).pathname) : new URL(r.url()).pathname === '/api/runs'), { timeout: 30000 });
  await callback();const response = await submission;if (!response.ok()) throw new Error(`Run submission ${response.status()}`);const descriptor = await response.json();report.runs.push(descriptor.run_id);
  const start = Date.now();let row;
  while (Date.now() - start < 210000) { row = await get(actor, `/api/runs/${descriptor.run_id}`);if (!['queued', 'running', 'cancel_requested'].includes(row.run_status || row.status)) break;await new Promise(resolve => setTimeout(resolve, 1000)); }
  const event = row?.result_event;if (event?.type !== 'complete') throw new Error(`Run failed: ${row?.error_code || row?.run_status || row?.status}`);
  await actor.page.getByRole('button', { name: '发送消息' }).isEnabled();progress(`Completed ${descriptor.run_id}`);return event;
}
async function send(actor, text) {
  await actor.page.locator('.new-conversation').click();await actor.page.getByRole('textbox', { name: '输入消息' }).fill(text);
  return runAfter(actor, () => actor.page.getByRole('button', { name: '发送消息' }).click());
}
async function approve(staff, identifier) {
  const page = staff.page;await page.getByRole('button', { name: '客服工作台', exact: true }).click();await page.getByRole('tab', { name: '待审批申请' }).click();
  // Locate only the newly created QA request; never open historical requests.
  const row = await get(staff, `/api/staff/action-requests/${identifier}`);const needle = String(row.parameters.summary || row.parameters.ticket_id || '查看申请内容');
  const list = page.locator('.desk-list button').filter({ hasText: needle });await list.first().click();const card = page.getByRole('region', { name: '人工审批详情' });await card.getByRole('checkbox').check();await card.getByLabel('审批说明').fill('第21步浏览器验收，已核对新建QA申请及当前状态。');await card.getByRole('button', { name: '批准申请' }).click();await card.getByText('审批已记录，申请人可继续办理。').waitFor();progress('Independent approval recorded');
}
async function confirmAndContinue(owner, event, reviewer) {
  if (!event.action_request?.action_id) throw new Error('Expected reviewed action draft');
  const page = owner.page;const card = page.getByRole('region', { name: '申请确认', exact: true });await card.getByRole('checkbox').check();await card.getByRole('button', { name: '确认申请', exact: true }).click();
  let current = await get(owner, `/api/action-requests/${event.action_request.action_id}`);
  if (current.status === 'awaiting_staff') { await approve(reviewer, current.action_id);await card.getByRole('button', { name: '刷新申请状态' }).click(); }
  await card.getByRole('button', { name: '继续办理', exact: true }).waitFor();return runAfter(owner, () => card.getByRole('button', { name: '继续办理', exact: true }).click(), true);
}
async function ticketFlow() {
  const customer = await login('customer-a');const support = await login('support');const admin = await login('admin');
  let consult;
  if (process.env.PRODUCT_REUSE_CONSULT_RUN) {
    const original = await get(customer, `/api/runs/${process.env.PRODUCT_REUSE_CONSULT_RUN}`);consult = original.result_event;report.reused_consult_run = process.env.PRODUCT_REUSE_CONSULT_RUN;
    await customer.page.evaluate(({ user, session }) => { const id = crypto.randomUUID();const c = { id, sessionId: session, title: '第21步咨询验收', createdAt: Date.now(), updatedAt: Date.now(), messages: [], draft: '' };localStorage.setItem(`smart-cs.workspace.v2:${user}`, JSON.stringify({ userId: user, activeId: id, conversations: [c] })); }, { user: customer.user.id, session: consult.session_id });
    await customer.page.reload();await customer.page.getByRole('button', { name: /核对引用正文/ }).first().waitFor();
  } else consult = await send(customer, '【第21步页面验收】请先依据公开资料说明哪些商品不适用七日无理由退货，然后仅查询订单 ORDER-A-001 的当前支付金额和状态。不要登记申请或修改订单、工单。');
  if (!consult.compliance_passed || !consult.citations?.length || consult.action_request?.action_id) throw new Error('Consultation/order flow failed');
  report.consultation = { session_id: consult.session_id, citations: consult.citations.length, task_status: consult.task_plan?.status };
  const citation = customer.page.getByRole('button', { name: /核对引用正文/ }).first();await citation.click();await customer.page.locator('.citation-preview strong').first().waitFor();await customer.page.screenshot({ path: `${output}/live-consultation.png`, fullPage: true });
  let id;
  if (process.env.PRODUCT_REUSE_TICKET) { id = process.env.PRODUCT_REUSE_TICKET;report.ticket_id = id;report.reused_ticket = true;const existing = await get(customer, `/api/tickets/${id}`);if (!existing.ticket.summary.startsWith('第21步页面闭环验收-')) throw new Error('Refuse non-QA ticket'); } else {
  const draft = await send(customer, `请为我登记一张普通服务咨询工单（general），优先级 medium。摘要：第21步页面闭环验收-${stamp}。问题描述：这是本地演示的页面验收申请，请客服核实工作台办理流程。不涉及订单、退款或资金操作。请生成待本人确认的工单草稿。`);
  report.action_id = draft.action_request?.action_id;
  const executed = await confirmAndContinue(customer, draft, support);id = executed.action_execution?.ticket_id;if (!id) throw new Error('Missing committed ticket receipt');report.ticket_id = id;progress(`Created ${id}`);
  await customer.page.getByRole('button', { name: `${id} · 查看工单` }).click();const detail = customer.page.getByRole('region', { name: '工单详情' });await detail.getByLabel('分派给').selectOption(support.user.id);await detail.getByLabel('分派说明').fill('第21步QA工单，请处理并记录结论。');await detail.getByRole('button', { name: '确认分派' }).click();await detail.getByText('分派已更新。').waitFor();
  }
  for (const status of ['processing', 'resolved', 'closed']) {
    const currentTicket = (await get(support, `/api/tickets/${id}`)).ticket;if (currentTicket.status === status || status === 'processing' && currentTicket.status !== 'created' || status === 'resolved' && currentTicket.status === 'closed') continue;
    const page = support.page;await page.getByRole('button', { name: '客服工作台', exact: true }).click();await page.getByRole('tab', { name: '待我处理' }).click();await page.getByRole('button', { name: '刷新列表', exact: true }).click();await page.locator('.desk-list button').filter({ hasText: id }).click();const ticketDetail = page.getByRole('region', { name: '工单详情' });await ticketDetail.getByLabel('目标状态').selectOption(status);await ticketDetail.getByLabel('处理原因').fill(status === 'processing' ? '已接收QA工单，正在核实页面闭环。' : '已验证确认、分派、独立审批和工单事件记录，问题已解决。');await ticketDetail.getByRole('button', { name: '生成处理草稿' }).click();
    let event = await runAfter(support, () => page.getByRole('button', { name: '发送消息' }).click());if (!event.action_request?.action_id) { report.clarifications = [...(report.clarifications || []), { run_id: event.run_id, response: event.response, task_plan: event.task_plan }];const state = await get(support, `/api/tickets/${id}`);await page.getByRole('textbox', { name: '输入消息' }).fill(`我明确申请办理工单 ${id} 的状态变更，将状态改为 ${status}，工单当前版本是 ${state.ticket.version}，处理原因为：第21步页面闭环验收处理完成。请生成状态变更草稿。`);event = await runAfter(support, () => page.getByRole('button', { name: '发送消息' }).click()); }const result = await confirmAndContinue(support, event, admin);if (result.action_execution?.ticket_status !== status) throw new Error(`Transition receipt differs: ${status}`);progress(`Ticket ${status}`);
  }
  // Assigned support closes this QA ticket with independent approval; the owner queries the actual result.
  await customer.page.getByRole('button', { name: '我的工单', exact: true }).click();await customer.page.getByRole('button', { name: '刷新列表', exact: true }).click();await customer.page.locator('.desk-list button').filter({ hasText: id }).click();await customer.page.getByRole('button', { name: '刷新工单详情' }).click();const final = await get(customer, `/api/tickets/${id}`);if (final.ticket.status !== 'closed') throw new Error('QA ticket not closed');
  const events = await get(customer, `/api/tickets/${id}/events?limit=50`);report.ticket_status = final.ticket.status;report.ticket_events = events.events.map(e => ({ from: e.from_status, to: e.to_status, role: e.actor_role }));
  await customer.page.screenshot({ path: `${output}/live-ticket-closed.png`, fullPage: true });await support.page.screenshot({ path: `${output}/live-support.png`, fullPage: true });
  // Logout clears private account data and returns to login.
  await customer.page.getByRole('button', { name: '退出登录' }).click();await customer.page.getByRole('heading', { name: '登录工作台' }).waitFor();if (await customer.page.locator('.ticket-detail').count()) throw new Error('Ticket cache remains after logout');report.logout_clears_view = true;
}
async function knowledgeFlow() {
  const admin = await login('admin');const page = admin.page;
  const previous = JSON.parse(await readFile(`${output}/live-knowledge.json`, 'utf8').catch(() => '{}'));
  if (!previous.passed && previous.staged_index_id && previous.document_id) {
    Object.assign(report, previous, { passed: false });delete report.error;
    await page.getByRole('button', { name: '知识管理', exact: true }).click();
    const original = await get(admin, `/api/admin/knowledge/documents/${report.document_id}`);
    await page.locator('.desk-list button').filter({ hasText: original.title }).click();
    const detail = page.getByRole('region', { name: '知识文档详情' });
    await detail.getByRole('checkbox').check();await detail.getByRole('button', { name: '确认撤回文档' }).click();await detail.getByText('文档已撤回，后续检索会排除此版本。').waitFor();report.qa_document_withdrawn = true;
    const audits = await get(admin, '/api/admin/knowledge/events');report.audit_kinds = audits.events.filter(e => e.resource_id === report.document_id).map(e => e.kind);await page.screenshot({ path: `${output}/live-knowledge-withdrawn.png`, fullPage: true });return;
  }
  const manifest = JSON.parse(await readFile('../data/public-after-sales-v2/source_manifest.json', 'utf8'));const source = manifest.sources.find(s => s.file === 'consumer_protection_regulation.html');if (!source) throw new Error('Missing authentic source');
  await page.getByRole('button', { name: '知识管理', exact: true }).click();await page.getByRole('tab', { name: '上传文件' }).click();await page.getByLabel('知识原文件').setInputFiles(`../data/public-after-sales-v2/sources/${source.file}`);await page.getByLabel('文档标题', { exact: true }).fill(`第21步原文入库验收-${stamp}`);await page.getByLabel('来源标识', { exact: false }).fill(`product-21-${stamp}`);await page.getByLabel('版本', { exact: true }).fill('step21-v1');await page.getByLabel('发布者', { exact: true }).fill(source.publisher);await page.getByLabel('使用许可', { exact: true }).fill(source.license);await page.getByLabel('生效日期', { exact: true }).fill(source.effective_from);await page.getByLabel('原始来源网址（可选）').fill(source.url);
  const submission = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/admin/jobs');await page.getByRole('button', { name: '上传并后台入库' }).click();const r = await submission;if (!r.ok()) throw new Error(`Ingestion submission ${r.status()}`);const job = await r.json();report.job_id = job.id;
  let done;for (let i = 0; i < 180; i++) { done = await get(admin, `/api/admin/jobs/${job.id}`);if (['succeeded', 'failed', 'cancelled'].includes(done.status)) break;await new Promise(resolve => setTimeout(resolve, 1000)); }
  if (done.status !== 'succeeded' || !done.result?.document_id) throw new Error(`Ingestion ${done.status}:${done.error_code || ''}`);report.document_id = done.result.document_id;report.chunks = done.result.chunks;progress('Authentic HTML parsed by background worker');
  await page.getByRole('button', { name: '刷新后台任务' }).click();const row = page.locator('.job-row').filter({ hasText: new Date(job.created_at).toLocaleString('zh-CN') });if (await row.count()) await row.first().getByRole('button', { name: '核对入库文档' }).click();else await page.locator('.job-row').first().getByRole('button', { name: '核对入库文档' }).click();
  const detail = page.getByRole('region', { name: '知识文档详情' });await detail.locator('.chunk-preview').first().waitFor();await detail.getByRole('checkbox').check();await detail.getByRole('button', { name: '确认发布文档' }).click();await detail.getByText('文档已发布。构建并校验索引后，可启用新版本。').waitFor();report.published_via_ui = true;
  await page.screenshot({ path: `${output}/live-knowledge-published.png`, fullPage: true });
  // Build a staged index using local BGE-M3, without changing the established live index.
  await page.getByRole('tab', { name: '索引与记录' }).click();const before = await get(admin, '/api/admin/knowledge/indexes');report.active_before = before.active_index_id;
  const indexSubmission = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/admin/jobs');await page.getByRole('button', { name: '后台构建新索引' }).click();const indexJob = await (await indexSubmission).json();report.index_job_id = indexJob.id;
  for (let i = 0; i < 420; i++) { done = await get(admin, `/api/admin/jobs/${indexJob.id}`);if (['succeeded', 'failed', 'cancelled'].includes(done.status)) break;if (i % 30 === 0) progress(`Index ${done.stage} ${done.progress}/${done.total}`);await new Promise(resolve => setTimeout(resolve, 1000)); }
  if (done.status !== 'succeeded') throw new Error(`Index build ${done.status}:${done.error_code || ''}`);report.staged_index_id = done.result.index_id;report.active_after = (await get(admin, '/api/admin/knowledge/indexes')).active_index_id;if (report.active_after !== report.active_before) throw new Error('Live index unexpectedly switched');
  await page.getByRole('tab', { name: '知识文档', exact: true }).click();await page.locator('.desk-list button').filter({ hasText: `第21步原文入库验收-${stamp}` }).click();await detail.getByRole('checkbox').check();await detail.getByRole('button', { name: '确认撤回文档' }).click();await detail.getByText('文档已撤回，后续检索会排除此版本。').waitFor();report.qa_document_withdrawn = true;
  const audits = await get(admin, '/api/admin/knowledge/events');report.audit_kinds = audits.events.filter(e => e.resource_id === report.document_id).map(e => e.kind);await page.screenshot({ path: `${output}/live-knowledge-withdrawn.png`, fullPage: true });
}
try {
  let healthy = false;for (let i = 0; i < 90; i++) { try { const r = await fetch('http://127.0.0.1:8000/health', { signal: AbortSignal.timeout(2000) });if (r.ok) { healthy = true;break; } } catch {}await new Promise(resolve => setTimeout(resolve, 1000)); }if (!healthy) throw new Error('Local API not ready');
  if (stage === 'tickets') await ticketFlow();else if (stage === 'knowledge') await knowledgeFlow();else throw new Error('Unknown stage');report.passed = true;
} catch (error) {
  report.error = error.message;for (let i = 0; i < actors.length; i++) await actors[i].page.screenshot({ path: `${output}/failed-${stage}-${i}.png`, fullPage: true }).catch(() => {});
} finally {
  for (const actor of actors) { await actor.page.request.post('/api/auth/logout', { headers: actor.headers }).catch(() => {});await actor.context.close(); }
  await browser.close();await writeFile(`${output}/live-${stage}.json`, JSON.stringify(report, null, 2) + '\n');await mkdir(`${output}/attempts`, { recursive: true });await writeFile(`${output}/attempts/${stage}-${Date.now()}.json`, JSON.stringify(report, null, 2) + '\n');console.log(JSON.stringify(report));
}
if (!report.passed) process.exitCode = 1;
