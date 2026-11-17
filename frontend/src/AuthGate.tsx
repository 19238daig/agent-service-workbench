import { useEffect, useState, type FormEvent } from 'react';
import { Headphones, LockKeyhole, ArrowRight, ShieldCheck } from 'lucide-react';
import App from './App';
import { loginAccount, restoreAuthentication, type User } from './auth';
import { workspaceKey } from './workspace';
import './auth.css';

export default function AuthGate() {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [password, setPassword] = useState('');
  useEffect(() => {
    let alive = true;
    void restoreAuthentication().then(result => { if (alive) setUser(result?.user || null); })
      .catch(() => { if (alive) setError('暂时无法连接登录服务，请检查本地服务后重试。'); })
      .finally(() => { if (alive) setChecking(false); });
    return () => { alive = false; };
  }, []);
  useEffect(() => {
    const clear = () => {
      if (user) { try { localStorage.removeItem(workspaceKey(user.id)); } catch { /* restricted storage */ } }
      setUser(null); setPassword(''); setError('');
    };
    window.addEventListener('smartcs:logout', clear);
    return () => window.removeEventListener('smartcs:logout', clear);
  }, [user]);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError('');
    try { setUser(await loginAccount(name.trim(), password)); setPassword(''); }
    catch (failure) { setError(failure instanceof Error ? failure.message : '登录未完成，请重试。'); }
    finally { setBusy(false); }
  }
  if (checking) return <main className="auth-loading" role="status">正在恢复登录会话…</main>;
  if (user) return <App key={user.id} user={user} />;
  return <main className="auth-page">
    <section className="auth-intro" aria-label="智能客服工作台">
      <div className="auth-brand"><span><Headphones size={24} /></span>智能客服</div>
      <div><p className="auth-eyebrow">多 Agent 服务工作台</p><h1>每个问题，<br />都有协作的路径。</h1><p className="auth-description">从意图识别到知识检索、工单处理与合规审查，在同一个工作台查看服务进展。</p>
        <div className="auth-route" aria-label="服务流程"><span>识别需求</span><ArrowRight size={16} /><span>协作处理</span><ArrowRight size={16} /><span>审查答复</span></div></div>
      <p className="auth-note"><ShieldCheck size={16} />使用账号访问属于你的会话和服务记录</p>
    </section>
    <section className="auth-form-panel"><form className="auth-form" onSubmit={submit}>
      <span className="auth-lock"><LockKeyhole size={25} /></span><h2>登录工作台</h2><p>使用管理员为你创建的账号继续。</p>
      <label htmlFor="login-name">账号</label><input id="login-name" name="username" autoComplete="username" autoCapitalize="none" spellCheck={false} required maxLength={64} value={name} onChange={e => setName(e.target.value)} disabled={busy} autoFocus />
      <label htmlFor="login-password">密码</label><input id="login-password" name="password" type="password" autoComplete="current-password" required maxLength={128} value={password} onChange={e => setPassword(e.target.value)} disabled={busy} />
      {error && <p className="auth-error" role="alert">{error}</p>}
      <button className="auth-submit" type="submit" disabled={busy || !name.trim() || !password}>{busy ? '正在验证…' : '登录工作台'}<ArrowRight size={18} /></button>
      <p className="auth-help">忘记密码或需要新账号，请联系本机管理员。</p>
    </form></section>
  </main>;
}
