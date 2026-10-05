"""Cache repeat queries so they cost almost nothing.

Three layers, each safe for a different kind of repetition:

1. **Provider prompt caching** (see ``llm/anthropic_provider.py``): the tool
   list + system prompt prefix is identical for every ticket, so repeated
   calls read it at ~5-10% of the input price.
2. **Answer cache** (here): general questions with *no account-specific
   content* ("what's your return policy?") are answered once by the model;
   near-duplicates afterwards skip the model entirely ($0) and only need the
   human's one-click approval. Entries are namespaced by prompt version +
   knowledge-base hash, so editing either invalidates them automatically, and a
   thumbs-down on the source run evicts its entry.
3. **Tool-result cache** (here): read-only tools (policy search, tracking)
   are memoised with a short TTL, scoped to the customer.

The answer cache uses lexical similarity to stay dependency-free; swap
``similarity`` for embedding cosine similarity at scale.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from ticketpilot.store import Store, dumps

STOPWORDS = set(["a", "an", "the", "is", "are", "was", "were", "be", "been", "am", "i", "me", "my", "mine", "you", "your", "yours", "we", "our", "us", "it", "its", "this", "that", "these", "those", "what", "whats", "which", "who", "whom", "how", "when", "where", "why", "if", "then", "than", "so", "do", "does", "did", "doing", "don", "dont", "doesn", "didn", "can", "could", "would", "should", "will", "shall", "may", "might", "must", "to", "of", "in", "on", "at", "by", "for", "with", "about", "from", "into", "over", "under", "up", "down", "out", "off", "and", "or", "but", "not", "no", "yes", "please", "thanks", "thank", "hi", "hello", "hey", "quick", "question", "just", "any", "some", "there", "their", "they", "them", "have", "has", "had", "having", "get", "got", "also", "too", "very", "really", "much", "many", "more", "most", "own", "same", "such", "only", "s", "t"])

PLACEHOLDER = re.compile(r"<[A-Z]+_\d+>")


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def normalize(text: str) -> list[str]:
    words = re.findall(r"[a-z]+", PLACEHOLDER.sub(" ", text.lower()))
    return sorted({_stem(w) for w in words if len(w) > 2 and w not in STOPWORDS})


def similarity(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


class AnswerCache:
    def __init__(self, store: Store, namespace: str, *, threshold: float = 0.8, ttl_s: float = 86_400):
        self.store = store
        self.namespace = namespace
        self.threshold = threshold
        self.ttl_s = ttl_s

    def lookup(self, text: str) -> dict[str, Any] | None:
        tokens = normalize(text)
        if len(tokens) < 2:
            return None
        rows = self.store.query(
            "SELECT id, normalized, answer_json FROM semantic_cache WHERE namespace=? AND created_at>=?",
            (self.namespace, time.time() - self.ttl_s),
        )
        best, best_sim = None, 0.0
        for row in rows:
            sim = similarity(tokens, row["normalized"].split())
            if sim > best_sim:
                best, best_sim = row, sim
        if best is None or best_sim < self.threshold:
            return None
        self.store.execute("UPDATE semantic_cache SET hits = hits + 1 WHERE id=?", (best["id"],))
        return {"entry_id": best["id"], "similarity": round(best_sim, 3), **json.loads(best["answer_json"])}

    def put(self, text: str, answer: dict[str, Any]) -> None:
        tokens = normalize(text)
        if len(tokens) < 2:
            return
        self.store.execute(
            "INSERT INTO semantic_cache(namespace, normalized, answer_json, created_at) VALUES(?,?,?,?)",
            (self.namespace, " ".join(tokens), json.dumps(answer), time.time()),
        )

    @staticmethod
    def evict_run(store: Store, run_id: str) -> int:
        return store.execute("DELETE FROM semantic_cache WHERE json_extract(answer_json, '$.source_run') = ?",
                             (run_id,))


class ToolCache:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def key(tool: str, args: dict, scope: str) -> str:
        return hashlib.sha256(f"{tool}|{scope}|{dumps(args)}".encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        row = self.store.one("SELECT output_json, expires_at FROM tool_cache WHERE key=?", (key,))
        if row and row["expires_at"] > time.time():
            return json.loads(row["output_json"])
        return None

    def put(self, key: str, output: dict, ttl_s: float) -> None:
        self.store.execute("INSERT OR REPLACE INTO tool_cache(key, output_json, expires_at) VALUES(?,?,?)",
                           (key, json.dumps(output), time.time() + ttl_s))
