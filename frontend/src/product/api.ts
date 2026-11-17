import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '../auth';

export class DeskError extends Error { constructor(message: string, public code: string, public status = 0) { super(message); } }
export async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try { response = await apiFetch(url, { cache: 'no-store', ...init }); }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new DeskError(init.method && init.method !== 'GET' ? '提交结果暂未确认，请先刷新状态核实，勿重复办理。' : '暂时无法读取，请检查连接后刷新。', 'NETWORK_UNCERTAIN');
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new DeskError(body?.error?.message || body?.detail || '操作未完成，请刷新后重试。', body?.error?.code || 'REQUEST_FAILED', response.status);
  return body as T;
}
export const post = <T,>(url: string, body: object) => request<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export function useResource<T>(url: string | null, poll = 0) {
  const [data, setData] = useState<T>();const [error, setError] = useState('');const [loading, setLoading] = useState(false);const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(n => n + 1), []);
  useEffect(() => {
    setData(undefined);setError('');if (!url) return;
    const controller = new AbortController();let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      setLoading(true);
      try { const value = await request<T>(url, { signal: controller.signal });if (!controller.signal.aborted) { setData(value);setError(''); } }
      catch (failure) { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '读取失败'); }
      finally { if (!controller.signal.aborted) { setLoading(false);if (poll) timer = setTimeout(load, poll); } }
    };
    void load();return () => { controller.abort();clearTimeout(timer); };
  }, [url, revision, poll]);
  return { data, error, loading, refresh, setData };
}
export function useCommand() {
  const [busy, setBusy] = useState(false);const [error, setError] = useState('');const locked = useRef(false);
  async function run(task: () => Promise<void>) {
    if (locked.current) return;locked.current = true;setBusy(true);setError('');
    try { await task(); } catch (failure) { setError(failure instanceof Error ? failure.message : '操作未完成'); }
    finally { locked.current = false;setBusy(false); }
  }
  return { busy, error, run, setError };
}
