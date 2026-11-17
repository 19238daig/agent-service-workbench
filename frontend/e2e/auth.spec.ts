import { mockAnswer } from './run-helper';
import { test, expect } from '@playwright/test';
test.beforeEach(async ({ page }) => { await page.route('**/api/sessions?*', r => r.fulfill({ json: { sessions: [] } })); });

test('login errors, account switching and logout clear visible and stored private history', async ({ page }) => {
  await page.route('**/api/auth/refresh', route => route.fulfill({ status: 401, json: {} }));
  await page.route('**/api/auth/login', route => {
    const body = route.request().postDataJSON();
    return body.password === 'correct-password'
      ? route.fulfill({ json: { access_token: `secret-${body.login_name}`, user: { id: body.login_name, display_name: body.login_name, role: 'customer' } } })
      : route.fulfill({ status: 401, json: {} });
  });
  await page.route('**/api/auth/logout', route => route.fulfill({ status: 204 }));
  await page.route('**/health', route => route.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', route => route.fulfill({ status: 404 }));
  await page.route('**/api/history/**', route => route.fulfill({ json: { messages: [{ role: 'user', content: 'saved' }] } }));
  await mockAnswer(page, request => {
    expect(request.headers().authorization).toBe('Bearer secret-alice');
    expect(request.postDataJSON()).not.toHaveProperty('user_id');
    return { response: 'Alice 的私人记录', session_id: 'alice-session', compliance_passed: true };
  });
  await page.goto('/');
  await page.getByLabel('账号', { exact: true }).fill('alice');
  await page.getByLabel('密码', { exact: true }).fill('incorrect');
  await page.getByRole('button', { name: '登录工作台' }).click();
  await expect(page.getByRole('alert')).toContainText('账号或密码不正确');
  await page.getByLabel('密码', { exact: true }).fill('correct-password');
  await page.getByRole('button', { name: '登录工作台' }).click();
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('我的个人问题');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('Alice 的私人记录', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('secret-alice');
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('smart-cs.workspace.v2:alice'))).toBeNull();
  await page.getByLabel('账号', { exact: true }).fill('bob');
  await page.getByLabel('密码', { exact: true }).fill('correct-password');
  await page.getByRole('button', { name: '登录工作台' }).click();
  await expect(page.getByRole('textbox', { name: '输入消息' })).toBeVisible();
  await expect(page.getByText('Alice 的私人记录', { exact: true })).toHaveCount(0);
  await expect(page.getByText('我的个人问题', { exact: true })).toHaveCount(0);
});

test('login layout and keyboard focus work at desktop and mobile sizes', async ({ page }) => {
  await page.route('**/api/auth/refresh', route => route.fulfill({ status: 401, json: {} }));
  await page.goto('/');
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await expect(page.getByLabel('账号', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '登录工作台' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: test.info().outputPath(`login-${width}.png`), fullPage: true });
  }
  await page.getByLabel('账号', { exact: true }).focus();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('密码', { exact: true })).toBeFocused();
});

test('expired access refreshes once and keeps bearer tokens out of browser storage', async ({ page }) => {
  let refreshes = 0;
  await page.route('**/api/auth/refresh', route => {
    refreshes++;
    return route.fulfill({ json: { access_token: `rotated-${refreshes}`, user: { id: 'alice', display_name: 'alice', role: 'customer' } } });
  });
  await page.route('**/health', route => route.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', route => route.request().headers().authorization === 'Bearer rotated-1'
    ? route.fulfill({ status: 401, json: {} }) : route.fulfill({ json: { nodes: [], edges: [] } }));
  await page.goto('/');
  await expect(page.getByRole('textbox', { name: '输入消息' })).toBeVisible();
  await expect.poll(() => refreshes).toBe(2);
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain('rotated-');
});

test('refresh cannot bind a different account from another tab to the current workspace', async ({ page }) => {
  let refreshes = 0;
  let sentAsBob = false;
  await page.route('**/api/auth/refresh', route => {
    const id = ++refreshes === 1 ? 'alice' : 'bob';
    return route.fulfill({ json: { access_token: `secret-${id}`, user: { id, display_name: id, role: 'customer' } } });
  });
  await page.route('**/health', route => route.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', route => {
    sentAsBob ||= route.request().headers().authorization === 'Bearer secret-bob';
    return route.fulfill({ status: 401, json: {} });
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '登录工作台' })).toBeVisible();
  expect(refreshes).toBe(2);
  expect(sentAsBob).toBe(false);
  expect(await page.evaluate(() => localStorage.getItem('smart-cs.workspace.v2:alice'))).toBeNull();
});
