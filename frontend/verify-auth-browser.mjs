import { chromium } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';

const output = '../.local/auth';
await mkdir(output, { recursive: true });
const accounts = JSON.parse(await readFile(`${output}/bootstrap-accounts.json`, 'utf8')).accounts;
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const report = { passed: false, real_api: true, real_model: true, browser: 'Edge' };
try {
  const page = await browser.newPage({ baseURL: 'http://127.0.0.1:8000', viewport: { width: 1440, height: 900 } });
  await page.goto('/');
  await page.getByRole('heading', { name: '登录工作台' }).waitFor();
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Login overflow');
    await page.screenshot({ path: `${output}/login-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  async function login(name) {
    const account = accounts.find(x => x.login_name === name);
    await page.getByLabel('账号', { exact: true }).fill(account.login_name);
    await page.getByLabel('密码', { exact: true }).fill(account.password);
    const response = page.waitForResponse('**/api/auth/login');
    await page.getByRole('button', { name: '登录工作台' }).click();
    const result = await (await response).json();
    if (!result.access_token) throw new Error('Login failed');
    await page.getByRole('textbox', { name: '输入消息' }).waitFor();
    return { Authorization: `Bearer ${result.access_token}` };
  }
  const alice = await login('customer-a');
  await page.getByText('服务已连接', { exact: true }).waitFor();
  await page.getByRole('textbox', { name: '输入消息' }).fill('请依据公开的消费者权益保护法实施条例，说明自动续费应在何时提醒消费者。');
  const stream = page.waitForResponse('**/api/chat/stream');
  await page.getByRole('button', { name: '发送消息' }).click();
  const response = await stream;
  const events = (await response.text()).split(/\r?\n/).filter(x => x.startsWith('data: ')).map(x => JSON.parse(x.slice(6)));
  const final = events.find(x => x.type === 'complete');
  if (!final?.compliance_passed || !final.citations?.length) throw new Error('Missing reviewed RAG answer');
  const run = await page.request.get(`/api/runs/${final.run_id}`, { headers: alice });
  if (run.status() !== 200 || !(await run.json()).identity.authenticated) throw new Error('Run ownership failed');
  const file = await page.request.get(final.citations[0].url.split('#')[0], { headers: alice });
  if (file.status() !== 200) throw new Error('Authenticated source access failed');
  await page.locator('.source-citations button').first().waitFor();
  await page.screenshot({ path: `${output}/authenticated-chat-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Chat overflow');
  await page.screenshot({ path: `${output}/authenticated-chat-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '退出登录' }).click();
  await page.getByRole('heading', { name: '登录工作台' }).waitFor();
  const bob = await login('customer-b');
  for (const path of [`/api/history/${final.session_id}`, `/api/runs/${final.run_id}?user_id=${accounts.find(x => x.login_name === 'customer-a').user_id}`]) {
    if ((await page.request.get(path, { headers: bob })).status() !== 404) throw new Error('Cross-account read succeeded');
  }
  if ((await page.request.post('/api/chat/stream', { headers: bob, data: { message: 'test', session_id: final.session_id } })).status() !== 404) throw new Error('Cross-account thread accepted');
  if ((await page.request.get('/api/auth/me', { headers: alice })).status() !== 401) throw new Error('Logged-out token accepted');
  if (await page.getByText(final.response, { exact: true }).count()) throw new Error('Previous account answer visible');
  await page.getByRole('button', { name: '退出登录' }).click();
  report.passed = true;
  report.run_id = final.run_id;
  report.nodes = final.execution_trace.map(x => x.node);
  report.citations = final.citations.length;
  report.ownership_checks = ['history', 'run', 'thread', 'logout_revocation', 'account_switch'];
} catch (error) {
  report.error_type = error instanceof Error ? error.name : 'UnknownError';
} finally { await browser.close(); }
await writeFile(`${output}/live-browser-verification.json`, JSON.stringify(report, null, 2));
console.log(JSON.stringify(report));
process.exitCode = report.passed ? 0 : 1;
