import { mockAnswer } from './run-helper';
import { mockSession } from './auth-helper';
import { test, expect, type Page } from '@playwright/test';

test.beforeEach(async ({ page }) => { await mockSession(page); });

async function health(page: Page, ready = true) {
  await page.route('**/api/workflow', route => route.fulfill({ status: 404 }));
  await page.route('**/health', route => route.fulfill({ json: { status: ready ? 'healthy' : 'configuration_required', chat_ready: ready } }));
  await page.route('**/api/history/**', route => route.fulfill({ json: { messages: [{ role: 'user', content: '已保存的消息' }] } }));
}

test('send, follow up in the same session, render safely, and restore after reload', async ({ page }) => {
  await health(page);
  const requests: Record<string, string>[] = [];
  await mockAnswer(page, async request => {
    requests.push(request.postDataJSON());
    await new Promise(resolve => setTimeout(resolve, 250));
    return {
      response: requests.length === 1 ? '**7天内**可申请退款。\n\n来源：refund_policy.md\n\n<script>window.hacked=true</script>' : '退款将在3-5个工作日内原路退回。',
      session_id: 'browser-test-session', intent: 'knowledge_rag', compliance_passed: true,
    };
  });
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('退款政策是什么？');
  await page.getByRole('textbox', { name: '输入消息' }).press('Enter');
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  await expect(page.getByText('7天内', { exact: true })).toBeVisible();
  expect(requests[0].session_id).toBeNull();
  expect(await page.evaluate(() => (window as unknown as { hacked?: boolean }).hacked)).toBeUndefined();
  await page.getByRole('textbox', { name: '输入消息' }).fill('多久可以到账？');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('退款将在3-5个工作日内原路退回。', { exact: true })).toBeVisible();
  expect(requests[1].session_id).toBe('browser-test-session');
  expect(requests[1]).not.toHaveProperty('user_id');
  await page.reload();
  await expect(page.getByText('退款将在3-5个工作日内原路退回。', { exact: true })).toBeVisible();
  await expect(page.getByRole('article', { name: '你的消息' })).toHaveCount(2);
  await page.screenshot({ path: test.info().outputPath('conversation-desktop.png'), fullPage: true });
});

test('an error can be retried without duplicating the user turn, and conversations can be deleted', async ({ page }) => {
  await health(page);
  let attempts = 0;
  await mockAnswer(page, () => {
    attempts++;
    return attempts === 1 ? { errorStatus: 500 } : { response: '退款工单已创建：TK-TEST-001', session_id: 'retry-session', intent: 'ticket_handler', compliance_passed: true };
  });
  await page.goto('/');
  await page.getByRole('textbox', { name: '输入消息' }).fill('请帮我申请退款');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('button', { name: '重试', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await expect(page.getByText('退款工单已创建：TK-TEST-001', { exact: true })).toBeVisible();
  await expect(page.getByRole('article', { name: '你的消息' })).toHaveCount(1);
  await page.getByRole('button', { name: '新建对话' }).click();
  await expect(page.getByRole('heading', { name: '你好，有什么可以帮你？' })).toBeVisible();
  await page.getByRole('button', { name: '删除对话：请帮我申请退款' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '删除对话', exact: true }).click();
  await expect(page.getByRole('button', { name: '删除对话：请帮我申请退款' })).toHaveCount(0);
});

test('missing model configuration and corrupt storage do not produce silent sends', async ({ page }) => {
  await health(page, false);
  let requests = 0;
  await page.route('**/api/runs', route => { requests++; return route.abort(); });
  await page.addInitScript(() => localStorage.setItem('smart-cs.workspace.v2:saved-user', 'invalid json'));
  await page.goto('/');
  await expect(page.getByText('需要配置模型', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('测试问题');
  await page.getByRole('textbox', { name: '输入消息' }).press('Enter');
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  expect(requests).toBe(0);
  await expect(page.getByRole('textbox', { name: '输入消息' })).toHaveValue('测试问题');
});

test('restarting the backend preserves browser history and starts a fresh server session', async ({ page }) => {
  await health(page);
  await page.route('**/api/history/**', route => route.fulfill({ json: { messages: [] } }));
  await page.addInitScript(() => localStorage.setItem('smart-cs.workspace.v2:saved-user', JSON.stringify({
    userId: 'saved-user', activeId: 'saved-conversation', conversations: [{
      id: 'saved-conversation', title: '之前的对话', sessionId: 'expired-session', createdAt: 1, updatedAt: 1, draft: '',
      messages: [{ id: 'old-user', role: 'user', content: '旧的问题' }, { id: 'old-answer', role: 'assistant', content: '旧的回答', status: 'done' }],
    }],
  })));
  let requestedSession: string | null | undefined;
  await mockAnswer(page, request => {
    requestedSession = request.postDataJSON().session_id;
    return { response: '新的回答', session_id: 'new-session', intent: 'knowledge_rag', compliance_passed: true };
  });
  await page.goto('/');
  await expect(page.getByText('历史消息已保留在此浏览器。服务记录已重置，继续发送将建立新的服务会话。')).toBeVisible();
  await page.getByRole('textbox', { name: '输入消息' }).fill('新的问题');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('新的回答', { exact: true })).toBeVisible();
  expect(requestedSession).toBeNull();
  await expect(page.getByText('旧的回答', { exact: true })).toBeVisible();
});

test('desktop, tablet and mobile have no overflow and the mobile conversation drawer is usable', async ({ page }) => {
  await health(page);
  await page.goto('/');
  await expect(page.getByText('服务已连接', { exact: true })).toBeVisible();
  for (const width of [1440, 1024, 768, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: test.info().outputPath(`welcome-${width}.png`), fullPage: true });
  }
  await page.getByRole('button', { name: '打开会话列表' }).click();
  await expect(page.getByRole('textbox', { name: '搜索对话' })).toBeVisible();
  await page.getByRole('textbox', { name: '搜索对话' }).fill('没有这个会话');
  await expect(page.getByText('没有找到相关对话')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('textbox', { name: '搜索对话' })).not.toBeVisible();
  await page.getByRole('button', { name: '退货规则查询' }).click();
  await expect(page.getByRole('textbox', { name: '输入消息' })).toHaveValue('哪些商品不适用七日无理由退货？请说明资料来源。');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.screenshot({ path: test.info().outputPath('mobile-compose.png'), fullPage: true });
});
