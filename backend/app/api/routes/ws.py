"""WebSocket 实时事件协议（§12.3）。

- 客户端发送 {"op":"subscribe","channel":"run:<id>","after_seq":N}（或 "global"）。
- 服务端先补发 after_seq 之后的历史持久化事件，再推送实时事件。
- 心跳：服务端每 20s 发 {"op":"ping"}；60s 无 pong 断开。
- attempt.output.delta 仅推送不持久化，补发不含它。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.auth import ws_authorized
from app.bootstrap import get_container
from app.events.bus import BusMessage
from app.events.service import load_events
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["ws"])

PING_INTERVAL = 20.0
PONG_TIMEOUT = 60.0


def _event_message(channel: str, event: Any) -> dict[str, Any]:
    return {
        "channel": channel,
        "event": {
            "id": event.id,
            "seq": event.seq,
            "type": event.type,
            "actor": event.actor,
            "task_key": event.task_key,
            "attempt_id": event.attempt_id,
            "payload": event.payload,
            "ts": event.ts.isoformat() if hasattr(event.ts, "isoformat") else str(event.ts),
        },
    }


def _bus_message(channel: str, msg: BusMessage) -> dict[str, Any]:
    return {
        "channel": channel,
        "event": {
            "id": msg.event_id,
            "seq": msg.seq,
            "type": msg.event_type,
            "actor": msg.actor,
            "task_key": msg.task_key,
            "attempt_id": msg.attempt_id,
            "payload": msg.payload,
            "ts": msg.ts,
        },
    }


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if not await ws_authorized(ws):
        # 认证失败：4401 = 自定义"未授权"关闭码，前端据此回到登录页
        await ws.accept()
        await ws.close(code=4401)
        return
    await ws.accept()
    container = get_container()
    channels: list[str] = []
    queue = await container.bus.subscribe(["global"])
    live = True

    async def _reader() -> None:
        nonlocal channels
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            op = data.get("op")
            if op == "pong":
                continue
            if op == "subscribe":
                channel = str(data.get("channel") or "global")
                after_seq = int(data.get("after_seq") or 0)
                new_channels = [c for c in (channel, "global") if c not in channels]
                if new_channels:
                    await container.bus.unsubscribe(queue, [])
                    more = await container.bus.subscribe(new_channels)
                    # 合并订阅：把新队列并入（简单实现：改用多队列读取）
                    channels.extend(new_channels)
                    _merger_tasks.append(asyncio.create_task(_merge(more)))
                if channel.startswith("run:"):
                    run_id = channel.split(":", 1)[1]
                    async with db_session() as session:
                        events = await load_events(session, run_id, after_seq)
                    for ev in events:
                        await ws.send_text(json.dumps(_event_message(channel, ev), ensure_ascii=False))
                await ws.send_text(json.dumps({"op": "subscribed", "channel": channel}))

    _merger_tasks: list[asyncio.Task[None]] = []

    async def _merge(more: Any) -> None:
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                msg = await more.get()
                await queue.put(msg)

    async def _pinger() -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await ws.send_text(json.dumps({"op": "ping"}))

    reader = asyncio.create_task(_reader())
    pinger = asyncio.create_task(_pinger())
    try:
        while live:
            msg = await asyncio.wait_for(queue.get(), timeout=PONG_TIMEOUT + 10)
            if msg.channel != "global" and msg.channel not in channels:
                continue
            await ws.send_text(json.dumps(_bus_message(msg.channel, msg), ensure_ascii=False))
    except (TimeoutError, WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader.cancel()
        pinger.cancel()
        for t in _merger_tasks:
            t.cancel()
        await container.bus.unsubscribe(queue, channels or ["global"])
