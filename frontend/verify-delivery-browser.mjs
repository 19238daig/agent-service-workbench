/** Real deployed React/HTTP checks, no model requests or recorded credentials. */
import { chromium, expect } from '@playwright/test';
import { readFile, writeFile } from 'node:fs/promises';

const base = 'http://127.0.0.1:18000';
const source = JSON.parse(await readFile('../.local/delivery/backup/source.json', 'utf8'));
const accounts = JSON.parse(await readFile(source.source_live ? '../.local/auth/bootstrap-accounts.json' : '../.local/delivery/fresh-accounts.json', 'utf8')).accounts;
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_BROWSER_CHANNEL || 'msedge', headless: true });
const report = { passed: false, real_browser: true, real_api: true, models_invoked: 0, roles: [] };
try {
  for (const [name, button, heading] of [['customer-a', '我的工单', '查看你的申请进度'], ['support', '客服工作台', '处理分派与审批'], ['admin', '知识管理', '从原文件到可追溯知识']]) {
    const actor = accounts.find(a => a.login_name === name);
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    try {
      const page = await context.newPage();await page.goto(base);
      await page.getByLabel('账号', { exact: true }).fill(name);await page.getByLabel('密码', { exact: true }).fill(actor.password);
      const loggedIn = page.waitForResponse(r => new URL(r.url()).pathname === '/api/auth/login');
      await page.getByRole('button', { name: '登录工作台', exact: true }).click();expect((await loggedIn).status()).toBe(200);
      await page.getByRole('button', { name: button, exact: true }).click();await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
      if (name === 'customer-a') await expect(page.getByRole('button', { name: '知识管理', exact: true })).toHaveCount(0);
      if (name === 'admin') {
        await page.getByRole('button', { name: '运行观测', exact: true }).click();await expect(page.getByRole('heading', { name: '运行观测', exact: true })).toBeVisible();
      }
      report.roles.push({ name, login: true, authorized_workbench: true });
    } finally { await context.close(); }
  }
  report.passed = true;
} finally {
  await browser.close();await writeFile('../.local/delivery/browser-live.json', JSON.stringify(report, null, 2));
}
console.log(JSON.stringify({ passed: report.passed, real_api: true, roles: report.roles.length, models_invoked: 0 }));
