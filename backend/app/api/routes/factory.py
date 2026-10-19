"""Agent 工厂 API：起草 agent / 团队（草稿不落库，§14.5）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.bootstrap import get_container
from app.factory.service import FactoryError
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["factory"])


class AgentDraftBody(BaseModel):
    requirement: str = Field(min_length=2, max_length=2000)
    executor: str = "lite"
    model_profile_id: str | None = None


class TeamDraftBody(BaseModel):
    requirement: str = Field(min_length=2, max_length=2000)
    model_profile_id: str | None = None


@router.post("/factory/agents")
async def draft_agent(body: AgentDraftBody) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        try:
            return await container.factory.draft_agent(
                session,
                body.requirement,
                executor=body.executor,
                model_profile_id=body.model_profile_id,
            )
        except FactoryError as e:
            raise HTTPException(422, str(e)) from e


@router.post("/factory/teams")
async def draft_team(body: TeamDraftBody) -> dict[str, Any]:
    container = get_container()
    async with db_session() as session:
        try:
            return await container.factory.draft_team(
                session,
                body.requirement,
                model_profile_id=body.model_profile_id,
            )
        except FactoryError as e:
            raise HTTPException(422, str(e)) from e
