"""Local-only model adapters; no fallback to random vectors or a chat reranker."""
import os
import httpx
import numpy as np
from smart_cs.observability.models import local_model
from smart_cs.retrieval.endpoints import validate_local_endpoint


class LocalModels:
    def __init__(self):
        self.embedding_url = os.getenv('RAG_EMBEDDING_URL', 'http://127.0.0.1:61535').rstrip('/')
        self.reranker_url = os.getenv('RAG_RERANKER_URL', 'http://127.0.0.1:61536').rstrip('/')
        self.model = os.getenv('RAG_EMBEDDING_MODEL', 'smart-cs-bge-m3')
        for url in [self.embedding_url, self.reranker_url]:
            validate_local_endpoint(url)

    @staticmethod
    def vectors(value, count):
        data = np.asarray(value, dtype=np.float32)
        if data.shape != (count, 1024) or not np.isfinite(data).all():
            raise ValueError('Embedding shape/value validation failed')
        norm = np.linalg.norm(data.astype(np.float64), axis=1, keepdims=True)
        if not np.isfinite(norm).all() or (norm <= 0).any():
            raise ValueError('Zero embedding vector')
        return (data / norm).astype(np.float32).tolist()

    def embedding_identity(self):
        with httpx.Client(trust_env=False,timeout=5) as client:
            response=client.get(self.embedding_url+'/api/tags');response.raise_for_status();models=response.json()['models']
        match=next((m for m in models if m['name'].removesuffix(':latest')==self.model.removesuffix(':latest')),None)
        if match is None or not match.get('digest'):raise RuntimeError('Configured embedding model is unavailable')
        return {'embedding':self.model+'@'+match['digest'],'dimensions':1024}

    def embed(self, texts):
        vectors = []
        with httpx.Client(trust_env=False, timeout=180) as client:
            for start in range(0, len(texts), 8):
                batch = texts[start:start+8]
                response = client.post(self.embedding_url + '/api/embed', json={
                    'model': self.model, 'input': batch, 'truncate': False, 'keep_alive': '30m'})
                response.raise_for_status()
                vectors.extend(self.vectors(response.json()['embeddings'], len(batch)))
        return vectors

    @local_model('embedding')
    async def aembed(self, texts):
        async with httpx.AsyncClient(trust_env=False, timeout=180) as client:
            response = await client.post(self.embedding_url + '/api/embed', json={
                'model': self.model, 'input': texts, 'truncate': False, 'keep_alive': '30m'})
            response.raise_for_status()
            return self.vectors(response.json()['embeddings'], len(texts))

    def tokenize(self, texts, max_tokens=300, overlap=0):
        with httpx.Client(trust_env=False, timeout=120) as client:
            response = client.post(self.reranker_url + '/tokenize', json={
                'texts': texts, 'max_tokens': max_tokens, 'overlap': overlap})
            response.raise_for_status()
            return response.json()['pieces']

    @local_model('reranker')
    async def arerank(self, query, passages):
        async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
            response = await client.post(self.reranker_url + '/rerank', json={'query': query, 'passages': passages})
            response.raise_for_status()
            scores = response.json()['scores']
            if len(scores) != len(passages) or not all(np.isfinite(s) for s in scores):
                raise ValueError('Reranker score validation failed')
            return scores

    def identity(self):
        with httpx.Client(trust_env=False, timeout=5) as client:
            health = client.get(self.reranker_url + '/health').json()
            models = client.get(self.embedding_url + '/api/tags').json()['models']
        match = next(m for m in models if m['name'].split(':')[0] == self.model)
        if not health.get('ready'):
            raise RuntimeError('Reranker not ready')
        return {'embedding': self.model + '@' + match['digest'], 'reranker': 'bge-reranker-v2-m3',
                'device': health['device'], 'dimensions': 1024}
