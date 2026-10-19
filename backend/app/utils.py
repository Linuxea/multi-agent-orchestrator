"""通用工具：时间、token 估算、文本相似度。"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def estimate_tokens(text: str) -> int:
    """粗略 token 估算（中文混合场景 ~2.5 字符/token），仅用于阈值判断。"""
    if not text:
        return 0
    return max(1, len(text) // 3)


def text_similarity(a: str, b: str) -> float:
    """死循环防护用的文本相似度（D-1.12）。"""
    import difflib

    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()
