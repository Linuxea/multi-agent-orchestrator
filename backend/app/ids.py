"""ULID 生成（规格 §4：所有实体 id 为 ULID 字符串）。

自实现以避免额外依赖：48bit 毫秒时间戳 + 80bit 随机，Crockford base32 编码，
同一毫秒内单调递增，保证字典序可排序。
"""

from __future__ import annotations

import os
import threading
import time

_ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOCK = threading.Lock()
_LAST_TS = 0
_LAST_RAND = 0


def _encode(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ENCODING[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成单调递增的 ULID 字符串（26 字符）。"""
    global _LAST_TS, _LAST_RAND
    with _LOCK:
        ts = int(time.time() * 1000)
        if ts == _LAST_TS:
            _LAST_RAND += 1
        else:
            _LAST_TS = ts
            # 80bit 随机数，避开 os.urandom 开销之外还要保证进程内唯一
            _LAST_RAND = int.from_bytes(os.urandom(10), "big")
        rand = _LAST_RAND & ((1 << 80) - 1)
        return _encode(ts, 10) + _encode(rand, 16)


def new_id(prefix: str) -> str:
    """带实体前缀的 ULID，便于日志阅读：如 run_01J...、task_01J...。"""
    return f"{prefix}_{new_ulid()}"
