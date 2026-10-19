"""事件派生视图（§12.5）：仅凭事件流重建运行状态。

回放与一致性属性测试的基础；与物化状态表由同一事件序列驱动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.enums import AttemptStatus, RunStatus, TaskStatus
from app.events.service import load_events


@dataclass
class ReplayedTask:
    key: str
    title: str = ""
    agent_slug: str = ""
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.pending


@dataclass
class ReplayedAttempt:
    id: str
    task_key: str
    number: int = 0
    agent_slug: str = ""
    status: str = "queued"


@dataclass
class ReplayedRun:
    run_id: str
    status: RunStatus = RunStatus.created
    tasks: dict[str, ReplayedTask] = field(default_factory=dict)
    attempts: dict[str, ReplayedAttempt] = field(default_factory=dict)
    plan_versions: list[int] = field(default_factory=list)


async def replay_run(session: Any, run_id: str) -> ReplayedRun:
    """从事件流重建运行状态（只读，不触发任何执行）。"""
    replayed = ReplayedRun(run_id=run_id)
    events = await load_events(session, run_id)
    for ev in events:
        _apply(replayed, ev.type, ev.payload or {}, ev.task_key, ev.attempt_id)
    return replayed


def _apply(
    replayed: ReplayedRun,
    etype: str,
    payload: dict[str, Any],
    task_key: str | None,
    attempt_id: str | None,
) -> None:
    if etype == "task.created":
        key = payload.get("key") or task_key or ""
        replayed.tasks[key] = ReplayedTask(
            key=key,
            title=payload.get("title", ""),
            agent_slug=payload.get("agent_slug", ""),
            depends_on=list(payload.get("depends_on") or []),
        )
    elif etype == "task.status_changed":
        key = payload.get("key") or task_key or ""
        task = replayed.tasks.get(key)
        if task is not None:
            task.status = TaskStatus(payload.get("to_status", task.status.value))
    elif etype == "task.reassigned":
        key = payload.get("key") or task_key or ""
        task = replayed.tasks.get(key)
        if task is not None:
            task.agent_slug = payload.get("to_agent", task.agent_slug)
    elif etype == "run.status_changed":
        replayed.status = RunStatus(payload.get("to_status", replayed.status.value))
    elif etype in ("attempt.queued", "attempt.started"):
        key = payload.get("task_key") or task_key or ""
        aid = attempt_id or ""
        att = replayed.attempts.get(aid)
        if att is None and aid:
            att = ReplayedAttempt(id=aid, task_key=key)
            replayed.attempts[aid] = att
        if att is not None:
            att.task_key = key or att.task_key
            att.number = int(payload.get("number", att.number) or 0)
            att.agent_slug = payload.get("agent_slug", att.agent_slug)
            att.status = "running" if etype == "attempt.started" else "queued"
    elif etype in ("attempt.reported", "attempt.crashed", "attempt.timed_out", "attempt.cancelled"):
        aid = attempt_id or ""
        att = replayed.attempts.get(aid)
        if att is not None:
            status_map = {
                "attempt.reported": AttemptStatus.reported.value,
                "attempt.crashed": AttemptStatus.crashed.value,
                "attempt.timed_out": AttemptStatus.timed_out.value,
                "attempt.cancelled": AttemptStatus.cancelled.value,
            }
            att.status = status_map[etype]
    elif etype in ("plan.proposed", "plan.revised"):
        replayed.plan_versions.append(int(payload.get("version", 0) or 0))
