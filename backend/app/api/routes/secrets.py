"""密钥 API（§8.8：任何接口不返回明文）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app import secrets as secrets_util
from app.bootstrap import get_container
from app.ids import new_id
from app.models import Secret
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["secrets"])


class SecretCreate(BaseModel):
    name: str
    value: str
    description: str = ""


@router.get("/secrets")
async def list_secrets() -> list[dict[str, Any]]:
    from datetime import datetime

    def _iso(value: object) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None

    async with db_session() as session:
        result = await session.execute(select(Secret).order_by(Secret.created_at))
        secrets: list[Secret] = list(result.scalars().all())
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "created_at": _iso(s.created_at) or "",
                "last_used_at": _iso(s.last_used_at),
            }
            for s in secrets
        ]


@router.post("/secrets", status_code=201)
async def create_secret(body: SecretCreate) -> dict[str, Any]:
    async with db_session() as session:
        existing = await session.execute(select(Secret).where(Secret.name == body.name))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(409, f"密钥名已存在: {body.name}")
        secret = Secret(
            id=new_id("sec"),
            name=body.name,
            encrypted_value=secrets_util.encrypt(body.value),
            description=body.description,
        )
        session.add(secret)
        await session.flush()
        container = get_container()
        await container.events.emit(
            session,
            type="config.settings_changed",
            actor="user",
            run_id=None,
            payload={"entity": "secret", "entity_id": secret.id,
                     "diff": {"name": body.name, "value": "***"}},
        )
        return {"id": secret.id, "name": secret.name}


@router.put("/secrets/{secret_id}")
async def update_secret(secret_id: str, body: SecretCreate) -> dict[str, Any]:
    async with db_session() as session:
        s = await session.get(Secret, secret_id)
        if s is None:
            raise HTTPException(404, "密钥不存在")
        s.encrypted_value = secrets_util.encrypt(body.value)
        s.description = body.description
        await session.flush()
        return {"id": s.id, "name": s.name}


@router.delete("/secrets/{secret_id}", status_code=204)
async def delete_secret(secret_id: str) -> None:
    async with db_session() as session:
        s = await session.get(Secret, secret_id)
        if s is not None:
            await session.delete(s)
