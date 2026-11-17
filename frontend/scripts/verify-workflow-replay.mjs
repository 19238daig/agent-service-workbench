/** Check the UI against saved real executions, without making more model calls. */
import { chromium } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = path.join(root, '.local');
const knowledge = JSON.parse(await readFile(path.join(output, 'frontend-live-response-0.json'), 'utf8')).events.at(-1);
const tickets = JSON.parse(await readFile(path.join(output, 'workflow-live-ticket.json'), 'utf8'));
if (knowledge.type !== 'complete' || !knowledge.compliance_passed || !tickets.passed || tickets.checks.length !== 3) throw new Error('Successful real execution evidence is required');
const results = [knowledge, ...tickets.checks];
const questions = ['购买后第3天能否无理由退款，退款多久能到账？', '请帮我创建一张退款申请工单，原因是误购。', '刚才那张工单现在是什么状态？', '我怀疑遇到诈骗，账户里出现了不是我本人操作的扣款，现在该怎么办？'];
const messages = (start, end) => results.slice(start, end).flatMap((result, offset) => {
  const i = start + offset;
  return [{ id: `user-${i}`, role: 'user', content: questions[i] },
    { id: `reply-${i}`, role: 'assistant', content: result.response, replyTo: `user-${i}`, status: 'done', intent: result.intent, reviewed: result.compliance_passed,
      trace: { status: 'done', steps: result.execution_trace, duration_ms: result.duration_ms } }];
});
const seed = { userId: 'workflow-evidence-replay', activeId: 'knowledge', conversations: [
  { id: 'knowledge', title: '知识路径验证', sessionId: knowledge.session_id, createdAt: 1, updatedAt: 1, draft: '', messages: messages(0, 1) },
  { id: 'tickets', title: '工单与安全路径验证', sessionId: tickets.checks[0].session_id, createdAt: 2, updatedAt: 2, draft: '', messages: messages(1, 4) },
] };
const report = { mode: 'real_backend_executions_and_browser_replay', passed: false, live_browser_knowledge: true, live_backend_routes: results.map(r => r.intent), checks: [], browser_errors: [] };
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.on('pageerror', error => report.browser_errors.push(error.message));
  await page.goto('http://127.0.0.1:8000/');
  await page.evaluate(value => localStorage.setItem('smart-cs.workspace.v1', JSON.stringify(value)), seed);
  await page.reload();
  const panel = page.getByRole('region', { name: 'Agent 协作过程' });
  for (const [index, result] of results.entries()) {
    if (index === 1) await page.getByRole('button', { name: '工单与安全路径验证', exact: true }).click();
    await page.getByRole('button', { name: /查看本轮协作/ }).nth(index === 0 ? 0 : index - 1).click();
    for (const node of result.agent_steps) {
      const step = panel.locator(`[data-node="${node}"]`);
      await step.waitFor();
      if (await step.getAttribute('data-status') !== 'done') throw new Error(`Incorrect state: ${index}/${node}`);
    }
    for (const node of ['knowledge_rag', 'ticket_handler']) {
      if (!result.agent_steps.includes(node) && await panel.locator(`[data-node="${node}"]`).getAttribute('data-status') !== 'skipped') throw new Error('An unused branch was shown as executed');
    }
    const ticket = result.execution_trace.find(s => s.node === 'ticket_handler')?.summary;
    if (ticket?.ticket_id) await panel.getByText(ticket.ticket_id, { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, `workflow-real-result-${index}.png`), fullPage: true });
    report.checks.push({ intent: result.intent, agent_models: result.agent_models, agent_steps: result.agent_steps, duration_ms: result.duration_ms, replay_matched_actual_events: true });
  }
  await page.reload();
  await page.getByRole('button', { name: /查看本轮协作/ }).first().click();
  await panel.locator('[data-node="ticket_handler"][data-status="done"]').waitFor();
  report.history_restored = true;
  for (const width of [1086, 768, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole('button', { name: '协作过程', exact: true }).click();
    await page.getByRole('dialog', { name: '协作过程' }).waitFor();
    if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))) throw new Error('Horizontal overflow');
    await panel.getByText(tickets.checks[0].execution_trace[2].summary.ticket_id, { exact: true }).waitFor();
    await page.screenshot({ path: path.join(output, `workflow-real-result-${width}.png`), fullPage: true });
    await page.keyboard.press('Escape');
  }
  report.passed = report.browser_errors.length === 0;
} catch (error) {
  report.error = error.message;
  process.exitCode = 1;
} finally {
  await writeFile(path.join(output, 'workflow-verification.json'), JSON.stringify(report, null, 2), 'utf8');
  console.log(JSON.stringify(report));
  await browser.close();
}
