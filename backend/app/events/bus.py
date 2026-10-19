"""进程内事件总线：持久化后广播给订阅者（WebSocket / CLI）。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from app.ids import new_ulid


@dataclass(frozen=True)
class BusMessage:
    """总线消息：channel 形如 run:<run_id> 或 global。"""

    channel: str
    event_type: str
    run_id: str | None
    seq: int | None
    actor: str
    task_key: str | None
    attempt_id: str | None
    payload: dict[str, Any]
    ts: str
    event_id: str = field(default_factory=new_ulid)


Listener = asyncio.Queue[BusMessage]


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[str, set[Listener]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, channels: list[str]) -> Listener:
        q: Listener = asyncio.Queue(maxsize=10000)
        async with self._lock:
            for ch in channels:
                self._listeners[ch].add(q)
        return q

    async def unsubscribe(self, q: Listener, channels: list[str]) -> None:
        async with self._lock:
            for ch in channels:
                self._listeners[ch].discard(q)
                if not self._listeners[ch]:
                    del self._listeners[ch]

    def publish(self, msg: BusMessage) -> None:
        """同步发布（在事件持久化事务提交后调用）。队列满则丢弃最旧，保实时不阻塞。"""
        targets: set[Listener] = set()
        for ch in (msg.channel, "global"):
            targets |= self._listeners.get(ch, set())
        for q in targets:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except asyncio.QueueEmpty:
                    pass
