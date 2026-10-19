"""团队与模板 API（§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.bootstrap import get_container
from app.models import Playbook, Team
from app.orchestrator.helpers import db_session
from app.registry.service import validate_playbook_stages

router = APIRouter(tags=["teams"])


class TeamCreate(BaseModel):
    slug: str
    name: str
    description: str = ""
    member_agent_ids: list[str] = []
    lead_hint: str = ""


def _team_out(t: Team, playbook: Playbook | None = None) -> dict[str, Any]:
    return {
        "id": t.id,
        "slug": t.slug,
        "name": t.name,
        "description": t.description,
        "member_agent_ids": t.member_agent_ids,
        "playbook_id": t.playbook_id,
        "lead_hint": t.lead_hint,
        "playbook": (
            {
                "id": playbook.id,
                "name": playbook.name,
                "trigger_hint": playbook.trigger_hint,
                "stages": playbook.stages,
            }
            if playbook
            else None
        ),
    }


@router.get("/teams")
async def list_teams() -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(select(Team).order_by(Team.created_at))
        out = []
        for t in result.scalars().all():
            pb = await session.get(Playbook, t.playbook_id) if t.playbook_id else None
            out.append(_team_out(t, pb))
        return out


@router.post("/teams", status_code=201)
async def create_team(body: TeamCreate) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        existing = await container.registry.get_team_by_slug(session, body.slug)
        if existing is not None:
            raise HTTPException(409, f"slug 已存在: {body.slug}")
        try:
            t = await container.registry.create_team(
                session,
                slug=body.slug,
                name=body.name,
                description=body.description,
                member_agent_ids=body.member_agent_ids,
                lead_hint=body.lead_hint,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return _team_out(t)


async def _validate_members(container: Any, session: Any, member_ids: list[str]) -> None:
    missing = await container.registry.missing_agents(session, member_ids)
    if missing:
        raise HTTPException(400, f"成员 agent 不存在: {', '.join(missing)}")


@router.get("/teams/{team_id}")
async def get_team(team_id: str) -> dict[str, Any]:
    async with db_session() as session:
        t = await session.get(Team, team_id)
        if t is None:
            raise HTTPException(404, "team 不存在")
        pb = await session.get(Playbook, t.playbook_id) if t.playbook_id else None
        return _team_out(t, pb)


@router.patch("/teams/{team_id}")
async def update_team(team_id: str, body: dict[str, Any]) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        t = await session.get(Team, team_id)
        if t is None:
            raise HTTPException(404, "team 不存在")
        if "member_agent_ids" in body:
            await _validate_members(container, session, list(body["member_agent_ids"] or []))
        for field in ("name", "description", "member_agent_ids", "lead_hint", "playbook_id"):
            if field in body:
                setattr(t, field, body[field])
        await session.flush()
        pb = await session.get(Playbook, t.playbook_id) if t.playbook_id else None
        return _team_out(t, pb)


@router.delete("/teams/{team_id}", status_code=204)
async def delete_team(team_id: str) -> None:
    async with db_session() as session:
        t = await session.get(Team, team_id)
        if t is not None:
            await session.delete(t)


class PlaybookBody(BaseModel):
    name: str
    trigger_hint: str = ""
    stages: list[dict[str, Any]] = []


@router.put("/teams/{team_id}/playbook", status_code=201)
async def set_playbook(team_id: str, body: PlaybookBody) -> dict[str, Any]:
    container = get_container()
    try:
        stages = validate_playbook_stages(body.stages)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    async with db_session() as session:
        t = await session.get(Team, team_id)
        if t is None:
            raise HTTPException(404, "team 不存在")
        if t.playbook_id:
            pb = await session.get(Playbook, t.playbook_id)
            if pb is not None:
                pb.name = body.name
                pb.trigger_hint = body.trigger_hint
                pb.stages = stages
                await session.flush()
                return {"id": pb.id, "name": pb.name, "stages": pb.stages}
        pb = await container.registry.create_playbook(
            session, name=body.name, trigger_hint=body.trigger_hint, stages=stages
        )
        t.playbook_id = pb.id
        await session.flush()
        return {"id": pb.id, "name": pb.name, "stages": pb.stages}


@router.delete("/teams/{team_id}/playbook", status_code=204)
async def delete_playbook(team_id: str) -> None:
    """解除团队的剧本绑定并删除剧本实体。"""
    async with db_session() as session:
        t = await session.get(Team, team_id)
        if t is None:
            raise HTTPException(404, "team 不存在")
        if t.playbook_id:
            pb = await session.get(Playbook, t.playbook_id)
            t.playbook_id = None
            if pb is not None:
                await session.delete(pb)
