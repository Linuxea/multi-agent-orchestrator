"""E2E happy path：多领域调研场景（YAML 剧本驱动）。

覆盖：计划→派发→执行→复核→依赖解锁→汇总→引用，全部事件留痕。
"""

from __future__ import annotations

from pathlib import Path as _P

import pytest
from sqlalchemy import select

from app.models import Artifact
from app.orchestrator.helpers import db_session
from tests.conftest import Harness

pytestmark = pytest.mark.asyncio

SCRIPT = str((_P(__file__).parent.parent / "scripts" / "multi_domain_research.yaml").resolve())


async def test_multi_domain_research_happy_path(platform: Harness) -> None:
    h = platform
    await h.agent("web-researcher", "市场调研员", prompt="你是网络调研员（测试）。", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    await h.agent("writer", "撰稿人", prompt="你是撰稿人（测试）。", budget={"max_turns": 6, "max_cost": 0.1, "max_wall_seconds": 30})
    h.load_yaml(SCRIPT)

    run_id, _session_id = await h.start("调研并写一份简报")
    assert await h.wait(run_id, timeout=30), "运行未在时限内静止"

    run = await h.run_row(run_id)
    assert run.status.value == "completed", f"运行未完成：{run.status.value}"
    assert run.outcome and run.outcome.value == "success"
    assert "报告已完成" in run.final_answer
    assert run.summary_of_gaps == ""

    tasks = await h.tasks(run_id)
    assert set(tasks) == {"T1", "T2"}
    assert tasks["T1"].status.value == "accepted"
    assert tasks["T2"].status.value == "accepted"
    assert tasks["T2"].depends_on == ["T1"]
    assert tasks["T1"].result_summary

    # 依赖顺序：T2 进入 ready 必须晚于 T1 accepted
    types = await h.event_types(run_id)
    t1_accepted = types.index("task.status_changed")  # 占位，下面用 payload 精确比较
    _ = t1_accepted
    events = await h.events(run_id, ["task.status_changed"])
    seq_t1_accepted = next(
        e.seq for e in events
        if e.payload.get("key") == "T1" and e.payload.get("to_status") == "accepted"
    )
    seq_t2_ready = next(
        e.seq for e in events
        if e.payload.get("key") == "T2" and e.payload.get("to_status") == "ready"
    )
    assert seq_t1_accepted < seq_t2_ready

    # 交付物：每任务一个，状态 accepted；引用可用
    async with db_session() as session:
        arts = (
            await session.execute(select(Artifact).where(Artifact.run_id == run_id))
        ).scalars().all()
    assert len(arts) == 2
    assert all(a.status.value == "accepted" for a in arts)
    assert any(a.name == "research.md" for a in arts)
    assert any(a.name == "report.md" for a in arts)
    assert tasks["T1"].result_artifact_ids and tasks["T2"].result_artifact_ids

    # 用量与事件完整性
    assert run.usage and run.usage.get("input_tokens", 0) > 0
    for expected in [
        "run.created",
        "plan.proposed",
        "plan.approved",
        "task.created",
        "attempt.started",
        "tool.call.started",
        "attempt.reported",
        "review.submitted",
        "decision.recorded",
        "run.completed",
    ]:
        assert expected in types, f"缺少事件 {expected}"

    # T2 的任务单包含上游交付物引用（inputs 物化到工作目录）
    t2_attempts = await h.attempts(tasks["T2"].id)
    assert t2_attempts and t2_attempts[0].brief["inputs"], "T2 任务单应引用 T1 交付物"
    from app.config import get_config

    ws = (
        get_config().data_dir
        / "workspaces"
        / run_id
        / "T2"
        / str(t2_attempts[0].number)
        / "inputs"
    )
    assert ws.exists() and any(ws.iterdir()), "inputs 目录应包含上游交付物副本"
