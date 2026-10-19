"""LiteExecutor 单元测试（§9.8、§5.4 运行时兜底）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.runtime.lite import LiteExecutor
from app.runtime.types import (
    EvCrashed,
    EvReported,
    EvTimedOut,
    ModelConfig,
    RunContext,
)
from app.schemas.common import Budget
from tests.conftest import free_port


def _start_mock() -> tuple[Any, Any, Any, int]:
    from tests.mockmodel.server_main import serve

    port = free_port()
    state, server, thread = serve(port)
    return state, server, thread, port


async def _wait_health(url: str) -> None:
    import httpx

    async with httpx.AsyncClient() as client:
        for _ in range(100):
            try:
                if (await client.get(f"{url}/health", timeout=1)).status_code == 200:
                    return
            except Exception:
                await asyncio.sleep(0.05)
        raise RuntimeError("模拟模型服务启动失败")




def _ctx(url: str, workspace: Path, budget: Budget, attempt: str = "att_1") -> RunContext:
    return RunContext(
        run_id="run_1",
        task_key="T1",
        attempt_id=attempt,
        attempt_no=1,
        agent_slug="tester",
        workspace=str(workspace),
        system_prompt="你是测试 agent。标记词：tester",
        first_input="请开始执行任务",
        budget=budget,
        model=ModelConfig(
            profile_id="mp", model="mock-model", base_url=url, api_key="none"
        ),
        executor="lite",
    )


async def _collect(executor: LiteExecutor, ctx: RunContext) -> list[Any]:
    events: list[Any] = []
    async for ev in executor.start(ctx):
        events.append(ev)
    return events


@pytest.fixture
async def lite_mock(env: None, tmp_path: Path) -> tuple[Any, str, Path]:
    state, server, thread, port = await asyncio.to_thread(_start_mock)
    url = f"http://127.0.0.1:{port}"
    await _wait_health(url)
    yield state, url, tmp_path / "ws"
    server.should_exit = True
    thread.join(timeout=5)


async def test_submit_report_flow(lite_mock: tuple[Any, str, Path]) -> None:
    state, url, ws = lite_mock
    state.load(
        {
            "scopes": [
                {
                    "match": {"system_contains": "tester"},
                    "steps": [
                        {
                            "tool_calls": [
                                {
                                    "name": "submit_report",
                                    "input": {
                                        "status": "done",
                                        "summary": "完成",
                                        "deliverables": [
                                            {"name": "r.md", "content": "# 好", "summary": "正文"}
                                        ],
                                        "criteria_self_check": [
                                            {"criterion": "c1", "met": True}
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    )
    ws.mkdir(parents=True, exist_ok=True)
    executor = LiteExecutor()
    events = await _collect(executor, _ctx(url, ws, Budget(max_turns=5, max_wall_seconds=30)))
    reported = [e for e in events if isinstance(e, EvReported)]
    assert len(reported) == 1
    assert reported[0].report["status"] == "done"
    assert reported[0].report["_usage"]["turns"] == 1
    crashed = [e for e in events if isinstance(e, EvCrashed)]
    assert not crashed


async def test_budget_exhausted_auto_report(lite_mock: tuple[Any, str, Path]) -> None:
    """轮次预算耗尽 → 自动生成 budget_exhausted 回报（§5.4）。"""
    state, url, ws = lite_mock
    state.load(
        {
            "scopes": [
                {
                    "match": {"system_contains": "tester"},
                    "steps": [
                        {"text": "我先想想……"},
                        {"text": "继续想……"},
                    ],
                }
            ]
        }
    )
    ws.mkdir(parents=True, exist_ok=True)
    executor = LiteExecutor()
    events = await _collect(executor, _ctx(url, ws, Budget(max_turns=1, max_wall_seconds=30)))
    reported = [e for e in events if isinstance(e, EvReported)]
    assert len(reported) == 1
    assert reported[0].report["blocker"]["type"] == "budget_exhausted"


async def test_no_report_then_crashed(lite_mock: tuple[Any, str, Path]) -> None:
    """结束会话却没调用 submit_report → 追问后仍不提交 → crashed（§5.4）。"""
    state, url, ws = lite_mock
    state.load(
        {
            "scopes": [
                {
                    "match": {"system_contains": "tester"},
                    "steps": [
                        {"text": "做完了。"},
                        {"text": "我说了做完了。"},
                        {"text": "……"},
                    ],
                }
            ]
        }
    )
    ws.mkdir(parents=True, exist_ok=True)
    executor = LiteExecutor()
    events = await _collect(executor, _ctx(url, ws, Budget(max_turns=10, max_wall_seconds=30)))
    crashed = [e for e in events if isinstance(e, EvCrashed)]
    assert crashed and "未提交回报" in crashed[0].error


async def test_invalid_report_twice_crashed(lite_mock: tuple[Any, str, Path]) -> None:
    """回报 JSON 不合法 → 校验错误重交，最多 2 次（§5.4）。"""
    state, url, ws = lite_mock
    bad_input = {"status": "not-a-status", "summary": "x"}
    state.load(
        {
            "scopes": [
                {
                    "match": {"system_contains": "tester"},
                    "steps": [
                        {"tool_calls": [{"name": "submit_report", "input": bad_input}]},
                        {"tool_calls": [{"name": "submit_report", "input": bad_input}]},
                        {"tool_calls": [{"name": "submit_report", "input": bad_input}]},
                    ],
                }
            ]
        }
    )
    ws.mkdir(parents=True, exist_ok=True)
    executor = LiteExecutor()
    events = await _collect(executor, _ctx(url, ws, Budget(max_turns=10, max_wall_seconds=30)))
    crashed = [e for e in events if isinstance(e, EvCrashed)]
    assert crashed and "不合法" in crashed[0].error


async def test_wall_timeout(lite_mock: tuple[Any, str, Path]) -> None:
    """超过 max_wall_seconds → 运行时兜底：超时事件或 budget_exhausted 自动回报。

    预算含 max_wall_seconds 时，轮末的预算检查先于下一轮的超时检查触发，
    因此常见路径是自动回报；EvTimedOut 保留给"暂停后跨过死线"等场景。
    """
    state, url, ws = lite_mock
    state.load(
        {
            "scopes": [
                {
                    "match": {"system_contains": "tester"},
                    "steps": [
                        {"text": "第一步。", "stream_delay": 0.3},
                        {"text": "第二步。", "stream_delay": 0.3},
                        {"text": "第三步。", "stream_delay": 0.3},
                        {"text": "第四步。", "stream_delay": 0.3},
                        {"text": "第五步。", "stream_delay": 0.3},
                    ],
                }
            ]
        }
    )
    ws.mkdir(parents=True, exist_ok=True)
    executor = LiteExecutor()
    events = await _collect(executor, _ctx(url, ws, Budget(max_turns=10, max_wall_seconds=1)))
    timed_out = [e for e in events if isinstance(e, EvTimedOut)]
    reported = [e for e in events if isinstance(e, EvReported)]
    assert timed_out or (
        reported and reported[0].report["blocker"]["type"] == "budget_exhausted"
    ), "应产生超时或预算耗尽兜底"
