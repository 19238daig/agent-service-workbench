import type { Page, Request } from '@playwright/test';

export async function mockRunSubmission(page: Page, sessionId = 'stream-session') {
  await page.route('**/api/runs', route => route.fulfill({ status: 202, json: { run_id: 'fixture-run', session_id: sessionId, status: 'queued' } }));
  await page.route('**/api/runs/fixture-run', route => route.fulfill({ json: { run_id: 'fixture-run', session_id: sessionId,
    status: 'interrupted', run_status: 'interrupted', result_event: { type: 'error', code: 'EXECUTION_INTERRUPTED', message: '执行连接已中断，请恢复上轮处理。' } } }));
  await page.route('**/api/runs/by-request/*', route => route.fulfill({ status: 404 }));
}

type Reply = { [key: string]: unknown; response?: string; session_id?: string; errorStatus?: number };
export async function mockAnswer(page: Page, answer: (request: Request) => Reply | Promise<Reply>) {
  const runs = new Map<string, Reply>();const keys = new Map<string, string>();let next = 0;
  await page.route('**/api/runs', async route => {
    const key = route.request().postDataJSON().client_request_id;
    let id = keys.get(key);
    if (!id) {
      const reply = await answer(route.request());
      if (reply.errorStatus) return route.fulfill({ status: reply.errorStatus, json: { detail: 'test failure' } });
      id = `fixture-run-${++next}`;keys.set(key, id);runs.set(id, reply);
    }
    return route.fulfill({ status: 202, json: { run_id: id, session_id: runs.get(id)!.session_id, status: 'queued' } });
  });
  await page.route('**/api/runs/*/events', route => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    const reply = runs.get(id);
    return route.fulfill({ contentType: 'text/event-stream', body: `id: 1\nevent: complete\ndata: ${JSON.stringify({ ...reply, type: 'complete', run_id: id })}\n\n` });
  });
  await page.route('**/api/runs/by-request/*', route => route.fulfill({ status: 404 }));
}
