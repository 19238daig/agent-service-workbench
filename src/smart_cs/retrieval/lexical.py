"""Deterministic Chinese/Latin tokens and per-authorized-corpus BM25 statistics."""
import hashlib
import importlib.metadata
import json
import logging
from functools import lru_cache
from pathlib import Path
import re
import unicodedata
import jieba
from rank_bm25 import BM25Plus

RESOURCES=Path(__file__).parent/'resources'
PROTOCOL='jieba-search-hmm-off-nfkc-v1'


def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


class ChineseTokenizer:
    def __init__(self):
        jieba.setLogLevel(logging.WARNING)
        self.engine=jieba.Tokenizer();self.engine.initialize()
        self.engine.load_userdict(str(RESOURCES/'jieba_userdict_v1.txt'))
        self.stopwords=frozenset((RESOURCES/'stopwords_v1.txt').read_text(encoding='utf-8').split())
        self.identity={'protocol':PROTOCOL,'jieba':importlib.metadata.version('jieba'),'rank_bm25':importlib.metadata.version('rank-bm25'),
            'default_dictionary_sha256':hashlib.sha256((Path(jieba.__file__).parent/'dict.txt').read_bytes()).hexdigest(),
            'user_dictionary_sha256':hashlib.sha256((RESOURCES/'jieba_userdict_v1.txt').read_bytes()).hexdigest(),
            'stopwords_sha256':hashlib.sha256((RESOURCES/'stopwords_v1.txt').read_bytes()).hexdigest(),
            'normalization':'NFKC+casefold','english_stemming':False,'identifier_policy':'preserve-latin-digit-hyphen-dot-underscore-v1',
            'document_text':'title-heading-content-v1','scoring':'rank_bm25.BM25Plus(delta=0); idf=log((N+1)/df)'}
        self.fingerprint=fingerprint(self.identity)

    def tokenize(self,text):
        normalized=unicodedata.normalize('NFKC',text).casefold();tokens=[]
        for span in re.findall(r'[a-z0-9]+(?:[-_.][a-z0-9]+)*|[\u3400-\u9fff]+',normalized):
            pieces=self.engine.cut_for_search(span,HMM=False) if re.search(r'[\u3400-\u9fff]',span) else [span]
            tokens.extend(p for p in pieces if p.strip() and p not in self.stopwords)
        return tokens


@lru_cache(maxsize=1)
def tokenizer():return ChineseTokenizer()


class LexicalSnapshot:
    """Only already-authorized documents may enter this object, including IDF."""
    def __init__(self,documents):
        self.documents=sorted(documents,key=lambda d:d['id']);self.tokenizer=tokenizer()
        self.tokens=[self.tokenizer.tokenize(d.get('title','')+'\n'+(d['metadata'].get('heading') or '')+'\n'+d['content']) for d in self.documents]
        self.term_sets=[frozenset(t) for t in self.tokens]
        self.signature=fingerprint({'tokenizer':self.tokenizer.fingerprint,'members':[(d['id'],hashlib.sha256(d['content'].encode()).hexdigest(),d['metadata']) for d in self.documents]})
        self._indices={}

    def search(self,query,limit,k1=1.5,b=.75):
        terms=self.tokenizer.tokenize(query)
        if not terms or not any(self.tokens):return []
        key=(k1,b)
        if key not in self._indices:self._indices[key]=BM25Plus(self.tokens,k1=k1,b=b,delta=0)
        scores=self._indices[key].get_scores(terms);query_set=frozenset(terms)
        # No-match documents never receive a sparse rank, even with broad queries.
        candidates=[{**doc,'bm25_score':float(score)} for doc,score,present in zip(self.documents,scores,self.term_sets) if query_set&present]
        return sorted(candidates,key=lambda d:(-d['bm25_score'],d['id']))[:limit]
