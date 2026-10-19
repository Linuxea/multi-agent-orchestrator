"""访问密码认证测试（§11.4、D-1.11、第三期）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.websockets import WebSocket

from app.api.app import create_app
from app.api.auth import reset_auth_cache, ws_authorized
from app.bootstrap import Container
from app.orchestrator.helpers import db_session


@pytest.fixture
async def app_client(env: Container) -> AsyncIterator[httpx.AsyncClient]:
    reset_auth_cache()
    app = create_app()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    reset_auth_cache()


async def set_password(container: Container, password: str | None) -> None:
    async with db_session() as session:
        await container.settings.patch(session, {"access_password": password})
    reset_auth_cache()


async def test_auth_disabled_allows_all(app_client: httpx.AsyncClient, env: Container) -> None:
    r = await app_client.get("/api/v1/auth/status")
    assert r.status_code == 200 and r.json() == {"required": False}
    r = await app_client.get("/api/v1/sessions")
    assert r.status_code == 200


async def test_auth_status_endpoint_always_open(app_client: httpx.AsyncClient, env: Container) -> None:
    await set_password(env, "s3cret-key")
    r = await app_client.get("/api/v1/auth/status")
    assert r.status_code == 200 and r.json() == {"required": True}


async def test_password_required_when_set(app_client: httpx.AsyncClient, env: Container) -> None:
    await set_password(env, "s3cret-key")
    r = await app_client.get("/api/v1/sessions")
    assert r.status_code == 401
    r = await app_client.get("/api/v1/sessions", headers={"X-Platform-Key": "wrong"})
    assert r.status_code == 401
    r = await app_client.get("/api/v1/sessions", headers={"Authorization": "Bearer s3cret-key"})
    assert r.status_code == 200
    r = await app_client.get("/api/v1/sessions", headers={"X-Platform-Key": "s3cret-key"})
    assert r.status_code == 200


async def test_verify_endpoint(app_client: httpx.AsyncClient, env: Container) -> None:
    r = await app_client.post("/api/v1/auth/verify", json={"password": "x"})
    assert r.status_code == 200 and r.json()["ok"] is True  # 未启用时恒通过

    await set_password(env, "s3cret-key")
    r = await app_client.post("/api/v1/auth/verify", json={"password": "wrong"})
    assert r.status_code == 401
    r = await app_client.post("/api/v1/auth/verify", json={"password": "s3cret-key"})
    assert r.status_code == 200 and r.json() == {"ok": True, "required": True}


async def test_meta_endpoints_bypass(app_client: httpx.AsyncClient, env: Container) -> None:
    await set_password(env, "s3cret-key")
    r = await app_client.get("/healthz")
    assert r.status_code == 200


class _FakeWS(WebSocket):
    """只携带查询参数与头部的桩，测试 ws_authorized 判定。"""

    def __init__(self, query: dict[str, str], headers: dict[str, str]) -> None:  # noqa: ARG002
        pass

    @property
    def query_params(self) -> dict[str, str]:
        return self._query  # type: ignore[attr-defined]

    @property
    def headers(self) -> dict[str, str]:
        return self._headers  # type: ignore[attr-defined]


def _make_ws(query: dict[str, str], headers: dict[str, str]) -> Any:
    ws = object.__new__(_FakeWS)
    object.__setattr__(ws, "_query", query)
    object.__setattr__(ws, "_headers", headers)
    return ws


async def test_ws_authorized(env: Container) -> None:
    # 未设密码：放行
    reset_auth_cache()
    assert await ws_authorized(_make_ws({}, {})) is True

    await set_password(env, "s3cret-key")
    assert await ws_authorized(_make_ws({}, {})) is False
    assert await ws_authorized(_make_ws({"key": "wrong"}, {})) is False
    assert await ws_authorized(_make_ws({"key": "s3cret-key"}, {})) is True
    assert await ws_authorized(_make_ws({}, {"x-platform-key": "s3cret-key"})) is True
