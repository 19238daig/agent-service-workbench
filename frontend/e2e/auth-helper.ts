import type { Page } from '@playwright/test';

export async function mockSession(page: Page) {
  await page.route('**/api/sessions?*', route => route.fulfill({ json: { sessions: [], has_more: false } }));
  await page.route('**/api/auth/refresh', route => route.fulfill({ json: {
    access_token: 'browser-test-token', user: { id: 'saved-user', display_name: '测试用户', role: 'customer' },
  } }));
}
