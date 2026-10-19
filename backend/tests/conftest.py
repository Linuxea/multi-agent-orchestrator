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


async def eventually(
    fn: Any,
    timeout: float = 30.0,  # noqa: ASYNC109
    interval: float = 0.3,
    msg: str = "",
) -> Any:
    """轮询直到 fn() 返回真值；真实模型链路的非确定性断言辅助。"""
    import asyncio as _asyncio
    import time as _time

    deadline = _time.monotonic() + timeout
    last: Any = None
    while _time.monotonic() < deadline:
        last = await fn()
        if last:
            return last
        await _asyncio.sleep(interval)
    raise AssertionError(f"eventually 超时（{timeout}s）：{msg}；last={last!r}")


class CostLedger:
    """真实模型调用成本台账（session 级汇总，teardown 打印）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

    def summary(self) -> str:
        # deepseek-flash 定价约 $0.28/M in、$0.42/M out（实际以平台为准）
        cost = (self.input_tokens * 0.28 + self.output_tokens * 0.42) / 1_000_000
        return (
            f"[real-model cost] calls={self.calls} "
            f"in={self.input_tokens} out={self.output_tokens} ≈${cost:.4f}"
        )


@pytest.fixture(scope="session", autouse=True)
def _model_cost_ledger() -> Any:
    """统计全部模型调用用量（mock 与真实），session 结束打印真实成本估算。"""
    from app.runtime import model_client as mc

    ledger = CostLedger()
    orig_create = mc.ModelCaller.create
    orig_stream = mc.ModelCaller.stream

    async def create(self: Any, **kw: Any) -> Any:
        resp = await orig_create(self, **kw)
        ledger.add(resp.usage)
        return resp

    async def stream(self: Any, **kw: Any) -> Any:
        async for ev in orig_stream(self, **kw):
            if not isinstance(ev, mc.StreamEvent):
                ledger.add(ev.usage)
            yield ev

    mc.ModelCaller.create = create  # type: ignore[method-assign]
    mc.ModelCaller.stream = stream  # type: ignore[method-assign]
    try:
        yield ledger
    finally:
        mc.ModelCaller.create = orig_create  # type: ignore[method-assign]
        mc.ModelCaller.stream = orig_stream  # type: ignore[method-assign]
        if ledger.calls:
            print("\n" + ledger.summary())


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

    async def setup_real_profile(self, protocol: str = "anthropic_messages") -> str:
        """建立指向真实端点的模型配置（密钥走密钥库加密）。

        环境变量：DEEPSEEK_API_KEY（必需，缺失时 skip）、
        DEEPSEEK_MODEL（默认 deepseek-flash）、DEEPSEEK_BASE_URL、DEEPSEEK_OPENAI_BASE_URL。
        """
        import os as _os

        from app import secrets as secrets_util
        from app.models import Secret

        api_key = _os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            pytest.skip("未设置 DEEPSEEK_API_KEY，跳过真实模型测试")
        model = _os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        if protocol == "openai_chat":
            base = _os.environ.get("DEEPSEEK_OPENAI_BASE_URL", "https://api.deepseek.com")
        else:
            base = _os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic")
        async with db_session() as session:
            sec = Secret(
                id=new_id("sec"),
                name=f"MODEL_KEY::real-{protocol}",
                encrypted_value=secrets_util.encrypt(api_key),
                description="真实模型测试",
            )
            session.add(sec)
            await session.flush()
            profile = ModelProfile(
                id=new_id("mp"),
                name=f"ds-real-{protocol}",
                protocol=protocol,  # type: ignore[arg-type]
                base_url=base,
                model=model,
                max_output_tokens=4096,
                context_window=128000,
                is_default_orchestrator=True,
                is_default_worker=True,
                api_key_secret_id=sec.id,
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


@pytest.fixture
async def real_platform(env: Any) -> AsyncIterator[Harness]:
    """真实端点 Harness：模型调用全部走 DEEPSEEK_API_KEY 指向的真实服务。

    默认 anthropic_messages 协议；用例内可再调 setup_real_profile("openai_chat") 切换。
    """
    harness = Harness(container=env, model_state=None, model_url="")
    await harness.setup_real_profile("anthropic_messages")
    yield harness
