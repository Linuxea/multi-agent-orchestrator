"""pytest 公共夹具：隔离环境、模拟模型服务、平台装配。"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# 本环境可能配置了 SOCKS 代理；模拟模型服务走本地回环，必须绕过代理
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

import httpx  # noqa: E402
import pytest  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.bootstrap import Container, reset_container  # noqa: E402
from app.config import AppConfig, set_config  # noqa: E402
from app.db import dispose_engine, init_db  # noqa: E402
from app.events.service import load_events  # noqa: E402
from app.ids import new_id  # noqa: E402
from app.models import Agent, Attempt, ModelProfile, Run, Session, Task  # noqa: E402
from app.orchestrator.helpers import db_session  # noqa: E402
from app.registry.service import RegistryService  # noqa: E402

ORCH_MARKER = "主管"  # 主 agent 系统提示词标记
WORKER_MARKER = "执行型子 agent"  # 子 agent 平台前言标记


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def env(tmp_path: Any) -> AsyncIterator[Container]:
    """每个测试独立的数据库 / 数据目录 / 容器。"""
    cfg = AppConfig(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        data_dir=tmp_path / "data",
        wake_merge_seconds=0.02,
        external_error_retry_delay=0.05,
        master_key=Fernet.generate_key().decode(),
        model_retry_delays=(0.01, 0.01, 0.01),
    )
    set_config(cfg)
    await init_db()
    container = reset_container()
    yield container
    await container.shutdown()
    await dispose_engine()


@pytest.fixture
async def mock_model(env: Container) -> AsyncIterator[tuple[Any, str]]:
    from tests.mockmodel.server_main import serve

    port = free_port()
    state, server, thread = serve(port)
    url = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient() as client:
        for _ in range(200):
            try:
                r = await client.get(f"{url}/health", timeout=1.0)
                if r.status_code == 200:
                    break
            except Exception:
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError("模拟模型服务启动失败")
    yield state, url
    server.should_exit = True
    thread.join(timeout=10)


@dataclass
class Harness:
    container: Container
    model_state: Any
    model_url: str
    _profile_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # --- 剧本 ---
    def scopes(self, data: dict[str, Any]) -> None:
        self.model_state.load(data)

    def load_yaml(self, path: str) -> None:
        from tests.mockmodel.server import load_yaml_file

        self.model_state.load(load_yaml_file(path))

    # --- 种子 ---
    async def setup_profile(self) -> str:
        async with db_session() as session:
            profile = ModelProfile(
                id=new_id("mp"),
                name="模拟模型",
                base_url=self.model_url,
                model="mock-model",
                max_output_tokens=4096,
                context_window=100000,
                is_default_orchestrator=True,
                is_default_worker=True,
            )
            session.add(profile)
            await session.flush()
            await self.container.settings.patch(
                session, {"orchestrator_model_profile_id": profile.id}
            )
            self._profile_id = profile.id
        return profile.id

    async def agent(
        self,
        slug: str,
        name: str,
        *,
        marker: str | None = None,
        prompt: str | None = None,
        executor: str = "lite",
        budget: dict[str, Any] | None = None,
        status: str = "active",
        max_concurrency: int = 2,
    ) -> Agent:
        assert self._profile_id, "先调用 setup_profile()"
        async with db_session() as session:
            registry = RegistryService()
            agent = await registry.create_agent(
                session,
                slug=slug,
                name=name,
                description=f"{name}（测试）",
                role_detail=f"擅长：{name} 相关工作。不做：无关工作。",
                capability_tags=["test"],
                system_prompt=prompt or f"你是测试用的{name}。标记词：{marker or slug}",
                executor=executor,
                model_profile_id=self._profile_id,
                default_budget=budget or {"max_turns": 12, "max_cost": 2.0, "max_wall_seconds": 60},
                status=status,
                origin="manual",
            )
            return agent

    async def patch_settings(self, changes: dict[str, Any]) -> None:
        async with db_session() as session:
            await self.container.settings.patch(session, changes)

    # --- 运行 ---
    async def start(self, request: str = "写一份测试报告") -> tuple[str, str]:
        async with db_session() as session:
            s = Session(id=new_id("sess"), title=request[:20])
            session.add(s)
            await session.flush()
            session_id = s.id
        message, run = await self.container.engine.submit_user_message(session_id, request)
        return run.id, session_id

    async def wait(self, run_id: str, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        return await self.container.engine.quiesce(run_id, timeout=timeout)

    # --- 查询 ---
    async def events(self, run_id: str, types: list[str] | None = None) -> list[Any]:
        async with db_session() as session:
            return await load_events(session, run_id, 0, types)

    async def event_types(self, run_id: str) -> list[str]:
        evs = await self.events(run_id)
        return [e.type for e in evs]

    async def debug_events(self, run_id: str) -> str:
        """断言失败时的诊断信息。"""
        evs = await self.events(run_id)
        lines = [
            f"#{e.seq} {e.type} {e.task_key or ''} {str(e.payload)[:160]}" for e in evs
        ]
        return "\n".join(lines)

    async def run_row(self, run_id: str) -> Run:
        async with db_session() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            return run

    async def tasks(self, run_id: str) -> dict[str, Task]:
        async with db_session() as session:
            result = await session.execute(
                select(Task).where(Task.run_id == run_id).order_by(Task.key)
            )
            return {t.key: t for t in result.scalars().all()}

    async def attempts(self, task_id: str) -> list[Attempt]:
        async with db_session() as session:
            result = await session.execute(
                select(Attempt).where(Attempt.task_id == task_id).order_by(Attempt.number)
            )
            return list(result.scalars().all())

    async def answer(self, run_id: str, answer: str) -> None:
        await self.container.engine.submit_answer(run_id, answer)


@pytest.fixture
async def platform(mock_model: tuple[Any, str]) -> AsyncIterator[Harness]:
    state, url = mock_model
    harness = Harness(container=reset_container(), model_state=state, model_url=url)
    await harness.setup_profile()
    yield harness
