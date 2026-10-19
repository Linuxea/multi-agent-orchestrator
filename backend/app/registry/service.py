"""注册表服务：agent、团队、模板的增删改查与版本管理（§6）。

主 agent 对注册表只读（经控制工具）；写操作来自用户 API。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AgentStatus
from app.ids import new_id
from app.models import Agent, AgentVersion, Playbook, Team


def agent_snapshot(agent: Agent) -> dict[str, Any]:
    """版本快照（§4.2）。"""
    return {
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description,
        "role_detail": agent.role_detail,
        "capability_tags": list(agent.capability_tags),
        "system_prompt": agent.system_prompt,
        "executor": agent.executor.value if hasattr(agent.executor, "value") else agent.executor,
        "model_profile_id": agent.model_profile_id,
        "input_contract": agent.input_contract,
        "output_contract": agent.output_contract,
        "default_budget": dict(agent.default_budget),
        "max_concurrency": agent.max_concurrency,
        "status": agent.status.value if hasattr(agent.status, "value") else agent.status,
        "origin": agent.origin.value if hasattr(agent.origin, "value") else agent.origin,
        "avatar": agent.avatar,
    }


class RegistryService:
    async def get_agent(self, session: AsyncSession, agent_id: str) -> Agent | None:
        result = await session.execute(select(Agent).where(Agent.id == agent_id))
        return result.scalar_one_or_none()

    async def get_agent_by_slug(self, session: AsyncSession, slug: str) -> Agent | None:
        result = await session.execute(select(Agent).where(Agent.slug == slug))
        return result.scalar_one_or_none()

    async def list_agents(
        self, session: AsyncSession, statuses: list[AgentStatus] | None = None
    ) -> list[Agent]:
        stmt = select(Agent).order_by(Agent.created_at)
        if statuses:
            stmt = stmt.where(Agent.status.in_(statuses))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def search_agents(
        self,
        session: AsyncSession,
        query: str = "",
        tags: list[str] | None = None,
        active_only: bool = True,
    ) -> list[Agent]:
        """按描述和标签检索（主 agent 的 search_agents 工具实现）。

        匹配：query 命中 name/description/role_detail 任一（大小写不敏感）；
        tags 非空时要求 capability_tags 有交集。
        """
        agents = await self.list_agents(
            session, [AgentStatus.active] if active_only else None
        )
        q = query.strip().lower()
        matched: list[Agent] = []
        for a in agents:
            if q and q not in a.name.lower() and q not in a.description.lower() and q not in a.role_detail.lower():
                continue
            if tags and not (set(tags) & set(a.capability_tags)):
                continue
            matched.append(a)
        return matched

    async def create_agent(
        self, session: AsyncSession, *, slug: str, name: str, description: str,
        role_detail: str, capability_tags: list[str], system_prompt: str,
        executor: str, model_profile_id: str,
        default_budget: dict[str, Any] | None = None,
        max_concurrency: int = 2, status: str = AgentStatus.draft.value,
        origin: str = "manual", avatar: str | None = None,
        input_contract: str = "", output_contract: str = "",
    ) -> Agent:
        from app.enums import AgentOrigin, ExecutorKind

        agent = Agent(
            id=new_id("agent"),
            slug=slug,
            name=name,
            description=description,
            role_detail=role_detail,
            capability_tags=tags_list(capability_tags),
            system_prompt=system_prompt,
            executor=ExecutorKind(executor),
            model_profile_id=model_profile_id,
            default_budget=default_budget or {},
            max_concurrency=max_concurrency,
            status=AgentStatus(status),
            origin=AgentOrigin(origin),
            avatar=avatar,
            input_contract=input_contract,
            output_contract=output_contract,
        )
        session.add(agent)
        await session.flush()
        await self.record_version(session, agent, "初始版本")
        return agent

    async def record_version(
        self, session: AsyncSession, agent: Agent, change_note: str = ""
    ) -> AgentVersion:
        version = AgentVersion(
            id=new_id("av"),
            agent_id=agent.id,
            version=agent.version,
            snapshot=agent_snapshot(agent),
            change_note=change_note,
        )
        session.add(version)
        await session.flush()
        return version

    async def update_agent(
        self, session: AsyncSession, agent: Agent, changes: dict[str, Any],
        change_note: str = "",
    ) -> Agent:
        """保存生成新版本（§6.2 保存规则）。slug 保存后不可改。"""
        if "slug" in changes and changes["slug"] != agent.slug:
            raise ValueError("slug 保存后不可修改")
        for field_name, value in changes.items():
            if field_name == "executor" and isinstance(value, str):
                from app.enums import ExecutorKind

                value = ExecutorKind(value)
            if field_name == "status" and isinstance(value, str):
                value = AgentStatus(value)
            if not hasattr(agent, field_name):
                raise ValueError(f"未知字段: {field_name}")
            setattr(agent, field_name, value)
        agent.version += 1
        await session.flush()
        await self.record_version(session, agent, change_note)
        return agent

    # --- 团队与模板 ---
    async def create_team(
        self, session: AsyncSession, *, slug: str, name: str, description: str = "",
        member_agent_ids: list[str] | None = None, playbook_id: str | None = None,
        lead_hint: str = "",
    ) -> Team:
        team = Team(
            id=new_id("team"),
            slug=slug,
            name=name,
            description=description,
            member_agent_ids=member_agent_ids or [],
            playbook_id=playbook_id,
            lead_hint=lead_hint,
        )
        session.add(team)
        await session.flush()
        return team

    async def get_team_by_slug(self, session: AsyncSession, slug: str) -> Team | None:
        result = await session.execute(select(Team).where(Team.slug == slug))
        return result.scalar_one_or_none()

    async def list_teams(self, session: AsyncSession) -> list[Team]:
        result = await session.execute(select(Team).order_by(Team.created_at))
        return list(result.scalars().all())

    async def create_playbook(
        self, session: AsyncSession, *, name: str, trigger_hint: str = "",
        stages: list[dict[str, Any]] | None = None,
    ) -> Playbook:
        pb = Playbook(
            id=new_id("pb"),
            name=name,
            trigger_hint=trigger_hint,
            stages=stages or [],
        )
        session.add(pb)
        await session.flush()
        return pb

    async def list_playbooks(self, session: AsyncSession) -> list[Playbook]:
        result = await session.execute(select(Playbook).order_by(Playbook.created_at))
        return list(result.scalars().all())

    async def get_playbook(self, session: AsyncSession, playbook_id: str) -> Playbook | None:
        result = await session.execute(select(Playbook).where(Playbook.id == playbook_id))
        return result.scalar_one_or_none()


def tags_list(tags: list[str] | None) -> list[str]:
    return [t.strip() for t in (tags or []) if t.strip()]
