export type User = { id: string; display_name: string; role: 'customer' | 'support' | 'admin' };
type LoginResult = { access_token: string; user: User };
let accessToken = '';
let subjectId = '';
let refreshPending: Promise<LoginResult | null> | null = null;
let lifetime = new AbortController();
let generation = 0;

export const isCurrentAccount = (id: string) => subjectId === id && !!accessToken;

function csrf() {
  return document.cookie.split('; ').find(c => c.startsWith('smartcs_csrf='))?.slice('smartcs_csrf='.length) || '';
}

export function clearAuthentication() {
  generation++; accessToken = ''; subjectId = ''; lifetime.abort(); lifetime = new AbortController();
  window.dispatchEvent(new Event('smartcs:logout'));
}

export async function restoreAuthentication(): Promise<LoginResult | null> {
  if (!refreshPending) {
    const attempt = generation;
    refreshPending = (async () => {
      const response = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRF-Token': csrf() }, signal: AbortSignal.timeout(10000) });
      if (!response.ok) return null;
      const result: LoginResult = await response.json();
      if (attempt !== generation) return null;
      // Another tab may have replaced the shared refresh cookie with a different
      // account. Never attach that identity to the current account's workspace.
      if (subjectId && result.user.id !== subjectId) { clearAuthentication(); return null; }
      accessToken = result.access_token;
      subjectId = result.user.id;
      return result;
    })().finally(() => { refreshPending = null; });
  }
  return refreshPending;
}

export async function loginAccount(login_name: string, password: string): Promise<User> {
  const response = await fetch('/api/auth/login', { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ login_name, password }), signal: AbortSignal.timeout(15000) });
  if (!response.ok) throw new Error(response.status === 429 ? '登录尝试过于频繁，请在五分钟后重试。' : response.status === 401 ? '账号或密码不正确，或账号已停用。' : '登录服务暂时不可用，请稍后重试。');
  const result: LoginResult = await response.json();
  generation++; lifetime.abort(); lifetime = new AbortController(); accessToken = result.access_token; subjectId = result.user.id;
  return result.user;
}

export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const epoch = generation;
  const signal = init.signal ? AbortSignal.any([init.signal, lifetime.signal]) : lifetime.signal;
  const send = () => {
    const headers = new Headers(init.headers); headers.set('Authorization', `Bearer ${accessToken}`);
    return fetch(input, { ...init, headers, credentials: 'same-origin', signal });
  };
  let response = await send();
  if (response.status === 401) {
    const restored = await restoreAuthentication();
    if (epoch !== generation) throw new Error('账号会话已变更。');
    if (!restored) { clearAuthentication(); throw new Error('登录已失效，请重新登录。'); }
    response = await send();
    if (response.status === 401) { clearAuthentication(); throw new Error('登录已失效，请重新登录。'); }
  }
  if (epoch !== generation || signal.aborted) throw new DOMException('Account session changed', 'AbortError');
  return response;
}

export async function logoutAccount() {
  const response = await apiFetch('/api/auth/logout', { method: 'POST' });
  if (!response.ok) throw new Error('暂时无法退出登录，请重试。');
  clearAuthentication();
}
