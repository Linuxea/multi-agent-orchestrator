"""组件装配：单进程内组合所有服务（事件总线、引擎、调度器、执行器）。"""

from __future__ import annotations

import os
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


def configured_mock_url() -> str:
    """模拟模型地址：PLATFORM_MOCK_MODEL_URL 或 MOCK_MODEL_URL（简写兼容）。"""
    return os.environ.get("MOCK_MODEL_URL", "") or os.environ.get("PLATFORM_MOCK_MODEL_URL", "")


async def ensure_mock_profile(mock_url: str) -> None:
    """配置了模拟模型时确保存在指向它的默认模型配置（幂等）。"""
    from sqlalchemy import select

    from app.ids import new_id
    from app.models import ModelProfile
    from app.orchestrator.helpers import db_session

    async with db_session() as session:
        result = await session.execute(select(ModelProfile).limit(1))
        if result.scalar_one_or_none() is not None:
            return
        profile = ModelProfile(
            id=new_id("mp"),
            name="模拟模型",
            base_url=mock_url,
            model="mock-model",
            max_output_tokens=4096,
            is_default_orchestrator=True,
            is_default_worker=True,
        )
        session.add(profile)
        await session.flush()


async def demo_downgrade_executors() -> int:
    """演示模式（模拟模型且无 Claude Code 运行时）：sdk 执行器临时降级 lite（D-2.5）。"""
    from sqlalchemy import select

    from app.enums import ExecutorKind
    from app.models import Agent
    from app.orchestrator.helpers import db_session
    from app.runtime.sdk import sdk_runtime_available

    if sdk_runtime_available():
        return 0

    async with db_session() as session:
        result = await session.execute(select(Agent).where(Agent.executor == ExecutorKind.sdk))
        agents = list(result.scalars().all())
        for a in agents:
            a.executor = ExecutorKind.lite
        return len(agents)


async def seed_initial_data(mock_url: str = "") -> tuple[int, int, int]:
    """幂等种子数据：内置工具 + 预置 agent（+ 演示模拟模型与降级）。

    返回 (新建内置工具数, 新建预置 agent 数, 降级执行器数)。CLI init 与
    API 服务首次启动都会调用（界面部署可不跑 CLI，D-3.4）。
    """
    from app.orchestrator.helpers import db_session
    from app.registry.presets import seed_presets
    from app.tools.service import seed_builtin_tools

    if mock_url:
        await ensure_mock_profile(mock_url)
    async with db_session() as session:
        builtins = await seed_builtin_tools(session)
        created = await seed_presets(session)
    downgraded = await demo_downgrade_executors() if mock_url else 0
    return len(builtins), len(created), downgraded


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
