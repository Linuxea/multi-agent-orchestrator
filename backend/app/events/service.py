"""事件服务：校验载荷、分配运行内序号、持久化并在事务提交后广播。

可靠性（§13）：事件写入与状态更新在同一数据库事务 —— emit 只写 session，
广播挂到 after_commit；回滚后不广播。
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.bus import BusMessage, EventBus
from app.ids import new_id
from app.models import Event
from app.schemas.events import EVENT_PAYLOAD_MODELS
from app.utils import iso, utcnow

# session -> 待广播消息（弱引用：会话被回收时条目自动消失，防泄漏）
_REGISTRY: weakref.WeakKeyDictionary[AsyncSession, list[BusMessage]] = (
    weakref.WeakKeyDictionary()
)


def _registry_of(session: AsyncSession, bus: EventBus) -> list[BusMessage]:
    pending = _REGISTRY.get(session)
    if pending is None:
        pending = []
        _REGISTRY[session] = pending

        @sa_event.listens_for(session.sync_session, "after_commit")
        def _publish(_s: object, _pending: list[BusMessage] = pending, _bus: EventBus = bus) -> None:
            for msg in _pending:
                _bus.publish(msg)
            _pending.clear()

        @sa_event.listens_for(session.sync_session, "after_rollback")
        def _drop(_s: object, _pending: list[BusMessage] = pending) -> None:
            _pending.clear()

    return pending


class EventService:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._seq: dict[str, int] = {}
        self._seq_lock = asyncio.Lock()

    async def _next_seq(self, session: AsyncSession, run_id: str | None) -> int | None:
        if run_id is None:
            return None
        async with self._seq_lock:
            if run_id not in self._seq:
                result = await session.execute(
                    select(func.max(Event.seq)).where(Event.run_id == run_id)
                )
                current = result.scalar_one_or_none() or 0
                self._seq[run_id] = current
            self._seq[run_id] += 1
            return self._seq[run_id]

    def reset_seq_cache(self, run_id: str | None = None) -> None:
        """服务重启/测试重置时清空内存序号缓存。"""
        if run_id is None:
            self._seq.clear()
        else:
            self._seq.pop(run_id, None)

    async def emit(
        self,
        session: AsyncSession,
        *,
        type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        run_id: str | None = None,
        task_key: str | None = None,
        attempt_id: str | None = None,
        persist: bool = True,
    ) -> Event:
        """写入一条事件。persist=False 用于仅推送型（attempt.output.delta，§12.3）。"""
        payload = dict(payload or {})
        schema = EVENT_PAYLOAD_MODELS.get(type)
        if schema is not None:
            payload = schema(**payload).model_dump()

        seq = await self._next_seq(session, run_id if persist else None)
        ev = Event(
            id=new_id("evt"),
            run_id=run_id,
            seq=seq,
            type=type,
            actor=actor,
            task_key=task_key,
            attempt_id=attempt_id,
            payload=payload,
            ts=utcnow(),
        )
        if persist:
            session.add(ev)

        msg = BusMessage(
            channel=f"run:{run_id}" if run_id else "global",
            event_type=type,
            run_id=run_id,
            seq=seq,
            actor=actor,
            task_key=task_key,
            attempt_id=attempt_id,
            payload=payload,
            ts=iso(ev.ts),
            event_id=ev.id,
        )
        pending: list[BusMessage] | None = None
        if persist:
            pending = _registry_of(session, self.bus)
        if persist and pending is not None:
            pending.append(msg)
        else:
            self.bus.publish(msg)
        return ev


async def load_events(
    session: AsyncSession, run_id: str, after_seq: int = 0, types: list[str] | None = None
) -> list[Event]:
    stmt = select(Event).where(Event.run_id == run_id, Event.seq > after_seq)
    if types:
        stmt = stmt.where(Event.type.in_(types))
    stmt = stmt.order_by(Event.seq)
    result = await session.execute(stmt)
    return list(result.scalars().all())
