"""Retrieval hybride : dense (Qdrant) + lexical (BM25) fusionnés par RRF, filtrés par organisation."""
from __future__ import annotations

import math
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any

from app.rag.embedding import _TOKEN, normalize

STOPWORDS = set(
    "le la les un une des de du d l et ou a à au aux en dans pour par sur avec sans ce cette ces est sont être "
    "je tu il elle nous vous ils elles on me te se mon ma mes ton ta tes son sa ses notre votre leur leurs qui que "
    "quoi quel quelle quels quelles comment est-ce ne pas plus y ai as avez avons ont fait faire peux peut pouvez "
    "the a an of to in is are for on and or what how do does can you your".split()
)


def terms(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(normalize(text)) if t not in STOPWORDS and len(t) > 1]


@dataclass
class LexDoc:
    id: str
    tf: Counter[str]
    length: int
    payload: dict[str, Any]


@dataclass
class BM25Index:
    k1: float = 1.4
    b: float = 0.72
    docs: dict[str, LexDoc] = field(default_factory=dict)
    df: Counter[str] = field(default_factory=Counter)
    total_len: int = 0

    def add(self, doc_id: str, text: str, payload: dict[str, Any]) -> None:
        if doc_id in self.docs:
            self.remove(doc_id)
        tf = Counter(terms(text))
        self.docs[doc_id] = LexDoc(doc_id, tf, sum(tf.values()), payload)
        self.df.update(tf.keys())
        self.total_len += sum(tf.values())

    def remove(self, doc_id: str) -> None:
        d = self.docs.pop(doc_id, None)
        if d:
            self.df.subtract(d.tf.keys())
            self.total_len -= d.length

    def search(self, query: str, top_k: int, kb_ids: list[str] | None = None) -> list[tuple[str, float, int]]:
        """Retourne (id, score, nb de termes de la requête présents)."""
        q = set(terms(query))
        if not q or not self.docs:
            return []
        n = len(self.docs)
        avgdl = self.total_len / n if n else 1.0
        out: list[tuple[str, float, int]] = []
        for d in self.docs.values():
            if kb_ids and d.payload.get("knowledge_base_id") not in kb_ids:
                continue
            score, matched = 0.0, 0
            for t in q:
                f = d.tf.get(t)
                if not f:
                    continue
                matched += 1
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * d.length / avgdl))
            if score > 0:
                out.append((d.id, score, matched))
        out.sort(key=lambda x: -x[1])
        return out[:top_k]


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


class TTLCache:
    def __init__(self, max_items: int = 2048, ttl_s: float = 600.0) -> None:
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.max_items = max_items
        self.ttl_s = ttl_s

    def get(self, key: Any) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        ts, value = item
        if time.monotonic() - ts > self.ttl_s:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def invalidate_prefix(self, prefix: Any) -> None:
        for key in [k for k in self._data if isinstance(k, tuple) and k[0] == prefix]:
            self._data.pop(key, None)
