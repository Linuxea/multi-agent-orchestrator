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
        member_ids = member_agent_ids or []
        missing = await self.missing_agents(session, member_ids)
        if missing:
            raise ValueError(f"成员 agent 不存在: {', '.join(missing)}")
        team = Team(
            id=new_id("team"),
            slug=slug,
            name=name,
            description=description,
            member_agent_ids=member_ids,
            playbook_id=playbook_id,
            lead_hint=lead_hint,
        )
        session.add(team)
        await session.flush()
        return team

    async def get_team(self, session: AsyncSession, team_id: str) -> Team | None:
        return await session.get(Team, team_id)

    async def get_team_by_slug(self, session: AsyncSession, slug: str) -> Team | None:
        result = await session.execute(select(Team).where(Team.slug == slug))
        return result.scalar_one_or_none()

    async def find_team(self, session: AsyncSession, slug_or_id: str) -> Team | None:
        """按 slug 或 id 定位团队（编排器工具的 team 参数语义）。"""
        team = await self.get_team_by_slug(session, slug_or_id)
        if team is None:
            team = await self.get_team(session, slug_or_id)
        return team

    async def missing_agents(
        self, session: AsyncSession, agent_ids: list[str]
    ) -> list[str]:
        """返回不存在的 agent id 列表（成员校验用）。"""
        if not agent_ids:
            return []
        result = await session.execute(select(Agent.id).where(Agent.id.in_(agent_ids)))
        known = set(result.scalars().all())
        return [aid for aid in agent_ids if aid not in known]

    def resolve_team_members(
        self, team: Team, active_agents: list[Agent]
    ) -> list[Agent]:
        """团队成员中仍在注册表内且状态可用的 agent（按团队顺序）。"""
        by_id = {a.id: a for a in active_agents}
        return [by_id[aid] for aid in team.member_agent_ids if aid in by_id]

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


# 剧本阶段的规范字段（D-4.5）：key 必填且唯一；depends_on 引用已知 key。
STAGE_FIELDS: tuple[str, ...] = ("key", "name", "goal", "agent_hint", "depends_on")


def validate_playbook_stages(stages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """校验并规范化剧本 stages；非法时抛 ValueError（消息可直接展示）。"""
    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for i, raw in enumerate(stages or []):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {i + 1} 个阶段不是对象")
        stage = {k: v for k, v in raw.items() if k in STAGE_FIELDS}
        key = str(stage.get("key") or "").strip()
        if not key:
            raise ValueError(f"第 {i + 1} 个阶段缺少 key")
        if key in keys:
            raise ValueError(f"阶段 key 重复: {key}")
        keys.add(key)
        stage["key"] = key
        stage["name"] = str(stage.get("name") or key)
        stage["goal"] = str(stage.get("goal") or "")
        stage["agent_hint"] = str(stage.get("agent_hint") or "")
        deps = stage.get("depends_on") or []
        if not isinstance(deps, list):
            raise ValueError(f"阶段 {key} 的 depends_on 必须是数组")
        stage["depends_on"] = [str(d) for d in deps]
        normalized.append(stage)
    for stage in normalized:
        for dep in stage["depends_on"]:
            if dep not in keys:
                raise ValueError(f"阶段 {stage['key']} 依赖了不存在的阶段: {dep}")
            if dep == stage["key"]:
                raise ValueError(f"阶段 {stage['key']} 不能依赖自身")
    # 依赖成环检测（DFS）
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError(f"阶段依赖存在环: {node}")
        visiting.add(node)
        stage = next(s for s in normalized if s["key"] == node)
        for dep in stage["depends_on"]:
            visit(dep)
        visiting.discard(node)
        visited.add(node)

    for stage in normalized:
        visit(stage["key"])
    return normalized
