"""访问密码认证（§11.4、D-1.11）：设置页落地后启用强制。

- 未设置 access_password 时全部放行（默认开发态）。
- 设置后：/api/v1/* 除 /api/v1/auth/* 外必须携带
  `Authorization: Bearer <密码>` 或 `X-Platform-Key: <密码>`。
- WebSocket 经 `?key=<密码>` 查询参数（浏览器 WS 无法自定义头部）。
- 无状态：前端本地保存密码并随请求携带；校验用常数时间比较。
"""

from __future__ import annotations

import secrets as secrets_util
import time
from typing import Any

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.bootstrap import get_container
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["auth"])

# 设置缓存窗口：避免每个请求读库；改密码后最多 5 秒生效
_CACHE_TTL = 5.0
_cached: dict[str, Any] = {"password": None, "loaded_at": 0.0}


async def current_access_password() -> str | None:
    """读取 access_password（带短缓存）；读库失败时沿用上次结果。"""
    now = time.monotonic()
    if now - _cached["loaded_at"] < _CACHE_TTL:
        return _cached["password"] if isinstance(_cached["password"], str) else None
    try:
        async with db_session() as session:
            settings = await get_container().settings.get(session)
            _cached["password"] = settings.get("access_password") or None
            _cached["loaded_at"] = now
    except Exception:
        # 数据库暂不可用（如启动早期）：沿用旧值，由后续请求重试
        _cached["loaded_at"] = now
    return _cached["password"] if isinstance(_cached["password"], str) else None


def reset_auth_cache() -> None:
    """测试辅助：清除设置缓存。"""
    _cached["password"] = None
    _cached["loaded_at"] = 0.0


def key_matches(provided: str | None, expected: str) -> bool:
    return provided is not None and secrets_util.compare_digest(provided, expected)


def request_key(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-platform-key")


async def enforce_api_auth(request: Request, call_next: Any) -> Any:
    """ASGI 中间件：仅拦截 /api/v1/ 下非 auth 路径。"""
    path = request.url.path
    if not path.startswith("/api/") or path.startswith("/api/v1/auth/"):
        return await call_next(request)
    password = await current_access_password()
    if password is None or key_matches(request_key(request), password):
        return await call_next(request)
    return JSONResponse({"detail": "需要访问密码"}, status_code=401)


class VerifyBody(BaseModel):
    password: str = ""


@router.get("/auth/status")
async def auth_status() -> dict[str, Any]:
    """是否已启用访问密码（免认证，登录页探测用）。"""
    password = await current_access_password()
    return {"required": password is not None}


@router.post("/auth/verify")
async def verify_password(body: VerifyBody) -> Any:
    password = await current_access_password()
    if password is None:
        return {"ok": True, "required": False}
    if key_matches(body.password, password):
        return {"ok": True, "required": True}
    return JSONResponse({"ok": False, "detail": "密码错误"}, status_code=401)


async def ws_authorized(ws: WebSocket) -> bool:
    """WS 认证：?key= 或头部；未启用密码时放行。"""
    password = await current_access_password()
    if password is None:
        return True
    key = ws.query_params.get("key") or ws.headers.get("x-platform-key")
    return key_matches(key, password)
