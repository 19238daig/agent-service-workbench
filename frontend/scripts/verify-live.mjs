import { chromium } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const reportDir = path.join(root, '.local');
await mkdir(reportDir, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const report = { passed: false, browser_errors: [], checks: [] };
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on('pageerror', error => report.browser_errors.push(error.message));
  await page.goto('http://127.0.0.1:8000/');
  await page.getByText('服务已连接', { exact: true }).waitFor();
  const profile = await page.request.get('http://127.0.0.1:8000/api/workflow').then(r => r.json());
  if (!profile.ready || profile.nodes.filter(n => n.role).length !== 4) throw new Error('Workflow profile is unavailable');
  await page.screenshot({ path: path.join(reportDir, 'frontend-live-welcome.png'), fullPage: true });
  const questions = [
    ['knowledge_rag', '购买后第3天能否无理由退款，退款多久能到账？'],
    ['ticket_handler', '请帮我创建一张退款申请工单，订单号 DEMO-20261113，原因是误购。'],
    ['ticket_handler', '刚才那张工单现在是什么状态？'],
    ['compliance_checker', '我怀疑遇到诈骗，账户里出现了不是我本人操作的扣款，现在该怎么办？'],
  ];
  let ticketId;
  for (const [index, [route, question]] of questions.entries()) {
    await page.getByRole('textbox', { name: '输入消息' }).fill(question);
    const responseReady = page.waitForResponse(r => r.url().endsWith('/api/chat/stream') && r.request().method() === 'POST', { timeout: 240000 });
    await page.getByRole('button', { name: '发送消息' }).click();
    const response = await responseReady;
    if (!response.ok()) throw new Error('Stream HTTP=' + response.status());
    if (index === 0) {
      await page.locator('.workflow-panel [data-node="intent_router"][data-status="running"]').waitFor({ timeout: 10000 });
      await page.screenshot({ path: path.join(reportDir, 'frontend-live-running.png'), fullPage: true });
    }
    const reply = page.getByRole('article', { name: '客服回复' }).nth(index);
    await reply.locator('.markdown-body, .message-error').waitFor({ timeout: 240000 });
    if (await reply.locator('.message-error').count()) {
      report.failed_reply = await reply.locator('.message-error').innerText();
      report.failed_trace = await page.evaluate(() => {
        const data = JSON.parse(localStorage.getItem('smart-cs.workspace.v1') || '{}');
        return data.conversations?.find(c => c.id === data.activeId)?.messages?.at(-1)?.trace;
      });
      await page.screenshot({ path: path.join(reportDir, 'frontend-live-error.png'), fullPage: true });
      throw new Error('The browser reported an interrupted workflow');
    }
    const events = (await response.text()).split('\n').filter(line => line.startsWith('data: ')).map(line => JSON.parse(line.slice(6)));
    const data = events.at(-1);
    await writeFile(path.join(reportDir, `frontend-live-response-${index}.json`), JSON.stringify({ events }, null, 2), 'utf8');
    if (data?.type !== 'complete' || data.intent !== route || !data.compliance_passed) throw new Error(`Unexpected result in route ${index}: ${data?.type}/${data?.intent}`);
    const expected = ['intent_router', 'supervisor_route', ...(['knowledge_rag', 'ticket_handler'].includes(route) ? [route] : []), 'compliance_check', 'synthesize'];
    if (JSON.stringify(data.agent_steps) !== JSON.stringify(expected)) throw new Error('Unexpected actual path');
    for (const step of data.execution_trace) {
      if (step.duration_ms < 0) throw new Error('Invalid node duration');
      const node = profile.nodes.find(n => n.id === step.node);
      if (node?.role && node.model !== step.model) throw new Error('Model binding mismatch');
    }
    const ticket = data.execution_trace.find(s => s.node === 'ticket_handler')?.summary;
    if (index === 1) {
      if (ticket?.action !== 'create' || !ticket.found) throw new Error('Ticket was not actually created');
      ticketId = ticket.ticket_id;
    }
    if (index === 2 && (ticket?.action !== 'query' || ticket.ticket_id !== ticketId || !ticket.found)) throw new Error('Contextual ticket query failed');
    report.checks.push({ route, agent_steps: data.agent_steps, agent_models: data.agent_models, execution_trace: data.execution_trace, duration_ms: data.duration_ms, compliance_passed: data.compliance_passed });
    await page.screenshot({ path: path.join(reportDir, `frontend-live-route-${index}.png`), fullPage: true });
    console.log(JSON.stringify({ route, passed: true, steps: data.agent_steps, duration_ms: data.duration_ms }));
  }
  await page.reload();
  await page.getByRole('article', { name: '客服回复' }).nth(3).locator('.markdown-body').waitFor();
  await page.getByRole('button', { name: /查看本轮协作/ }).first().click();
  await page.locator('.workflow-panel [data-node="knowledge_rag"][data-status="done"]').waitFor();
  await page.locator('.workflow-panel [data-node="ticket_handler"][data-status="skipped"]').waitFor();
  report.history_restored = true;
  report.historical_trace_selected = true;
  await page.screenshot({ path: path.join(reportDir, 'frontend-live-conversation.png'), fullPage: true });
  for (const width of [1086, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole('button', { name: '协作过程', exact: true }).click();
    await page.getByRole('dialog', { name: '协作过程' }).waitFor();
    if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))) throw new Error('Horizontal overflow');
    await page.screenshot({ path: path.join(reportDir, `frontend-live-workflow-${width}.png`), fullPage: true });
    await page.keyboard.press('Escape');
  }
  report.passed = report.browser_errors.length === 0;
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
} finally {
  await writeFile(path.join(reportDir, 'frontend-live-verification.json'), JSON.stringify(report, null, 2), 'utf8');
  console.log(JSON.stringify({ passed: report.passed, checked_routes: report.checks.length, error: report.error, browser_errors: report.browser_errors }));
  await browser.close();
}
