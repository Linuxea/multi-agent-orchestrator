"""模型配置解析（第四期抽取）：编排器、工厂等共用同一条回退链。

设置项 orchestrator_model_profile_id → 默认编排配置（is_default_orchestrator）
→ 库中第一个配置；显式 profile_id 时直接解析该配置。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelProfile
from app.models import Secret as SecretModel
from app.runtime.types import ModelConfig
from app.settings import SettingsService
from app.utils import utcnow


def model_config_from_profile(profile: ModelProfile, api_key: str) -> ModelConfig:
    return ModelConfig(
        profile_id=profile.id,
        model=profile.model,
        base_url=profile.base_url,
        api_key=api_key,
        max_output_tokens=profile.max_output_tokens,
        temperature=profile.temperature,
        thinking=profile.thinking.value,
        context_window=profile.context_window,
        price_input=float(profile.price_input or 0),
        price_output=float(profile.price_output or 0),
    )


async def _api_key_for(session: AsyncSession, profile: ModelProfile) -> str:
    if not profile.api_key_secret_id:
        return ""
    from app import secrets as secrets_util

    secret = await session.get(SecretModel, profile.api_key_secret_id)
    if secret is None:
        return ""
    try:
        api_key = secrets_util.decrypt(secret.encrypted_value)
        secret.last_used_at = utcnow()  # type: ignore[assignment]
        return api_key
    except ValueError:
        return ""


async def resolve_model_config(
    session: AsyncSession,
    settings: SettingsService,
    *,
    orchestrator: bool = True,
    profile_id: str | None = None,
) -> ModelConfig | None:
    profile: ModelProfile | None = None
    if profile_id:
        profile = await session.get(ModelProfile, profile_id)
    if profile is None and orchestrator:
        settings_data = await settings.get(session)
        configured = settings_data.get("orchestrator_model_profile_id")
        if configured:
            result = await session.execute(
                select(ModelProfile).where(ModelProfile.id == configured)
            )
            profile = result.scalar_one_or_none()
    if profile is None and orchestrator:
        result = await session.execute(
            select(ModelProfile).where(ModelProfile.is_default_orchestrator.is_(True)).limit(1)
        )
        profile = result.scalar_one_or_none()
    if profile is None:
        result = await session.execute(select(ModelProfile).limit(1))
        profile = result.scalar_one_or_none()
    if profile is None:
        return None
    return model_config_from_profile(profile, await _api_key_for(session, profile))
