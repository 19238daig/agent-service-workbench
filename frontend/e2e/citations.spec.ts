import { mockAnswer } from './run-helper';
import { mockSession } from './auth-helper';
import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => { await mockSession(page); });

test('verified citations survive reload and reject arbitrary source URLs', async ({ page }) => {
  await page.route('**/health', route => route.fulfill({ json: { chat_ready: true } }));
  await page.route('**/api/workflow', route => route.fulfill({ status: 404 }));
  await page.route('**/api/history/**', route => route.fulfill({ json: { messages: [] } }));
  await mockAnswer(page, () => ({
    response: '依据公开条例，应在续费前提醒。[1]', session_id: 'citation-test', intent: 'knowledge_rag', compliance_passed: true,
    citations: [
      { number: 1, document_id: 'file-public', source: 'source.pdf', page: 3, url: '/api/knowledge/documents/file-public/file#page=3' },
      { number: 2, document_id: 'bad', source: 'unsafe', url: 'javascript:alert(1)' },
    ],
  }));
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('自动续费如何提醒？');
  await page.getByRole('button', { name: '发送消息' }).click();
  const citation = page.getByRole('button', { name: '[1] source.pdf · 第 3 页' });
  await expect(citation).toBeVisible();
  await page.route('**/api/knowledge/documents/file-public/file', route => {
    expect(route.request().headers().authorization).toBe('Bearer browser-test-token');
    return route.fulfill({ contentType: 'application/pdf', body: '%PDF-test' });
  });
  const request = page.waitForRequest('**/api/knowledge/documents/file-public/file');
  await citation.click(); await request;
  await expect(page.getByRole('button', { name: 'unsafe' })).toHaveCount(0);
  await page.reload();
  await expect(citation).toBeVisible();
});
