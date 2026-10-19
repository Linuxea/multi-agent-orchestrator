"""模型配置 API（§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.ids import new_id
from app.models import ModelProfile
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["model-profiles"])


class ModelProfileCreate(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: str = ""  # 直接写明文则自动转存 Secret
    protocol: str = "anthropic_messages"  # 第五期：openai_chat 兼容协议
    max_output_tokens: int = 8192
    temperature: float | None = None
    thinking: str = "off"
    context_window: int = 128000
    price_input: float = 0.0
    price_output: float = 0.0
    is_default_orchestrator: bool = False
    is_default_worker: bool = False


def _profile_out(p: ModelProfile) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "protocol": p.protocol.value,
        "base_url": p.base_url,
        "api_key_secret_id": p.api_key_secret_id,
        "model": p.model,
        "max_output_tokens": p.max_output_tokens,
        "temperature": p.temperature,
        "thinking": p.thinking.value,
        "context_window": p.context_window,
        "price_input": float(p.price_input or 0),
        "price_output": float(p.price_output or 0),
        "is_default_orchestrator": p.is_default_orchestrator,
        "is_default_worker": p.is_default_worker,
    }


async def _store_api_key(session: Any, plaintext: str, profile_name: str) -> str | None:
    if not plaintext:
        return None
    from app import secrets as secrets_util
    from app.models import Secret

    name = f"MODEL_KEY::{profile_name}"
    result = await session.execute(select(Secret).where(Secret.name == name))
    secret = result.scalar_one_or_none()
    if secret is None:
        secret = Secret(
            id=new_id("sec"),
            name=name,
            encrypted_value=secrets_util.encrypt(plaintext),
            description=f"模型配置 {profile_name} 的 API key",
        )
        session.add(secret)
    else:
        secret.encrypted_value = secrets_util.encrypt(plaintext)
    await session.flush()
    return str(secret.id)


@router.get("/model-profiles")
async def list_profiles() -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(select(ModelProfile).order_by(ModelProfile.created_at))
        return [_profile_out(p) for p in result.scalars().all()]


@router.post("/model-profiles", status_code=201)
async def create_profile(body: ModelProfileCreate) -> dict[str, Any]:
    from app.enums import ModelProtocol, ThinkingMode

    async with db_session() as session:
        secret_id = await _store_api_key(session, body.api_key, body.name)
        profile = ModelProfile(
            id=new_id("mp"),
            name=body.name,
            protocol=ModelProtocol(body.protocol),
            base_url=body.base_url,
            api_key_secret_id=secret_id,
            model=body.model,
            max_output_tokens=body.max_output_tokens,
            temperature=body.temperature,
            thinking=ThinkingMode(body.thinking),
            context_window=body.context_window,
            price_input=body.price_input,
            price_output=body.price_output,
            is_default_orchestrator=body.is_default_orchestrator,
            is_default_worker=body.is_default_worker,
        )
        session.add(profile)
        await session.flush()
        return _profile_out(profile)


@router.patch("/model-profiles/{profile_id}")
async def update_profile(profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    async with db_session() as session:
        p = await session.get(ModelProfile, profile_id)
        if p is None:
            raise HTTPException(404, "模型配置不存在")
        if "api_key" in body:
            secret_id = await _store_api_key(session, str(body.pop("api_key")), p.name)
            if secret_id:
                p.api_key_secret_id = secret_id
        for field in (
            "name", "base_url", "model", "max_output_tokens", "temperature",
            "context_window", "price_input", "price_output",
            "is_default_orchestrator", "is_default_worker",
        ):
            if field in body:
                setattr(p, field, body[field])
        if "protocol" in body:
            from app.enums import ModelProtocol

            p.protocol = ModelProtocol(str(body["protocol"]))
        if "thinking" in body:
            from app.enums import ThinkingMode

            p.thinking = ThinkingMode(str(body["thinking"]))
        await session.flush()
        return _profile_out(p)


@router.delete("/model-profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str) -> None:
    async with db_session() as session:
        p = await session.get(ModelProfile, profile_id)
        if p is not None:
            await session.delete(p)


@router.post("/model-profiles/{profile_id}/check")
async def check_profile(profile_id: str) -> dict[str, Any]:
    """模型兼容性体检（§9.6）：基础对话/流式/工具调用/用量四项 + 时延。"""
    import time as _time

    from app.runtime.model_client import ModelCaller, ModelError
    from app.runtime.model_config import model_config_from_profile
    from app.runtime.types import ContentBlock

    async with db_session() as session:
        p = await session.get(ModelProfile, profile_id)
        if p is None:
            raise HTTPException(404, "模型配置不存在")
        api_key = ""
        if p.api_key_secret_id:
            from app import secrets as secrets_util
            from app.models import Secret

            secret = await session.get(Secret, p.api_key_secret_id)
            if secret is not None:
                try:
                    api_key = secrets_util.decrypt(secret.encrypted_value)
                except ValueError:
                    api_key = ""
        cfg = model_config_from_profile(p, api_key)

    caller = ModelCaller(cfg)
    checks: list[dict[str, Any]] = []

    # 1. 基础对话（含时延）
    t0 = _time.monotonic()
    try:
        resp = await caller.create(
            system="你是测试助手。",
            messages=[{"role": "user", "content": "回复：OK"}],
            max_tokens=32,
        )
        checks.append({
            "name": "基础对话", "passed": bool(resp.blocks), "detail": "",
            "latency_ms": int((_time.monotonic() - t0) * 1000),
        })
    except ModelError as e:
        checks.append({
            "name": "基础对话", "passed": False, "detail": str(e),
            "latency_ms": int((_time.monotonic() - t0) * 1000),
        })
        return {"checks": checks}

    # 2. 流式输出
    t0 = _time.monotonic()
    try:
        got_delta = False
        async for ev in caller.stream(
            system="你是测试助手。", messages=[{"role": "user", "content": "从 1 数到 5"}],
            max_tokens=64,
        ):
            from app.runtime.model_client import StreamEvent

            if isinstance(ev, StreamEvent) and ev.kind == "delta":
                got_delta = True
        checks.append({
            "name": "流式输出", "passed": got_delta, "detail": "",
            "latency_ms": int((_time.monotonic() - t0) * 1000),
        })
    except ModelError as e:
        checks.append({
            "name": "流式输出", "passed": False, "detail": str(e),
            "latency_ms": int((_time.monotonic() - t0) * 1000),
        })

    # 3. 工具调用
    t0 = _time.monotonic()
    try:
        resp = await caller.create(
            system="你是测试助手。",
            messages=[{"role": "user", "content": "调用 echo 工具，参数 text=hello"}],
            tools=[{
                "name": "echo",
                "description": "回显文本",
                "input_schema": {"type": "object",
                                 "properties": {"text": {"type": "string"}},
                                 "required": ["text"]},
            }],
            max_tokens=128,
        )
        has_tool = any(b.type == "tool_use" for b in resp.blocks)
        checks.append({
            "name": "工具调用", "passed": has_tool, "detail": "",
            "latency_ms": int((_time.monotonic() - t0) * 1000),
        })
    except ModelError as e:
        checks.append({
            "name": "工具调用", "passed": False, "detail": str(e),
            "latency_ms": int((_time.monotonic() - t0) * 1000),
        })

    # 4. 用量返回
    checks.append({
        "name": "用量返回",
        "passed": resp.usage.input_tokens > 0 or resp.usage.output_tokens > 0,
        "detail": f"in={resp.usage.input_tokens} out={resp.usage.output_tokens}",
        "latency_ms": 0,
    })
    _ = ContentBlock
    return {
        "checks": checks,
        "ok": all(c["passed"] for c in checks),
        "base_url": cfg.base_url,
        "model": cfg.model,
    }
