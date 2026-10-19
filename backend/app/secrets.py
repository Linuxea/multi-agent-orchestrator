"""密钥加密（§4.7、§8.8）：Fernet，主密钥来自 PLATFORM_MASTER_KEY。"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_config

_fernet: Fernet | None = None
_warned = False


def _get_fernet() -> Fernet:
    global _fernet, _warned
    if _fernet is None:
        key = get_config().master_key
        if not key:
            if not _warned:
                import logging

                logging.getLogger(__name__).warning(
                    "未设置 PLATFORM_MASTER_KEY，使用进程临时密钥（重启后旧密钥不可解密）"
                )
                _warned = True
            key = Fernet.generate_key().decode()
        _fernet = Fernet(key if isinstance(key, bytes) else key.encode())
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("密钥解密失败（主密钥可能已变更）") from e


def mask(value: str) -> str:
    """界面与日志显示：只留末四位。"""
    if len(value) <= 4:
        return "***"
    return "*" * (len(value) - 4) + value[-4:]
