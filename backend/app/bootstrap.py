"""组件装配：单进程内组合所有服务（事件总线、引擎、调度器、执行器）。"""

from __future__ import annotations

from typing import Any

from app.artifacts.repo import ArtifactRepository
from app.events.bus import EventBus
from app.events.service import EventService
from app.orchestrator.engine import CoordinationEngine
from app.orchestrator.scheduler import Scheduler
from app.registry.service import RegistryService
from app.runtime.executor import ExecutorRegistry
from app.runtime.lite import LiteExecutor
from app.runtime.sdk import SdkExecutor
from app.settings import SettingsService


class Container:
    """进程级单例容器。测试中按需重建。"""

    def __init__(self) -> None:
        self.bus = EventBus()
        self.events = EventService(self.bus)
        self.registry = RegistryService()
        self.settings = SettingsService()
        self.artifacts = ArtifactRepository(self.events)
        self.executors = ExecutorRegistry()
        self.executors.register(LiteExecutor())
        self.executors.register(SdkExecutor())
        self.scheduler = Scheduler(
            self.events, self.registry, self.executors, self.settings, self.artifacts
        )
        self.engine = CoordinationEngine(self.events, self.registry, self.settings)
        self.engine.scheduler = self.scheduler
        self.scheduler.engine = self.engine

    async def recover(self) -> None:
        await self.engine.recover()

    async def shutdown(self) -> None:
        for _attempt_id, (task, _run) in list(self.scheduler._running.items()):  # noqa: SLF001
            if not task.done():
                task.cancel()
        self.scheduler._running.clear()  # noqa: SLF001


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container


def reset_container() -> Container:
    """测试用：重建容器（丢弃内存态：线程、唤醒队列、序号缓存）。"""
    global _container
    _container = Container()
    return _container


def wire(_: Any = None) -> Container:
    return get_container()
