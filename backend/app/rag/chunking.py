"""Chunking sémantique : respecte titres/sections, garde les paires Q/R intactes, ne coupe pas les tableaux/listes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
QA_START = re.compile(r"^\s*(?:Q\s*[:.)-]|Question\s*[:.)-]|\*\*.+\?\*\*$|.+\?$)", re.IGNORECASE)
WORD = re.compile(r"\w+", re.UNICODE)


def count_tokens(text: str) -> int:
    """Approximation rapide (≈1.3 token / mot en français)."""
    return int(len(WORD.findall(text)) * 1.3) + 1


@dataclass
class ChunkDraft:
    content: str
    section: str = ""
    kind: str = "text"  # text | qa
    metadata: dict = field(default_factory=dict)


@dataclass
class _Block:
    text: str
    section: str
    kind: str


def _blocks(text: str) -> list[_Block]:
    section_path: list[str] = []
    blocks: list[_Block] = []
    paragraphs = re.split(r"\n\s*\n", text)
    i = 0
    while i < len(paragraphs):
        para = paragraphs[i].strip()
        i += 1
        if not para:
            continue
        first_line = para.split("\n", 1)[0]
        if m := HEADING.match(first_line):
            level = len(m.group(1))
            section_path = section_path[: level - 1] + [m.group(2).strip()]
            rest = para.split("\n", 1)[1].strip() if "\n" in para else ""
            if not rest:
                continue
            para = rest
            first_line = para.split("\n", 1)[0]
        section = " > ".join(section_path)
        # Paire Q/R : question sur une ligne + réponse (même paragraphe ou suivant)
        if QA_START.match(first_line) and len(first_line) < 300:
            if "\n" not in para and i < len(paragraphs) and not HEADING.match(paragraphs[i].strip().split("\n", 1)[0]):
                para = para + "\n" + paragraphs[i].strip()
                i += 1
            blocks.append(_Block(para, section, "qa"))
        else:
            blocks.append(_Block(para, section, "text"))
    return blocks


def _split_long(text: str, max_tokens: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out, cur = [], ""
    for s in sentences:
        if cur and count_tokens(cur + " " + s) > max_tokens:
            out.append(cur.strip())
            cur = s
        else:
            cur = f"{cur} {s}" if cur else s
    if cur.strip():
        out.append(cur.strip())
    return out


def chunk_text(text: str, target_tokens: int = 400, max_tokens: int = 600, overlap_tokens: int = 60) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    buf: list[str] = []
    buf_section = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            drafts.append(ChunkDraft("\n\n".join(buf), buf_section, "text"))
            tail = buf[-1]
            buf = [tail] if count_tokens(tail) <= overlap_tokens * 2 and len(buf) > 1 else []

    for block in _blocks(text):
        if block.kind == "qa":
            flush()
            buf = []
            drafts.append(ChunkDraft(block.text, block.section, "qa"))
            continue
        if block.section != buf_section:
            flush()
            buf = []
            buf_section = block.section
        pieces = _split_long(block.text, max_tokens) if count_tokens(block.text) > max_tokens else [block.text]
        for piece in pieces:
            if buf and count_tokens("\n\n".join(buf + [piece])) > target_tokens:
                flush()
            buf.append(piece)
    flush()

    for idx, d in enumerate(drafts):
        d.metadata = {"section": d.section, "kind": d.kind, "position": idx, "tokens": count_tokens(d.content)}
        if d.section and not d.content.startswith(d.section):
            d.content = f"[{d.section}]\n{d.content}"
    return drafts
