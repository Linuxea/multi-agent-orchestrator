"""密钥运行时（§8.8、§11）：`$secret` 引用解析 + 事件/日志脱敏。

- 工具配置中的 `{"$secret": "名称"}` 占位在物化时解密替换；
- 所有进入事件流/审批/日志的文本经 SecretMasker 脱敏（密文值 → ***）。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import secrets as secrets_util
from app.models import Secret
from app.utils import utcnow

SECRET_REF_KEY = "$secret"


def _resolve_value(value: Any, lookup: dict[str, str]) -> Any:
    if isinstance(value, dict) and set(value) == {SECRET_REF_KEY}:
        name = str(value[SECRET_REF_KEY])
        if name not in lookup:
            raise KeyError(f"未找到密钥: {name}")
        return lookup[name]
    if isinstance(value, dict):
        return {k: _resolve_value(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, lookup) for v in value]
    return value


def _collect_ref_names(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        if set(value) == {SECRET_REF_KEY}:
            out.add(str(value[SECRET_REF_KEY]))
            return
        for v in value.values():
            _collect_ref_names(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_ref_names(v, out)


def looks_like_secret_ref(value: Any) -> bool:
    """配置中是否包含 $secret 引用（用于禁止把引用存进可见文本）。"""
    names: set[str] = set()
    _collect_ref_names(value, names)
    return bool(names)


class SecretMasker:
    """把已知密钥明文替换为掩码；用于事件载荷与审批参数。"""

    def __init__(self, values: list[str]) -> None:
        # 短值（<=4 字符）无掩码意义，跳过
        self._values = sorted({v for v in values if len(v) > 4}, key=len, reverse=True)

    def mask_text(self, text: str) -> str:
        for v in self._values:
            if v in text:
                text = text.replace(v, "***")
        return text

    def mask_data(self, data: Any) -> Any:
        if isinstance(data, str):
            return self.mask_text(data)
        if isinstance(data, dict):
            return {k: self.mask_data(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.mask_data(v) for v in data]
        return data

    def __bool__(self) -> bool:
        return bool(self._values)

    @property
    def values(self) -> list[str]:
        return list(self._values)


async def load_masker(session: AsyncSession) -> SecretMasker:
    """加载全部密钥明文构建脱敏器（每次物化尝试时调用一次）。"""
    result = await session.execute(select(Secret))
    values: list[str] = []
    for s in result.scalars().all():
        try:
            values.append(secrets_util.decrypt(s.encrypted_value))
        except ValueError:
            continue  # 主密钥变更后无法解密的密钥跳过
    return SecretMasker(values)


async def resolve_secret_refs(
    session: AsyncSession, config: Any
) -> tuple[Any, list[str]]:
    """解析配置中的 $secret 引用；返回（新配置, 使用的密钥名列表）。

    原配置不被修改；找不到密钥抛 KeyError。同时更新 Secret.last_used_at。
    """
    names: set[str] = set()
    _collect_ref_names(config, names)
    if not names:
        return config, []
    lookup: dict[str, str] = {}
    for name in sorted(names):
        result = await session.execute(select(Secret).where(Secret.name == name))
        secret = result.scalar_one_or_none()
        if secret is None:
            raise KeyError(f"未找到密钥: {name}")
        try:
            lookup[name] = secrets_util.decrypt(secret.encrypted_value)
        except ValueError as e:
            raise KeyError(f"密钥解密失败: {name}") from e
        secret.last_used_at = utcnow()
    return _resolve_value(config, lookup), sorted(names)


def random_mask_token() -> str:
    """生成一次性占位令牌（避免把真实值放进模板字符串）。"""
    return uuid4().hex[:8]
