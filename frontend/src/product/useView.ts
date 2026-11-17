import { useCallback, useEffect, useState } from 'react';
export type View = 'chat' | 'tickets' | 'support' | 'knowledge' | 'performance';
export function useView(role: string) {
  const allowed = useCallback((value: string): View => value === 'tickets' || value === 'chat' ? value : value === 'support' && role !== 'customer' ? 'support' : (value === 'knowledge' || value === 'performance') && role === 'admin' ? value : 'chat', [role]);
  const [view, setView] = useState<View>(() => allowed(location.hash.replace(/^#\/?/, '') || 'chat'));
  const navigate = useCallback((next: View) => { const value = allowed(next);if (location.hash !== '#/' + value) history.pushState(null, '', '#/' + value);setView(value); }, [allowed]);
  useEffect(() => { const update = () => setView(allowed(location.hash.replace(/^#\/?/, '') || 'chat'));window.addEventListener('popstate', update);window.addEventListener('hashchange', update);return () => { window.removeEventListener('popstate', update);window.removeEventListener('hashchange', update); }; }, [allowed]);
  return { view, navigate };
}
