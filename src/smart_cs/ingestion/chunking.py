"""Hard structural boundaries plus local BGE-M3 semantic paragraph boundaries."""
import hashlib
import numpy as np
from smart_cs.ingestion.parsers import Block


def semantic_chunks(blocks, models, max_tokens=360, min_tokens=100, similarity_threshold=0.65):
    pieces = []
    # Tables are split by rows, repeating their column headers in every fragment.
    for block in blocks:
        if not block.text.strip():continue
        if block.kind == 'table':
            rows = block.text.splitlines()
            values = [rows[0] + '\n' + row for row in rows[1:]] or [block.text]
        else:
            values = [block.text]
        tokenized = models.tokenize(values, max_tokens=max_tokens-40, overlap=0)
        for fragments in tokenized:
            for fragment in fragments:
                pieces.append({'text': fragment['text'], 'tokens': fragment['tokens'], 'page': block.page,
                               'heading': block.heading, 'kind': block.kind, 'anchor': block.anchor})
    if not pieces:
        raise ValueError('No content to chunk')
    vectors = models.embed([p['text'] for p in pieces])
    chunks, buffer = [], []
    def flush():
        if buffer:
            first = buffer[0]
            chunks.append({**{k:first[k] for k in ['page','heading','kind','anchor']},
                           'content':'\n'.join(p['text'] for p in buffer),
                           'token_count':sum(p['tokens'] for p in buffer)})
            buffer.clear()
    for i, piece in enumerate(pieces):
        count = sum(p['tokens'] for p in buffer)
        boundary = buffer and any(buffer[0][key] != piece[key] for key in ['page','heading','kind','anchor'])
        similarity = float(np.dot(vectors[i-1], vectors[i])) if i else 1
        if boundary or count + piece['tokens'] > max_tokens or (count >= min_tokens and similarity < similarity_threshold):
            flush()
        buffer.append(piece)
    flush()
    for chunk in chunks:
        chunk['content_hash'] = hashlib.sha256(chunk['content'].encode()).hexdigest()
    return chunks
