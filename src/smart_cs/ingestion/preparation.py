"""Synchronous normalization and source-locatable chunks; no required model service."""
from dataclasses import replace
import hashlib
import re
import unicodedata
from smart_cs.ingestion.parsers import parse
from smart_cs.ingestion.chunking import semantic_chunks
from smart_cs.ingestion.errors import IngestionError

PARSER_VERSION='native-structure-v3'
CLEANER_VERSION='unicode-nfc-control-v1'
CHUNKER_VERSION='structural-char-v1'


def normalize(value):
    value=unicodedata.normalize('NFC',value).replace('\r\n','\n').replace('\r','\n')
    value=value.replace('\u00a0',' ').replace('\u200b','').replace('\ufeff','').replace('\u00ad','')
    value=''.join(c for c in value if c in '\n\t' or unicodedata.category(c)!='Cc')
    return '\n'.join(line.rstrip() for line in value.splitlines()).strip()


def prepare_document(path,options,models=None):
    try:raw_blocks=parse(path)
    except UnicodeError:
        raise IngestionError('TEXT_ENCODING_UNSUPPORTED','Markdown/TXT 请保存为 UTF-8 编码后重试。') from None
    except ValueError as error:
        if 'OCR' in str(error):raise IngestionError('OCR_REQUIRED','PDF 无可提取文字，请先完成 OCR；当前不会生成替代正文。') from None
        if 'Encrypted PDF' in str(error):raise IngestionError('PDF_REJECTED','不支持加密 PDF 或超过 200 页的 PDF。') from None
        raise
    unprocessed=[b.page for b in raw_blocks if b.kind=='image_without_text']
    if unprocessed:raise IngestionError('OCR_REQUIRED','PDF 含未提取文字的图片页，请先完成 OCR。')
    blocks=[];seen=set();dropped=0
    for source_index,block in enumerate(raw_blocks):
        value=normalize(block.text)
        if not value:continue
        key=(value,block.page,block.heading,block.kind,block.anchor)
        if key in seen:dropped+=1;continue
        seen.add(key);blocks.append((source_index,replace(block,text=value,heading=normalize(block.heading)[:500])))
    if not blocks:raise IngestionError('EMPTY_DOCUMENT','清洗后无可入库正文。')
    if sum(len(b.text) for _,b in blocks)>2_000_000:raise IngestionError('EXTRACTED_TEXT_LIMIT','提取正文超过 200 万字符。')
    chunks=[]
    if options.chunker=='bge-semantic':
        if models is None:raise IngestionError('BGE_MODELS_REQUIRED','请先配置本地 BGE 模型服务。')
        chunks=semantic_chunks([b for _,b in blocks],models,max_tokens=options.max_tokens,min_tokens=options.min_tokens,similarity_threshold=options.semantic_threshold)
        for chunk in chunks:
            candidates=[(i,b) for i,b in blocks if (b.page,b.heading,b.kind,b.anchor)==(chunk['page'],chunk['heading'],chunk['kind'],chunk['anchor'])]
            chunk['locator']={'precision':'page_heading','block_indices':[i for i,_ in candidates],
                'line_start':min((b.line_start for _,b in candidates if b.line_start is not None),default=None),
                'line_end':max((b.line_end for _,b in candidates if b.line_end is not None),default=None)}
    else:
        for source_index,block in blocks:
            start=0
            while start<len(block.text):
                end=min(start+options.max_chars,len(block.text))
                if end<len(block.text):
                    # Prefer nearby paragraph/sentence boundaries without dropping text.
                    matches=list(re.finditer(r'[\n。！？.!?；;]',block.text[start+options.max_chars//2:end]))
                    if matches:end=start+options.max_chars//2+matches[-1].end()
                content=block.text[start:end]
                if content.strip():
                    chunks.append({'content':content,'page':block.page,'heading':block.heading,'kind':block.kind,'anchor':block.anchor,
                        'token_count':None,'content_hash':hashlib.sha256(content.encode()).hexdigest(),
                        'locator':{'precision':'normalized_block_offsets','block_indices':[source_index],
                            'char_start':start,'char_end':end,'line_start':block.line_start,'line_end':block.line_end}})
                if end==len(block.text):break
                start=max(start+1,end-options.overlap_chars)
    if len(chunks)>10_000:raise IngestionError('CHUNK_LIMIT','单个文件切片超过一万条。')
    if not chunks:raise IngestionError('EMPTY_DOCUMENT','未能生成有效切片。')
    return {'content':'\n\n'.join(b.text for _,b in blocks),'chunks':chunks,
        'stats':{'raw_blocks':len(raw_blocks),'clean_blocks':len(blocks),'duplicate_blocks_removed':dropped,
            'chunks':len(chunks),'pages':max((b.page or 0 for b in raw_blocks),default=0),
            'blank_pages':[b.page for b in raw_blocks if b.kind=='blank_page'],'unprocessed_pages':unprocessed}}
