"""模拟模型服务启动入口：python -m tests.mockmodel.server_main --port 8765 [script.yaml]"""

from __future__ import annotations

import argparse
import threading
from typing import Any

import uvicorn

from tests.mockmodel.server import MockModel, create_mock_app, load_yaml_file


def serve(
    port: int, script: str | None = None, host: str = "127.0.0.1"
) -> tuple[MockModel, Any, threading.Thread]:
    state = MockModel()
    if script:
        state.load(load_yaml_file(script))
    app = create_mock_app(state)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return state, server, thread


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--script", default=None)
    args = parser.parse_args()
    state, server, thread = serve(args.port, args.script)
    print(f"模拟模型服务已启动: http://127.0.0.1:{args.port}（剧本: {args.script or '无'}）")
    thread.join()
    _ = (state, server)


if __name__ == "__main__":
    main()
