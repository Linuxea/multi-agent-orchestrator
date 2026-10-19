"""事件查询（回放与补发，§12.2、§12.3）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.events.service import load_events
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["events"])


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    after_seq: int = 0,
    types: list[str] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    async with db_session() as session:
        events = await load_events(session, run_id, after_seq, types)
        items = [
            {
                "id": e.id,
                "seq": e.seq,
                "type": e.type,
                "actor": e.actor,
                "task_key": e.task_key,
                "attempt_id": e.attempt_id,
                "payload": e.payload,
                "ts": e.ts.isoformat(),
            }
            for e in events[: min(limit, 1000)]
        ]
    return {"items": items, "next_cursor": items[-1]["seq"] if items else None}
