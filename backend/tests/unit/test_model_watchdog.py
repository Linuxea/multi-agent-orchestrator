"""模型调用挂起看门狗测试。

背景（真实事故）：DeepSeek 端点偶发半开连接，SDK/httpx 的 600s 超时未触发，
编排协程永久挂起、run 停在 planning 无人恢复。看门狗以 asyncio.wait_for 绝对
兜底：非流式超 model_call_deadline_seconds、流式单次读取超 model_stream_idle_seconds
即抛 ModelError 走既有失败路径。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.conftest import Harness


@pytest.fixture
def fast_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """看门狗阈值调小，测试秒级验证。"""
    from app.config import get_config

    cfg = get_config()
    monkeypatch.setattr(cfg, "model_call_deadline_seconds", 0.4)
    monkeypatch.setattr(cfg, "model_stream_idle_seconds", 0.4)


async def test_orchestrator_deadline_fails_run(
    platform: Harness, fast_watchdog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主循环模型调用永久挂起 → run 应被标 failed（而非永久卡死）。"""
    from app.runtime import model_client as mc

    async def _hang(self: Any, **kw: Any) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(mc.ModelCaller, "create", _hang)

    run_id, _sess = await platform.start("写一份测试报告")
    ok = await platform.wait(run_id, timeout=15)
    assert ok, "挂起应被看门狗掐断，运行静止"

    row = await platform.run_row(run_id)
    assert row.status.value == "failed", f"应标 failed，实际 {row.status.value}"
    assert "墙钟" in (row.summary_of_gaps or ""), row.summary_of_gaps


async def test_stream_idle_watchdog_raises_model_error(
    fast_watchdog: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """流式单次读取超时 → ModelError（lite 执行器走 EvCrashed 路径）。"""
    from app.runtime.lite import _stream_with_watchdog
    from app.runtime.model_client import ModelCaller, ModelError
    from app.runtime.types import ModelConfig

    class _HangGen:
        def __anext__(self) -> Any:
            return asyncio.sleep(3600)  # 永不产出事件

        async def aclose(self) -> None:
            return None

    def _stream(self: Any, **kw: Any) -> Any:
        return _HangGen()

    monkeypatch.setattr(ModelCaller, "stream", _stream)
    caller = ModelCaller(
        ModelConfig(profile_id="mp", model="m", base_url="http://x", api_key="k")
    )
    with pytest.raises(ModelError, match="看门狗"):
        async for _ in _stream_with_watchdog(caller, system="s", messages=[], tools=None):
            pass  # pragma: no cover
