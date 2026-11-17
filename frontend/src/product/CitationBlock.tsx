import { useState } from 'react';
import type { Citation } from '../workflow';
import { useResource } from './api';

function Preview({ citation }: { citation: Citation }) {
  const source = useResource<{ content: string; currently_effective: boolean; heading?: string; page?: number; version: string }>(`/api/knowledge/documents/${encodeURIComponent(citation.document_id)}/chunks/${encodeURIComponent(citation.chunk_id!)}`);
  return <div className="citation-preview">{source.loading && <p role="status">正在核对引用…</p>}{source.error && <p role="alert">{source.error}</p>}{source.data && <><strong>{source.data.page ? `第 ${source.data.page} 页` : source.data.heading || '正文切片'} · {source.data.version}</strong><p>{source.data.content}</p>{!source.data.currently_effective && <p role="status">这是历史引用，目前已不属于有效检索资料，请核对最新版本。</p>}</>}</div>;
}

export default function CitationBlock({ citation }: { citation: Citation }) {
  const [open, setOpen] = useState(false);
  if (!citation.chunk_id || !/^[a-zA-Z0-9_-]+$/.test(citation.document_id) || !/^[a-zA-Z0-9_-]+$/.test(citation.chunk_id)) return null;
  return <div className="citation-block"><button className="desk-text" aria-expanded={open} onClick={() => setOpen(value => !value)}>[{citation.number}] {open ? '收起引用正文' : '核对引用正文'}</button>{open && <Preview citation={citation} />}</div>;
}
