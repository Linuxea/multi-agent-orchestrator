"""Agent 工厂（§14.5 第四期）：用模型起草 agent / 团队定义。

语义（D-4.2）：工厂只产出「草稿」，不直接落库——用户在编辑器里审阅修改后
经既有创建接口保存（agent 以 origin=factory 留痕）。每次成功生成发一条
factory.generated 事件（全局通道，run_id=None，供审计）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AgentStatus, ExecutorKind
from app.events.service import EventService
from app.registry.service import RegistryService, validate_playbook_stages
from app.runtime.model_client import ModelCaller, ModelError
from app.runtime.model_config import resolve_model_config
from app.settings import SettingsService

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

AGENT_FACTORY_MARKER = "Agent 工厂"  # 模拟模型剧本作用域标记
TEAM_FACTORY_MARKER = "团队工厂"

AGENT_DRAFT_SYSTEM = f"""你是{AGENT_FACTORY_MARKER}的装配师。根据用户对 agent 的需求，起草一份完整的 agent 定义。
只输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），字段：
- slug：小写字母/数字/连字符，能体现职责
- name：中文名（≤6 字）
- description：一句话职责（≤50 字）
- role_detail：「擅长：…\\n不做：…」两行，边界清晰
- capability_tags：3 个左右英文小写标签
- system_prompt：可直接使用的完整系统提示词（第二人称、包含工作方法与如实回报的要求）
- input_contract / output_contract：输入与交付物约定各一句
- default_budget：{"{max_turns, max_cost, max_wall_seconds}"}，常规任务 max_turns≤30、max_cost≤1.0
- avatar：一个 emoji
"""

TEAM_DRAFT_SYSTEM = f"""你是{TEAM_FACTORY_MARKER}的编队师。根据用户需求，从现有 agent 花名册中挑选成员组成团队，
并起草配套剧本（playbook）。
只输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），字段：
- slug / name / description：团队标识（slug 小写连字符）、中文名与一句话描述
- member_slugs：从花名册中挑选的成员 slug（只允许花名册内已有的；宁缺毋滥）
- lead_hint：组长提示一句话（谁适合牵头）
- missing_capabilities：花名册覆盖不了的能力清单（没有则为空数组）
- playbook：{"{name, trigger_hint, stages}"}；stages 是 3-5 个阶段，每个阶段
  {"{key, name, goal, agent_hint, depends_on}"}：key 形如 S1、S2；agent_hint 写该阶段
  适合的成员 slug 或标签；depends_on 引用更早阶段的 key（可为空数组）
"""


class FactoryError(RuntimeError):
    """工厂起草失败（消息可直接展示给用户）。"""


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象（容忍 markdown 代码块与前后杂文）。"""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise FactoryError("模型输出中未找到 JSON 对象")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as e:
        raise FactoryError(f"模型输出的 JSON 无法解析: {e}") from e
    if not isinstance(data, dict):
        raise FactoryError("模型输出不是 JSON 对象")
    return data


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    return slug or "agent"


def _normalize_slug(raw: Any, fallback: str) -> str:
    slug = str(raw or "").strip()
    if not _SLUG_RE.match(slug):
        slug = _slugify(slug if slug else fallback)
    return slug[:100]


class FactoryService:
    def __init__(
        self,
        events: EventService,
        registry: RegistryService,
        settings: SettingsService,
    ) -> None:
        self.events = events
        self.registry = registry
        self.settings = settings

    async def _call_model_json(
        self, session: AsyncSession, *, system: str, user: str,
        model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        cfg = await resolve_model_config(
            session, self.settings, orchestrator=True, profile_id=model_profile_id
        )
        if cfg is None:
            raise FactoryError("尚未配置任何模型，无法起草")
        caller = ModelCaller(cfg)
        try:
            response = await caller.create(
                system=system,
                messages=[{"role": "user", "content": user}],
                max_tokens=2048,
            )
        except ModelError as e:
            raise FactoryError(f"模型调用失败: {e}") from e
        text = "".join(b.text for b in response.blocks if b.type == "text")
        return _extract_json(text)

    async def _emit(
        self, session: AsyncSession, kind: str,
        *, input_summary: str = "", output_summary: str = "",
    ) -> None:
        await self.events.emit(
            session,
            type="factory.generated",
            actor="factory",
            payload={
                "kind": kind,
                "input_summary": input_summary,
                "output_summary": output_summary,
            },
        )

    # ------------------------------------------------------------------
    async def draft_agent(
        self,
        session: AsyncSession,
        requirement: str,
        *,
        executor: str = "lite",
        model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        """起草单个 agent 定义（不落库）。"""
        data = await self._call_model_json(
            session,
            system=AGENT_DRAFT_SYSTEM,
            user=f"agent 需求：{requirement}",
            model_profile_id=model_profile_id,
        )
        name = str(data.get("name") or requirement[:24]).strip() or "新 agent"
        slug = _normalize_slug(data.get("slug"), name)
        budget_raw = data.get("default_budget")
        budget = budget_raw if isinstance(budget_raw, dict) else {}
        draft: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "description": str(data.get("description") or "")[:200] or f"{name}（工厂起草）",
            "role_detail": str(data.get("role_detail") or ""),
            "capability_tags": [str(t) for t in (data.get("capability_tags") or []) if str(t).strip()][:8],
            "system_prompt": str(data.get("system_prompt") or "").strip()
            or f"你是{name}。{requirement}",
            "input_contract": str(data.get("input_contract") or ""),
            "output_contract": str(data.get("output_contract") or ""),
            "default_budget": {
                "max_turns": int(budget.get("max_turns") or 30),
                "max_cost": float(budget.get("max_cost") or 1.0),
                "max_wall_seconds": int(budget.get("max_wall_seconds") or 1800),
            },
            "avatar": str(data.get("avatar") or "")[:8] or None,
            "executor": ExecutorKind(executor).value,
        }
        existing = await self.registry.get_agent_by_slug(session, draft["slug"])
        draft["slug_available"] = existing is None
        if existing is not None:
            draft["slug_suggestion"] = f"{draft['slug']}-2"
        await self._emit(
            session,
            "agent_draft",
            input_summary=requirement[:200],
            output_summary=f"{draft['name']}（{draft['slug']}）",
        )
        return draft

    # ------------------------------------------------------------------
    async def draft_team(
        self,
        session: AsyncSession,
        requirement: str,
        *,
        model_profile_id: str | None = None,
    ) -> dict[str, Any]:
        """起草团队编队（成员 + 剧本骨架，不落库）。"""
        agents = await self.registry.list_agents(session, [AgentStatus.active])
        roster = "\n".join(
            f"- {a.slug}｜{a.name}｜{a.description}｜标签: {','.join(a.capability_tags)}"
            for a in agents
        ) or "（花名册为空）"
        data = await self._call_model_json(
            session,
            system=TEAM_DRAFT_SYSTEM,
            user=f"团队需求：{requirement}\n\n现有 agent 花名册：\n{roster}",
            model_profile_id=model_profile_id,
        )
        name = str(data.get("name") or requirement[:24]).strip() or "新团队"
        slug = _normalize_slug(data.get("slug"), name)

        by_slug = {a.slug: a for a in agents}
        member_slugs = [
            str(s) for s in (data.get("member_slugs") or []) if str(s) in by_slug
        ]
        dropped = [
            str(s) for s in (data.get("member_slugs") or []) if str(s) not in by_slug
        ]
        playbook_raw = data.get("playbook") or {}
        stages_raw = (
            playbook_raw.get("stages") if isinstance(playbook_raw, dict) else None
        ) or []
        try:
            stages = validate_playbook_stages(list(stages_raw))
        except ValueError as e:
            raise FactoryError(f"起草的剧本阶段不合规: {e}") from e

        draft: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "description": str(data.get("description") or "")[:200],
            "lead_hint": str(data.get("lead_hint") or ""),
            "member_agent_ids": [by_slug[s].id for s in member_slugs],
            "member_slugs": member_slugs,
            "dropped_member_slugs": dropped,
            "missing_capabilities": [str(c) for c in (data.get("missing_capabilities") or [])],
            "playbook": {
                "name": str(playbook_raw.get("name") or f"{name}剧本") if isinstance(playbook_raw, dict) else f"{name}剧本",
                "trigger_hint": str(playbook_raw.get("trigger_hint") or "") if isinstance(playbook_raw, dict) else "",
                "stages": stages,
            },
        }
        existing = await self.registry.get_team_by_slug(session, slug)
        draft["slug_available"] = existing is None
        if existing is not None:
            draft["slug_suggestion"] = f"{slug}-2"
        await self._emit(
            session,
            "team_draft",
            input_summary=requirement[:200],
            output_summary=f"{name}（成员 {len(member_slugs)}、阶段 {len(stages)}）",
        )
        return draft
