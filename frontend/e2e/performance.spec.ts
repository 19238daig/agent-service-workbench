import { test, expect } from '@playwright/test';

const latency = { samples: 3, p50: 100, p95: 300 };
test('administrator sees measured metrics, missing values and refresh', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 404 }));
  await page.route('**/api/auth/refresh', r => r.fulfill({ json: { access_token: 'fixture', user: { id: 'perf-admin', display_name: '管理员', role: 'admin' } } }));
  await page.route('**/health', r => r.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/sessions?*', r => r.fulfill({ json: { sessions: [] } }));
  await page.route('**/api/admin/performance', r => r.fulfill({ json: { runs: 3, observed_attempts: 5, retries: 1, cache_hits: 2, cache_misses: 2,
    known_reported_tokens: 300, unknown_usage_calls: 1, final_reply_ms: latency, first_execution_progress_ms: { samples: 0, p50: null, p95: null },
    failures: { MODEL_TIMEOUT: 1 }, nodes: [{ node: 'knowledge_rag', latency_ms: latency }],
    models: [{ role: 'knowledge_rag', model: 'fixture-model', kind: 'chat', attempts: 5, failed: 1, latency_ms: latency, queue_ms: 10 }] } }));
  await page.goto('/#/performance');
  await expect(page.getByRole('heading', { name: '运行观测', exact: true })).toBeVisible();
  await expect(page.getByText('未记录', { exact: true })).toBeVisible();
  await expect(page.getByText('1次用量不完整', { exact: true })).toBeVisible();
  await expect(page.getByText('MODEL_TIMEOUT：1次', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '刷新记录', exact: true }).click();
  await expect(page.getByText('fixture-model · chat', { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});

test('customer deep link cannot enter administrator performance page', async ({ page }) => {
  await page.route('**/api/**', r => r.fulfill({ status: 404 }));
  await page.route('**/api/auth/refresh', r => r.fulfill({ json: { access_token: 'fixture', user: { id: 'customer', display_name: '用户', role: 'customer' } } }));
  await page.route('**/health', r => r.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/sessions?*', r => r.fulfill({ json: { sessions: [] } }));
  await page.goto('/#/performance');
  await expect(page.getByRole('button', { name: '运行观测', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '运行观测', exact: true })).toHaveCount(0);
});
