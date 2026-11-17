import { chromium } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';

const output = '../.local/conversations';
await mkdir(output, { recursive: true });
const pending = process.argv.includes('--pending');
const report = { passed: false, real_api: true, real_models_called_by_browser: false, browser: 'Edge', pending };
const accounts = JSON.parse(await readFile('../.local/auth/bootstrap-accounts.json', 'utf8')).accounts;
const state = JSON.parse(await readFile(`${output}/live-case.json`, 'utf8'));
const browser = await chromium.launch({ channel: 'msedge', headless: true });
try {
  const page = await browser.newPage({ baseURL: 'http://127.0.0.1:8000', viewport: { width: 1440, height: 900 } });
  let modelRequests = 0;
  page.on('request', r => { if (r.method() === 'POST' && (/\/api\/chat/.test(r.url()) || /\/resume$/.test(r.url()))) modelRequests++; });
  await page.goto('/');
  const account = accounts.find(a => a.login_name === 'customer-a');
  await page.getByLabel('账号', { exact: true }).fill(account.login_name);
  await page.getByLabel('密码', { exact: true }).fill(account.password);
  await page.getByRole('button', { name: '登录工作台' }).click();
  await page.getByRole('log').getByText('【第十步中断恢复验收】请只查询工单 TK-20261114-9A0001 的处理状态，不要创建或修改工单。', { exact: true }).waitFor();
  if (pending) await page.getByRole('button', { name: '恢复上轮处理', exact: true }).waitFor();
  else await page.getByRole('article', { name: '客服回复' }).last().waitFor();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Layout overflow');
    await page.screenshot({ path: `${output}/${pending ? 'pending' : 'restored'}-${width}.png`, fullPage: true });
  }
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await page.getByRole('log').getByText('【第十步中断恢复验收】请只查询工单 TK-20261114-9A0001 的处理状态，不要创建或修改工单。', { exact: true }).waitFor();
  if (modelRequests !== 0) throw new Error('Viewing history executed a model');
  report.empty_browser_storage_restores_history = true;
  report.no_automatic_replay = true;
  report.session_id = state.session_id;
  report.passed = true;
} catch (error) { report.error_type = error instanceof Error ? error.name : 'UnknownError'; }
finally { await browser.close(); }
await writeFile(`${output}/live-browser-${pending ? 'pending' : 'restored'}.json`, JSON.stringify(report, null, 2));
console.log(JSON.stringify(report));
process.exitCode = report.passed ? 0 : 1;
