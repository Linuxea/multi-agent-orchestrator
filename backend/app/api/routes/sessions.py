"""会话与消息（§12.2）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.bootstrap import get_container
from app.ids import new_id
from app.models import Message, Run, Session
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["sessions"])


class SessionOut(BaseModel):
    id: str
    title: str
    status: str
    default_team_id: str | None = None
    created_at: str

    @classmethod
    def from_entity(cls, s: Session) -> SessionOut:
        return cls(
            id=s.id,
            title=s.title,
            status=s.status.value,
            default_team_id=s.default_team_id,
            created_at=s.created_at.isoformat(),
        )


class SessionCreate(BaseModel):
    title: str = "新会话"


class MessageOut(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    run_id: str | None = None
    created_at: str


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions() -> list[SessionOut]:
    async with db_session() as session:
        result = await session.execute(select(Session).order_by(Session.created_at.desc()))
        return [SessionOut.from_entity(s) for s in result.scalars().all()]


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(body: SessionCreate) -> SessionOut:
    async with db_session() as session:
        s = Session(id=new_id("sess"), title=body.title)
        session.add(s)
        await session.flush()
        return SessionOut.from_entity(s)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: str) -> SessionOut:
    async with db_session() as session:
        s = await session.get(Session, session_id)
        if s is None:
            raise HTTPException(404, "session 不存在")
        return SessionOut.from_entity(s)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    async with db_session() as session:
        s = await session.get(Session, session_id)
        if s is not None:
            await session.delete(s)


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(session_id: str, limit: int = 200) -> list[MessageOut]:
    async with db_session() as session:
        result = await session.execute(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at)
            .limit(min(limit, 500))
        )
        return [
            MessageOut(
                id=m.id,
                session_id=m.session_id,
                role=m.role.value,
                content=m.content,
                run_id=m.run_id,
                created_at=m.created_at.isoformat(),
            )
            for m in result.scalars().all()
        ]


class SendMessageBody(BaseModel):
    content: str = Field(min_length=1)


@router.post("/sessions/{session_id}/messages", response_model=MessageOut, status_code=201)
async def send_message(session_id: str, body: SendMessageBody) -> MessageOut:
    container = get_container()
    async with db_session() as session:
        s = await session.get(Session, session_id)
        if s is None:
            raise HTTPException(404, "session 不存在")
    message, run = await container.engine.submit_user_message(session_id, body.content)
    return MessageOut(
        id=message.id,
        session_id=session_id,
        role="user",
        content=message.content,
        run_id=run.id,
        created_at=message.created_at.isoformat(),
    )


@router.get("/sessions/{session_id}/runs")
async def list_session_runs(session_id: str) -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(
            select(Run).where(Run.session_id == session_id).order_by(Run.created_at.desc())
        )
        return [
            {"id": r.id, "status": r.status.value, "outcome": r.outcome.value if r.outcome else None}
            for r in result.scalars().all()
        ]
