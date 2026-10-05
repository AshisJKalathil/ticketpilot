"""Knowledge-base retrieval (BM25 over policy sections).

Small, dependency-free and deterministic. The KB hash feeds the answer-cache
namespace so editing a policy invalidates cached answers.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

KB_DIR = Path(__file__).resolve().parents[1] / "data" / "kb"
_TOKEN = re.compile(r"[a-z0-9$]+")


@dataclass(frozen=True)
class Section:
    doc: str
    section: str
    text: str
    tokens: tuple[str, ...]


def _tokens(text: str) -> list[str]:
    return [t[:-1] if len(t) > 4 and t.endswith("s") else t for t in _TOKEN.findall(text.lower())]


@cache
def load_sections(kb_dir: Path = KB_DIR) -> tuple[Section, ...]:
    sections = []
    for path in sorted(kb_dir.glob("*.md")):
        current, buf = "Overview", []
        title = path.stem
        for line in path.read_text().splitlines() + ["## __end__"]:
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                body = "\n".join(buf).strip().replace("**", "")
                if body:
                    sections.append(Section(title, current, body, tuple(_tokens(f"{current} {body}"))))
                current, buf = line[3:].strip(), []
            else:
                buf.append(line)
    return tuple(sections)


@cache
def kb_hash(kb_dir: Path = KB_DIR) -> str:
    h = hashlib.sha256()
    for path in sorted(kb_dir.glob("*.md")):
        h.update(path.read_bytes())
    return h.hexdigest()[:12]


def search(query: str, top_k: int = 3, k1: float = 1.5, b: float = 0.75) -> list[dict]:
    sections = load_sections()
    n = len(sections)
    avgdl = sum(len(s.tokens) for s in sections) / n
    df = Counter(t for s in sections for t in set(s.tokens))
    q = _tokens(query)
    scored = []
    for s in sections:
        tf = Counter(s.tokens)
        score = 0.0
        for t in q:
            if t not in tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(s.tokens) / avgdl))
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [{"doc": s.doc, "section": s.section, "text": s.text, "score": round(sc, 3)} for sc, s in scored[:top_k]]
