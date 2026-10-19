"""全局配置（规格 §13.1 设置项的服务端侧）。

界面可修改的运行设置存数据库（app_settings 表），此处是进程级配置与环境变量。
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class AppConfig(BaseSettings):
    """进程级配置，来自环境变量或 .env。"""

    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=".env", extra="ignore")

    # 数据库；默认 SQLite（开发），生产可指向 PostgreSQL 16+
    database_url: str = f"sqlite+aiosqlite:///{_REPO_ROOT / 'data' / 'platform.db'}"

    # 密钥加密主密钥（Fernet key）。未设置时生成临时密钥（仅适合测试，进程重启后失效）。
    master_key: str = ""

    # 监听地址（§11.4：默认仅本机）
    host: str = "127.0.0.1"
    port: int = 8000

    # 数据目录
    data_dir: Path = _REPO_ROOT / "data"

    # 唤醒合并窗口（秒）；测试中调小（见 conftest）
    wake_merge_seconds: float = 2.0

    # external_error 自动重试前等待（§5.7：等待 30 秒后重试一次）；测试调小
    external_error_retry_delay: float = 30.0

    # 模型调用重试（§9.5：429/5xx 指数退避 3 次）
    model_retry_delays: tuple[float, ...] = (1.0, 4.0, 16.0)

    # 日志
    log_level: str = "INFO"

    # 模拟模型服务（CLI 演示用；测试经 ModelProfile 直连）
    mock_model_url: str = ""


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def set_config(config: AppConfig) -> None:
    global _config
    _config = config
