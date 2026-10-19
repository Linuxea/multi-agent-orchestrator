"""Agent 注册表 API（§12.2；页面在第三期）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.bootstrap import get_container
from app.enums import AgentStatus
from app.models import Agent, AgentVersion
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["agents"])


class AgentCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    description: str = Field(max_length=200)
    role_detail: str = ""
    capability_tags: list[str] = []
    system_prompt: str
    executor: str = "lite"
    model_profile_id: str
    default_budget: dict[str, Any] | None = None
    max_concurrency: int = 2
    status: str = "draft"
    avatar: str | None = None
    input_contract: str = ""
    output_contract: str = ""


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    role_detail: str | None = None
    capability_tags: list[str] | None = None
    system_prompt: str | None = None
    executor: str | None = None
    model_profile_id: str | None = None
    default_budget: dict[str, Any] | None = None
    max_concurrency: int | None = None
    status: str | None = None
    avatar: str | None = None
    input_contract: str | None = None
    output_contract: str | None = None
    version: int
    change_note: str = ""


def _agent_out(a: Agent) -> dict[str, Any]:
    return {
        "id": a.id,
        "slug": a.slug,
        "name": a.name,
        "description": a.description,
        "role_detail": a.role_detail,
        "capability_tags": a.capability_tags,
        "system_prompt": a.system_prompt,
        "executor": a.executor.value,
        "model_profile_id": a.model_profile_id,
        "default_budget": a.default_budget,
        "max_concurrency": a.max_concurrency,
        "status": a.status.value,
        "version": a.version,
        "origin": a.origin.value,
        "avatar": a.avatar,
        "created_at": a.created_at.isoformat(),
    }


@router.get("/agents")
async def list_agents(status: str | None = None) -> list[dict[str, Any]]:
    async with db_session() as session:
        stmt = select(Agent).order_by(Agent.created_at)
        if status:
            stmt = stmt.where(Agent.status == AgentStatus(status))
        result = await session.execute(stmt)
        return [_agent_out(a) for a in result.scalars().all()]


@router.post("/agents", status_code=201)
async def create_agent(body: AgentCreate) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        existing = await container.registry.get_agent_by_slug(session, body.slug)
        if existing is not None:
            raise HTTPException(409, f"slug 已存在: {body.slug}")
        agent = await container.registry.create_agent(
            session,
            slug=body.slug,
            name=body.name,
            description=body.description,
            role_detail=body.role_detail,
            capability_tags=body.capability_tags,
            system_prompt=body.system_prompt,
            executor=body.executor,
            model_profile_id=body.model_profile_id,
            default_budget=body.default_budget,
            max_concurrency=body.max_concurrency,
            status=body.status,
            origin="manual",
            avatar=body.avatar,
            input_contract=body.input_contract,
            output_contract=body.output_contract,
        )
        return _agent_out(agent)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    async with db_session() as session:
        a = await session.get(Agent, agent_id)
        if a is None:
            raise HTTPException(404, "agent 不存在")
        return _agent_out(a)


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        a = await session.get(Agent, agent_id)
        if a is None:
            raise HTTPException(404, "agent 不存在")
        if a.version != body.version:
            raise HTTPException(409, "版本冲突（乐观锁）")
        changes = {k: v for k, v in body.model_dump().items()
                   if v is not None and k not in ("version", "change_note")}
        a = await container.registry.update_agent(session, a, changes, body.change_note)
        return _agent_out(a)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str) -> None:
    """仅从未被历史运行引用的 agent 可物理删除（§6.3）。"""
    from app.models import Attempt

    async with db_session() as session:
        a = await session.get(Agent, agent_id)
        if a is None:
            raise HTTPException(404, "agent 不存在")
        referenced = (
            await session.execute(
                select(Attempt.id).where(Attempt.agent_id == agent_id).limit(1)
            )
        ).scalar_one_or_none()
        if referenced:
            raise HTTPException(409, "被历史运行引用的 agent 只能归档，不能删除")
        await session.delete(a)


@router.get("/agents/{agent_id}/versions")
async def agent_versions(agent_id: str) -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc())
        )
        return [
            {
                "version": v.version,
                "snapshot": v.snapshot,
                "change_note": v.change_note,
                "created_at": v.created_at.isoformat(),
            }
            for v in result.scalars().all()
        ]


@router.post("/agents/{agent_id}/rollback", status_code=204)
async def rollback_agent(agent_id: str, body: dict[str, int]) -> None:
    container = get_container()
    async with db_session() as session:
        a = await session.get(Agent, agent_id)
        if a is None:
            raise HTTPException(404, "agent 不存在")
        target = int(body.get("version", 0))
        result = await session.execute(
            select(AgentVersion).where(
                AgentVersion.agent_id == agent_id, AgentVersion.version == target
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise HTTPException(404, "版本不存在")
        snapshot = dict(version.snapshot)
        snapshot.pop("slug", None)
        a = await container.registry.update_agent(
            session, a, snapshot, f"回滚到 v{target}"
        )
