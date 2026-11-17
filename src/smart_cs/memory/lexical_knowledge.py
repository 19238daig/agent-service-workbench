"""Transparent lexical bootstrap retrieval; NOT a semantic embedding model.

Chinese character bigrams + Latin terms use BM25-style scoring. This small local
baseline replaces random vectors when loading phase-one database knowledge.
"""
import math
import re
from collections import Counter


def tokenize(text):
    terms = re.findall(r'[a-z0-9]+(?:[-_][a-z0-9]+)*', text.lower())
    for span in re.findall(r'[\u4e00-\u9fff]+', text):
        terms.extend(span[i:i+2] for i in range(len(span)-1))
        if len(span) == 1:
            terms.append(span)
    return terms


class LexicalKnowledgeMemory:
    retrieval_mode = 'lexical_bootstrap_bm25'

    def __init__(self, documents=()):
        self._documents = {}
        for document in documents:
            self.add_document(document['content'], document['source'], document.get('metadata'))

    def add_document(self, content, source='', metadata=None):
        self._documents[source or content] = {'content': content, 'source': source, 'metadata': metadata or {}}
        return source

    def search(self, query, top_k=5):
        documents = list(self._documents.values())
        if not documents or not query.strip():
            return []
        corpora = [Counter(tokenize(d['content'] + ' ' + d['source'])) for d in documents]
        lengths = [sum(c.values()) for c in corpora]
        average = sum(lengths) / len(corpora) or 1
        query_terms = set(tokenize(query))
        scores = []
        for doc, frequencies, length in zip(documents, corpora, lengths):
            score = 0
            for term in query_terms:
                tf = frequencies[term]
                if not tf:
                    continue
                df = sum(term in corpus for corpus in corpora)
                idf = math.log(1 + (len(corpora) - df + 0.5) / (df + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / average))
            if score > 0:
                scores.append({**doc, 'score': score, 'retrieval_mode': self.retrieval_mode})
        return sorted(scores, key=lambda d: (-d['score'], d['source']))[:top_k]
