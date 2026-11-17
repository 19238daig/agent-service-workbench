"""Loopback GPU reranker/tokenizer worker using the user's existing DL environment."""
import asyncio
import hashlib
import json
import os
from time import perf_counter
from typing import Literal
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import torch
import transformers
from transformers import AutoModelForSequenceClassification, AutoTokenizer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_PATH = os.environ['RAG_RERANKER_PATH']
tokenizer = None
model = None
lock = Lock()
identity_record = None
asset_stats = {}
device = 'cuda' if torch.cuda.is_available() else 'cpu'


def load():
    global model, tokenizer
    torch.set_num_threads(4)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH, local_files_only=True, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH, local_files_only=True,
        trust_remote_code=False, torch_dtype=torch.float16 if device == 'cuda' else torch.float32)
    model.to(device)
    model.eval()
    global identity_record, asset_stats
    root = Path(MODEL_PATH).resolve()
    files = sorted(set([*root.glob('*.json'), *root.glob('*.safetensors'), *root.glob('pytorch_model*.bin'), *root.glob('*.model')]))
    if not any(p.suffix in {'.safetensors', '.bin'} for p in files):
        raise RuntimeError('Local reranker weights missing')
    digests = {}
    for path in files:
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(8*1024*1024), b''):
                h.update(block)
        digests[path.name] = h.hexdigest()
    asset_stats = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in files}
    contract = {'protocol': 'bge-cross-encoder-v2', 'model': 'bge-reranker-v2-m3',
                'files_sha256': digests, 'server_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'torch': torch.__version__, 'transformers': transformers.__version__,
                'device': device, 'dtype': str(next(model.parameters()).dtype),
                'normalization': 'sigmoid-float32-logits', 'truncation': 'longest_first',
                'local_files_only': True, 'trust_remote_code': False}
    digest = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    identity_record = {'sha256': digest, 'contract': contract}


@asynccontextmanager
async def lifespan(_app):
    await asyncio.to_thread(load)
    yield


app = FastAPI(title='smart-cs-local-reranker', lifespan=lifespan)


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    passages: list[str] = Field(min_length=1, max_length=64)


class TokenizeRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=512)
    max_tokens: int = Field(default=300, ge=32, le=512)
    overlap: int = Field(default=0, ge=0, le=64)


class RerankV2Request(RerankRequest):
    max_length: Literal[256, 512] = 512
    batch_size: Literal[1, 4] = 4
    expected_identity: str = Field(pattern=r'^[a-f0-9]{64}$')


def identity():
    if identity_record is None:
        raise HTTPException(503, 'Reranker identity not ready')
    if any(not p.is_file() or (p.stat().st_size, p.stat().st_mtime_ns) != stats for p, stats in asset_stats.items()):
        raise HTTPException(409, 'Local model files changed; restart and verify identity')
    return identity_record


@app.get('/identity')
def model_identity():
    return identity()


@app.post('/rerank/v2')
def rerank_v2(request: RerankV2Request):
    contract = identity()
    if contract['sha256'] != request.expected_identity:
        raise HTTPException(409, 'Reranker identity mismatch')
    if any(len(p) > 12000 for p in request.passages):
        raise HTTPException(413, 'Passage exceeds limit')
    scores, lengths = [], []
    started = perf_counter()
    with lock, torch.inference_mode():
        for start in range(0, len(request.passages), request.batch_size):
            batch = [[request.query, text] for text in request.passages[start:start+request.batch_size]]
            lengths.extend(len(tokens) for tokens in tokenizer(batch, truncation=False)['input_ids'])
            inputs = tokenizer(batch, padding=True, truncation='longest_first', max_length=request.max_length,
                               return_tensors='pt').to(device)
            logits = model(**inputs, return_dict=True).logits.view(-1).float()
            scores.extend(torch.sigmoid(logits).cpu().tolist())
    identity()
    return {'scores': scores, 'identity_sha256': contract['sha256'], 'normalized': True,
            'max_length': request.max_length, 'batch_size': request.batch_size,
            'pairs': len(scores), 'input_pair_tokens_before_truncation': lengths,
            'truncated_pairs': sum(n > request.max_length for n in lengths),
            'duration_ms': (perf_counter()-started)*1000}


@app.get('/health')
def health():
    return {'service': 'smart-cs-local-reranker', 'ready': model is not None,
            'model': 'bge-reranker-v2-m3', 'device': device,
            'torch': torch.__version__, 'local_files_only': True}


@app.post('/tokenize')
def tokenize(request: TokenizeRequest):
    if request.overlap >= request.max_tokens:
        raise HTTPException(422, 'overlap must be smaller than max_tokens')
    results = []
    for text in request.texts:
        if len(text) > 100000:
            raise HTTPException(413, 'Text exceeds limit')
        encoded = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        offsets = encoded['offset_mapping']
        pieces = []
        for start in range(0, max(len(offsets), 1), request.max_tokens-request.overlap):
            if not offsets:
                break
            stop = min(start+request.max_tokens, len(offsets))
            left = 0 if start == 0 else offsets[start][0]
            right = len(text) if stop == len(offsets) else offsets[stop-1][1]
            pieces.append({'text': text[left:right], 'tokens': stop-start, 'start': left, 'end': right})
            if stop == len(offsets):
                break
        results.append(pieces)
    return {'pieces': results, 'tokenizer': 'bge-reranker-v2-m3/xlm-roberta'}


@app.post('/rerank')
def rerank(request: RerankRequest):
    if any(len(p) > 12000 for p in request.passages):
        raise HTTPException(413, 'Passage exceeds limit')
    scores = []
    with lock, torch.inference_mode():
        for start in range(0, len(request.passages), 4):
            batch = [[request.query, text] for text in request.passages[start:start+4]]
            inputs = tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors='pt').to(device)
            logits = model(**inputs, return_dict=True).logits.view(-1).float()
            scores.extend(torch.sigmoid(logits).cpu().tolist())
    return {'scores': scores, 'model': 'bge-reranker-v2-m3', 'device': device, 'normalized': True}


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.getenv('RAG_RERANKER_PORT', '61536')), log_level='warning')
