"""执行器抽象（§9.1）：协调引擎只依赖此接口。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.runtime.types import ExecEvent, RunContext


@runtime_checkable
class Executor(Protocol):
    name: str

    def start(self, ctx: RunContext) -> AsyncIterator[ExecEvent]: ...

    async def send_input(self, ctx: RunContext, text: str) -> None: ...

    async def interrupt(self, attempt_id: str) -> None: ...

    def resume(self, ctx: RunContext) -> AsyncIterator[ExecEvent] | None:
        """服务重启后尝试续跑；返回 None 表示无法续跑（LiteExecutor 即如此）。"""
        ...


class ExecutorRegistry:
    """执行器注册表：新增执行器只需实现接口并注册（§13 可扩展性）。"""

    def __init__(self) -> None:
        self._executors: dict[str, Executor] = {}

    def register(self, executor: Executor) -> None:
        self._executors[executor.name] = executor

    def get(self, name: str) -> Executor:
        if name not in self._executors:
            raise KeyError(f"未注册的执行器: {name}")
        return self._executors[name]

    def available(self) -> list[str]:
        return sorted(self._executors)
