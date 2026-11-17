import { useRef, useState } from 'react';
import { apiFetch } from '../auth';
import { BookOpen, RefreshCw, Upload, FileText } from 'lucide-react';
import type { User } from '../auth';
import { post, request, useCommand, useResource } from './api';
import { date, label, type Document, type IndexVersion, type Job } from './types';

function DocumentPanel({ id, onChanged }: { id: string; onChanged: () => void }) {
  const source = useResource<Document>(`/api/admin/knowledge/documents/${encodeURIComponent(id)}`);const command = useCommand();
  const [checked, setChecked] = useState(false);const [notice, setNotice] = useState('');const doc = source.data;
  async function change(kind: 'publish' | 'withdraw') {
    if (!doc) return;
    await command.run(async () => { await post(`/api/admin/knowledge/documents/${encodeURIComponent(id)}/${kind}`, { content_hash: doc.content_hash, version: doc.version });setNotice(kind === 'publish' ? '文档已发布。构建并校验索引后，可启用新版本。' : '文档已撤回，后续检索会排除此版本。');onChanged(); });setChecked(false);source.refresh();
  }
  async function openFile() {
    await command.run(async () => {
      const response = await apiFetch(`/api/admin/knowledge/documents/${encodeURIComponent(id)}/file`);
      if (!response.ok) throw new Error('原文件不可用，请刷新文档核实。');
      const url = URL.createObjectURL(await response.blob());const link = document.createElement('a');link.href = url;link.download = doc?.source || 'knowledge-file';link.click();setTimeout(() => URL.revokeObjectURL(url), 60000);
    });
  }
  return <section className="desk-card" aria-label="知识文档详情"><h2>文档详情</h2>{source.error && <p role="alert" className="desk-error">{source.error}</p>}{source.loading && !doc && <p role="status">正在读取文档…</p>}
    {doc && <><div className="desk-line"><h3>{doc.title}</h3><span className="desk-status">{label(doc.status)}</span></div><dl className="desk-facts"><div><dt>版本 / 可见范围</dt><dd>{doc.version} / {label(doc.access_scope)}</dd></div><div><dt>发布者</dt><dd>{doc.publisher || '未提供'}</dd></div><div><dt>许可</dt><dd>{doc.license || '未提供'}</dd></div><div><dt>生效日期 / 切片</dt><dd>{doc.effective_from} / {doc.chunks} 块</dd></div></dl>
      <button className="desk-secondary" disabled={command.busy} onClick={() => void openFile()}><FileText size={16} aria-hidden="true" />下载原文件</button>
      <section className="desk-form-section"><h3>切片预览</h3>{doc.preview?.map(chunk => <div className="chunk-preview" key={chunk.id}><strong>{chunk.page ? `第 ${chunk.page} 页` : chunk.heading || '文档正文'}</strong><p>{chunk.content}</p></div>)}</section>
      {['draft', 'published'].includes(doc.status) && <div className="desk-form-section"><label className="desk-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} disabled={command.busy} />我已核对来源、有效期和可见范围</label>
        <button className={doc.status === 'draft' ? 'desk-primary' : 'desk-secondary'} disabled={!checked || command.busy || !!source.error} onClick={() => void change(doc.status === 'draft' ? 'publish' : 'withdraw')}>{doc.status === 'draft' ? '确认发布文档' : '确认撤回文档'}</button></div>}
      {doc.status === 'withdrawn' && <p className="desk-hint">已撤回的版本不能重新发布。需要更新时，请使用新的文档版本入库。</p>}
    </>}{notice && <p className="desk-success" role="status">{notice}</p>}{command.error && <p className="desk-error" role="alert">{command.error}</p>}
  </section>;
}

function UploadDocument({ user, onQueued }: { user: User; onQueued: (job: Job) => void }) {
  const [file, setFile] = useState<File>();const [title, setTitle] = useState('');const [sourceKey, setSourceKey] = useState('');const [version, setVersion] = useState('1');
  const [publisher, setPublisher] = useState('');const [license, setLicense] = useState('');const [url, setUrl] = useState('');const [scope, setScope] = useState('private');const [domain, setDomain] = useState('consumer_reference');
  const [effective, setEffective] = useState(new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' }));const [chunker, setChunker] = useState('structural');
  const command = useCommand();const submitted = useRef<{ serial: string; key: string } | undefined>(undefined);
  async function upload() {
    await command.run(async () => {
      if (!file || !file.size || file.size > 25 * 1024 * 1024 || !/\.(pdf|html?|md|txt)$/i.test(file.name)) throw new Error('请选择25MiB以内的文本PDF、HTML、Markdown或TXT文件。');
      const artifact = await request<{ artifact_sha256: string; extension: string }>(`/api/admin/job-artifacts?filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file });
      const work = { kind: 'ingest', artifact_sha256: artifact.artifact_sha256, extension: artifact.extension, publish: false,
        document: { source_key: sourceKey, title, version, publisher, license, source_url: url || null, domain, access_scope: scope,
          owner_id: scope === 'private' ? user.id : null, effective_from: effective, source_type: 'operator_document', expected_sha256: artifact.artifact_sha256 }, options: { chunker } };
      const serial = JSON.stringify(work);if (submitted.current?.serial !== serial) submitted.current = { serial, key: crypto.randomUUID() };
      const job = await post<Job>('/api/admin/jobs', { idempotency_key: submitted.current.key, work });onQueued(job);
    });
  }
  return <section className="desk-card upload-card"><h2>上传知识原文件</h2><p className="desk-hint">解析入库在后台运行，先保存为草稿。核对来源和切片后再发布。</p>
    <form onSubmit={e => { e.preventDefault();void upload(); }}>
      <label className="desk-file"><Upload size={26} aria-hidden="true" /><strong>{file?.name || '选择知识文件'}</strong><span>PDF / HTML / Markdown / TXT · 最大25MiB</span><input type="file" aria-label="知识原文件" required accept=".pdf,.html,.htm,.md,.txt" disabled={command.busy} onChange={e => { const value = e.target.files?.[0];setFile(value);if (value && !title) setTitle(value.name.replace(/\.[^.]+$/, ''));if (!sourceKey) setSourceKey('operator-' + crypto.randomUUID()); }} /></label>
      <div className="desk-fields"><label className="desk-field">文档标题<input required maxLength={180} value={title} onChange={e => setTitle(e.target.value)} /></label><label className="desk-field">来源标识<input required maxLength={256} value={sourceKey} onChange={e => setSourceKey(e.target.value)} /><small>同一资料更新版本时沿用此标识。</small></label>
        <label className="desk-field">版本<input required maxLength={32} value={version} onChange={e => setVersion(e.target.value)} /></label><label className="desk-field">生效日期<input required type="date" value={effective} onChange={e => setEffective(e.target.value)} /></label>
        <label className="desk-field">发布者<input required maxLength={180} value={publisher} onChange={e => setPublisher(e.target.value)} /></label><label className="desk-field">使用许可<input required maxLength={180} value={license} onChange={e => setLicense(e.target.value)} /></label>
        <label className="desk-field">可见范围<select value={scope} onChange={e => setScope(e.target.value)}>{['private', 'public', 'support', 'admin'].map(s => <option key={s} value={s}>{label(s)}</option>)}</select></label>
        <label className="desk-field">知识集合<select value={domain} onChange={e => setDomain(e.target.value)}><option value="consumer_reference">消费与售后参考</option><option value="helpdesk_reference">客服流程参考</option></select></label>
        <label className="desk-field">切分方式<select value={chunker} onChange={e => setChunker(e.target.value)}><option value="structural">按文档结构切分</option><option value="bge-semantic">本地 BGE-M3 语义切分</option></select></label>
        <label className="desk-field">原始来源网址（可选）<input type="url" maxLength={2048} value={url} onChange={e => setUrl(e.target.value)} /></label>
      </div><button className="desk-primary" disabled={!file || command.busy}>{command.busy ? '正在提交…' : '上传并后台入库'}</button>
      {command.error && <p className="desk-error" role="alert">{command.error}</p>}
    </form>
  </section>;
}

function JobEvents({ id }: { id: string }) {
  const [after, setAfter] = useState(0);const source = useResource<{ events: { version: number; type: string; payload: { stage?: string; progress?: number; total?: number }; created_at: string }[] }>(`/api/admin/jobs/${encodeURIComponent(id)}/events?after=${after}`);
  return <section className="desk-form-section"><h3>任务执行记录</h3>{source.error && <p className="desk-error" role="alert">{source.error}</p>}<ol className="desk-timeline">{source.data?.events.map(e => <li key={e.version}><div className="desk-line"><strong>{label(e.type)}</strong><time>{date(e.created_at)}</time></div>{e.payload.total ? <p>{e.payload.progress}/{e.payload.total}</p> : null}</li>)}</ol><div className="desk-pages"><button disabled={!after} onClick={() => setAfter(0)}>回到首条</button><button disabled={source.data?.events.length !== 100} onClick={() => setAfter(source.data!.events.at(-1)!.version)}>下一页记录</button></div></section>;
}
function JobsPanel({ onDocument }: { onDocument: (id: string) => void }) {
  const source = useResource<{ jobs: Job[] }>('/api/admin/jobs', 3000);const command = useCommand();const [selected, setSelected] = useState('');
  async function mutate(job: Job, action: 'cancel' | 'retry') { await command.run(async () => { await post(`/api/admin/jobs/${encodeURIComponent(job.id)}/${action}`, action === 'retry' ? { payload_hash: job.payload_hash } : {}); });source.refresh(); }
  return <section className="desk-card"><header className="desk-card-heading"><h2>后台任务</h2><button className="desk-icon" onClick={source.refresh} aria-label="刷新后台任务"><RefreshCw size={16} /></button></header>
    <p className="desk-hint">文档解析、索引构建和评测的状态由服务器保存。停止任务不会撤回已保存的结果。</p>{source.error && <p className="desk-error" role="alert">{source.error}</p>}{!source.data?.jobs.length && !source.loading && <p className="desk-empty">尚未提交后台任务。</p>}
    <div className="job-list">{source.data?.jobs.map(job => <article key={job.id} className="job-row"><div className="desk-line"><strong>{label(job.kind)}</strong><span className="desk-status">{label(job.status)}</span></div><small>{date(job.created_at)} · 执行 {job.attempt}/{job.max_attempts} 次</small>
      {!!job.total && <><progress value={job.progress} max={job.total} aria-label="后台任务进度" /><p className="desk-meta">已处理 {job.progress}/{job.total}</p></>}{job.error_code && <p className="desk-error">错误编号：{job.error_code}</p>}
      <div className="desk-actions">{job.result?.document_id && <button className="desk-secondary" onClick={() => onDocument(job.result!.document_id!)}>核对入库文档</button>}
        {['queued', 'running', 'retry_wait'].includes(job.status) && <button className="desk-secondary" disabled={command.busy} onClick={() => void mutate(job, 'cancel')}>停止任务</button>}
        {job.status === 'failed' && job.attempt < job.max_attempts && <button className="desk-secondary" disabled={command.busy} onClick={() => void mutate(job, 'retry')}>重试原任务</button>}
        <button className="desk-text" onClick={() => setSelected(selected === job.id ? '' : job.id)}>查看执行记录</button></div>{selected === job.id && <JobEvents key={job.id} id={job.id} />}
    </article>)}</div>{command.error && <p className="desk-error" role="alert">{command.error}</p>}
  </section>;
}

function IndexPanel({ onQueued }: { onQueued: (job: Job) => void }) {
  const source = useResource<{ active_index_id: string | null; indexes: IndexVersion[] }>('/api/admin/knowledge/indexes');
  const audit = useResource<{ events: { id: string; kind: string; resource_id: string; created_at: string }[] }>('/api/admin/knowledge/events');
  const command = useCommand();const [target, setTarget] = useState('');const [checked, setChecked] = useState(false);const [notice, setNotice] = useState('');const submitted = useRef<{ kind: string; key: string } | undefined>(undefined);
  async function queue(kind: 'index' | 'retrieval_evaluation') {
    await command.run(async () => { if (submitted.current?.kind !== kind) submitted.current = { kind, key: crypto.randomUUID() };const job = await post<Job>('/api/admin/jobs', { idempotency_key: submitted.current.key, work: kind === 'index' ? { kind } : { kind, split: 'validation' } });submitted.current = undefined;onQueued(job); });
  }
  async function activate() {
    if (!source.data || !target) return;
    await command.run(async () => { await post(`/api/admin/knowledge/indexes/${encodeURIComponent(target)}/activate`, { expected_active_index_id: source.data!.active_index_id });setNotice('新索引已启用，原版本保留。'); });setChecked(false);source.refresh();audit.refresh();
  }
  return <><section className="desk-card"><header className="desk-card-heading"><h2>向量索引版本</h2><button className="desk-icon" onClick={source.refresh} aria-label="刷新索引版本"><RefreshCw size={16} /></button></header>
    <p className="desk-hint">构建完成后先保留为待校验版本。启用前会核对实际模型、文档范围和向量完整性。</p><div className="desk-actions"><button className="desk-primary" disabled={command.busy} onClick={() => void queue('index')}>后台构建新索引</button><button className="desk-secondary" disabled={command.busy} onClick={() => void queue('retrieval_evaluation')}>运行开发检索评测</button></div>
    {source.error && <p className="desk-error" role="alert">{source.error}</p>}<div className="desk-list">{source.data?.indexes.map(index => <label className="index-choice" key={index.id}><input type="radio" name="index-version" value={index.id} checked={target === index.id} disabled={index.active || index.protocol !== 'dense-exact-v1'} onChange={() => { setTarget(index.id);setChecked(false); }} /><div><div className="desk-line"><strong>{index.documents ?? '—'} 份文档 · {index.chunks ?? '—'} 个向量</strong><span className="desk-status">{index.active ? '当前启用' : '待校验版本'}</span></div><code>{index.id}</code><small>{index.model_identity.embedding || '旧版模型'} · {date(index.created_at)}</small></div></label>)}</div>
    <label className="desk-check"><input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} disabled={!target || command.busy} />我已核对目标索引的文档范围和模型</label><button className="desk-secondary" disabled={!target || !checked || command.busy || !!source.error} onClick={() => void activate()}>校验并启用所选索引</button>
    {notice && <p className="desk-success" role="status">{notice}</p>}{command.error && <p className="desk-error" role="alert">{command.error}</p>}</section>
    <section className="desk-card"><h2>知识管理操作记录</h2>{audit.error && <p className="desk-error" role="alert">{audit.error}</p>}<ol className="desk-timeline">{audit.data?.events.map(e => <li key={e.id}><div className="desk-line"><strong>{{ document_published: '文档已发布', document_withdrawn: '文档已撤回', index_activated: '索引已切换' }[e.kind] || e.kind}</strong><time>{date(e.created_at)}</time></div><code>{e.resource_id}</code></li>)}</ol>{!audit.data?.events.length && <p className="desk-empty">暂无管理操作记录。</p>}</section></>;
}

export default function KnowledgeDesk({ user, blocked }: { user: User; blocked?: boolean }) {
  const [tab, setTab] = useState('documents');const [filter, setFilter] = useState('');const [offset, setOffset] = useState(0);const [selected, setSelected] = useState('');const [notice, setNotice] = useState('');
  const source = useResource<{ documents: Document[]; total: number; has_more: boolean }>(`/api/admin/knowledge/documents?limit=20&offset=${offset}${filter ? '&status=' + filter : ''}`);
  function queued(job: Job) { setNotice(`已登记${label(job.kind)}任务，可在后台任务中查看进度。`);setTab('jobs'); }
  function showDocument(id: string) { setSelected(id);setFilter('');setTab('documents');source.refresh(); }
  return <main className="main-panel desk-main" id="product-main" inert={blocked || undefined}><header className="topbar"><div className="page-location"><span>管理</span><strong>知识与后台任务</strong></div><BookOpen size={19} aria-hidden="true" /></header><div className="desk-page">
    <div className="desk-page-heading"><div><p className="desk-eyebrow">知识管理</p><h1>从原文件到可追溯知识</h1><p>上传原件、核对草稿、发布资料，再校验并启用索引。</p></div></div>
    <div className="desk-tabs" role="tablist" aria-label="知识管理页面">{[['documents', '知识文档'], ['upload', '上传文件'], ['jobs', '后台任务'], ['indexes', '索引与记录']].map(([id, name]) => <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>{name}</button>)}</div>
    {notice && <p className="desk-success" role="status">{notice}</p>}
    {tab === 'upload' ? <UploadDocument user={user} onQueued={queued} /> : tab === 'jobs' ? <JobsPanel onDocument={showDocument} /> : tab === 'indexes' ? <IndexPanel onQueued={queued} />
      : <><div className="desk-toolbar"><label>发布状态<select aria-label="筛选发布状态" value={filter} onChange={e => { setFilter(e.target.value);setOffset(0); }}><option value="">全部版本</option>{['draft', 'published', 'withdrawn'].map(s => <option key={s} value={s}>{label(s)}</option>)}</select></label><button className="desk-secondary" onClick={source.refresh}><RefreshCw size={15} aria-hidden="true" />刷新文档</button></div>
        <div className="desk-split"><section className="desk-card"><h2>知识文档</h2>{source.error && <p className="desk-error" role="alert">{source.error}</p>}{source.loading && <p role="status">正在读取…</p>}<div className="desk-list">{source.data?.documents.map(doc => <button key={doc.id} className={selected === doc.id ? 'selected' : ''} onClick={() => setSelected(doc.id)}><div className="desk-line"><strong>{doc.title}</strong><span className="desk-status">{label(doc.status)}</span></div><small>{doc.version} · {label(doc.access_scope)} · {doc.chunks} 块</small></button>)}</div>{!source.loading && !source.data?.documents.length && <p className="desk-empty">暂无符合条件的文档。</p>}
          <div className="desk-pages"><button disabled={!offset} onClick={() => setOffset(n => Math.max(0, n - 20))}>上一页</button><button disabled={!source.data?.has_more} onClick={() => setOffset(n => n + 20)}>下一页</button></div></section>
          {selected ? <DocumentPanel key={selected} id={selected} onChanged={source.refresh} /> : <div className="desk-card desk-empty">选择文档核对原件、权限和切片。</div>}</div></>}
  </div></main>;
}
