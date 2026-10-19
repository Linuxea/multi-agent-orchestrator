"""工具库 API（第一期骨架；完整功能第二期）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.models import Tool
from app.orchestrator.helpers import db_session

router = APIRouter(tags=["tools"])


class ToolCreate(BaseModel):
    kind: str
    slug: str
    name: str
    description: str = ""
    config: dict[str, Any] = {}
    default_permission: str = "allow"


def _tool_out(t: Tool) -> dict[str, Any]:
    return {
        "id": t.id,
        "kind": t.kind.value,
        "slug": t.slug,
        "name": t.name,
        "description": t.description,
        "config": t.config,
        "default_permission": t.default_permission.value,
        "status": t.status.value,
        "last_test": t.last_test,
        "discovered": t.discovered,
        "tags": t.tags,
    }


@router.get("/tools")
async def list_tools() -> list[dict[str, Any]]:
    async with db_session() as session:
        result = await session.execute(select(Tool).order_by(Tool.created_at))
        return [_tool_out(t) for t in result.scalars().all()]


@router.post("/tools", status_code=201)
async def create_tool(body: ToolCreate) -> dict[str, Any]:
    from app.enums import ToolKind, ToolPermission
    from app.ids import new_id

    async with db_session() as session:
        existing = await session.execute(select(Tool).where(Tool.slug == body.slug))
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(409, f"slug 已存在: {body.slug}")
        tool = Tool(
            id=new_id("tool"),
            kind=ToolKind(body.kind),
            slug=body.slug,
            name=body.name,
            description=body.description,
            config=body.config,
            default_permission=ToolPermission(body.default_permission),
        )
        session.add(tool)
        await session.flush()
        return _tool_out(tool)


@router.patch("/tools/{tool_id}")
async def update_tool(tool_id: str, body: dict[str, Any]) -> dict[str, Any]:
    async with db_session() as session:
        t = await session.get(Tool, tool_id)
        if t is None:
            raise HTTPException(404, "tool 不存在")
        for field in ("name", "description", "config", "tags"):
            if field in body:
                setattr(t, field, body[field])
        await session.flush()
        return _tool_out(t)


@router.delete("/tools/{tool_id}", status_code=204)
async def delete_tool(tool_id: str) -> None:
    async with db_session() as session:
        t = await session.get(Tool, tool_id)
        if t is not None:
            if t.kind.value == "builtin":
                raise HTTPException(409, "内置工具不可删除，只能停用")
            await session.delete(t)


@router.post("/tools/{tool_id}/test")
async def test_tool(tool_id: str) -> dict[str, Any]:
    """连接测试（第二期实现真实测试；第一期返回未实现说明）。"""
    async with db_session() as session:
        t = await session.get(Tool, tool_id)
        if t is None:
            raise HTTPException(404, "tool 不存在")
        return {"status": "untested", "message": "工具测试在第二期实现"}
