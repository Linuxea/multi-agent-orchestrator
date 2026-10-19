"""API 服务入口：uvicorn app.main:app。"""

from __future__ import annotations

import logging

from app.api.app import create_app
from app.config import get_config

logging.basicConfig(level=get_config().log_level)

app = create_app()
