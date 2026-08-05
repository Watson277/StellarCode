from __future__ import annotations

import logging
import re


ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.$-]{1,}")
HAN_SEQUENCE = re.compile(r"[\u4e00-\u9fff]{2,}")
STOPWORDS = {
    "怎么",
    "如何",
    "什么",
    "哪些",
    "一下",
    "实现",
    "的是",
    "一个",
    "可以",
    "这里",
    "那里",
}


def tokenize_query(query: str | None) -> list[str]:
    if not query or not query.strip():
        return []
    normalized = query.strip()
    candidates: list[str] = []
    try:
        import jieba

        jieba.setLogLevel(logging.WARNING)
        candidates.extend(jieba.lcut(normalized, cut_all=False))
    except ModuleNotFoundError:
        candidates.extend(HAN_SEQUENCE.findall(normalized))
    candidates.extend(ASCII_TOKEN.findall(normalized))

    tokens: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        token = candidate.strip()
        lowered = token.lower()
        if len(token) < 2 or lowered in STOPWORDS or lowered in seen:
            continue
        if not (ASCII_TOKEN.fullmatch(token) or HAN_SEQUENCE.fullmatch(token)):
            continue
        seen.add(lowered)
        tokens.append(token)
    return tokens
