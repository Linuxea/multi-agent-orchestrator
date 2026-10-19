"""伪 claude-agent-sdk：按 0.2.x 关键接口模拟，用于 SdkExecutor 单元测试。"""

from __future__ import annotations

import types
from typing import Any

LAST: dict[str, Any] = {"server": None, "clients": [], "can_use_calls": [], "permission_results": []}


class PermissionResultAllow:
    def __repr__(self) -> str:  # pragma: no cover
        return "PermissionResultAllow()"


class PermissionResultDeny:
    def __init__(self, message: str = "") -> None:
        self.message = message


class ClaudeAgentOptions:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.kwargs = kwargs


class FakeTool:
    def __init__(self, name: str, description: str, input_schema: Any, fn: Any) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.fn = fn


def tool(name: str, description: str, input_schema: Any) -> Any:
    def deco(fn: Any) -> FakeTool:
        return FakeTool(name, description, input_schema, fn)

    return deco


def create_sdk_mcp_server(name: str, version: str = "1.0.0", tools: Any = None) -> dict[str, Any]:
    server = {"type": "sdk", "name": name, "version": version, "tools": tools or []}
    LAST["server"] = server
    return server


class FakeClient:
    """按脚本回放消息；支持 dict 步骤：

    - {"invoke": (tool_name, arguments)} → 调 options.can_use_tool
    - {"call_platform": kwargs} → 调 platform 工具回调（submit_report）
    - {"interrupt": True}
    - {"sleep": seconds}
    - 其他 → 原样 yield（用 SimpleNamespace 构造的伪消息）
    """

    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompt: str | None = None
        self.interrupted = False
        self.disconnected = False
        self.queries: list[str] = []
        self.script: list[Any] = []
        LAST["clients"].append(self)

    async def connect(self, prompt: Any = None) -> None:
        self.prompt = prompt

    async def receive_response(self):
        # 等待测试注入脚本（有上限，避免空转）
        import asyncio
        import time as _time

        deadline = _time.monotonic() + 5.0
        while not self.script and _time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        while self.script:
            step = self.script.pop(0)
            if isinstance(step, dict) and "invoke" in step:
                name, args = step["invoke"]
                LAST["can_use_calls"].append((name, args))
                result = await self.options.can_use_tool(name, args, None)
                LAST["permission_results"].append(result)
                continue
            if isinstance(step, dict) and "call_platform" in step:
                fake_tool = LAST["server"]["tools"][0]
                result = fake_tool.fn(**step["call_platform"])
                if hasattr(result, "__await__"):
                    result = await result
                yield types.SimpleNamespace(kind="platform_result", result=result)
                continue
            if isinstance(step, dict) and step.get("interrupt"):
                self.interrupted = True
                continue
            if isinstance(step, dict) and "sleep" in step:
                import asyncio

                await asyncio.sleep(step["sleep"])
                continue
            yield step

    async def query(self, text: Any, session_id: str = "default") -> None:
        self.queries.append(str(text))

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True


def build_fake_sdk() -> types.ModuleType:
    mod = types.ModuleType("claude_agent_sdk")
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ClaudeSDKClient = FakeClient
    mod.PermissionResultAllow = PermissionResultAllow
    mod.PermissionResultDeny = PermissionResultDeny
    mod.tool = tool
    mod.create_sdk_mcp_server = create_sdk_mcp_server
    return mod


def reset() -> None:
    LAST["server"] = None
    LAST["clients"] = []
    LAST["can_use_calls"] = []
    LAST["permission_results"] = []


def text_msg(text: str) -> Any:
    return _named(
        "AssistantMessage",
        content=[_named("TextBlock", text=text)],
        usage={"input_tokens": 100, "output_tokens": 20},
    )


def thinking_msg(text: str) -> Any:
    return _named(
        "AssistantMessage",
        content=[_named("ThinkingBlock", thinking=text)],
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def tool_use_msg(tool_id: str, name: str, tool_input: dict[str, Any]) -> Any:
    return _named(
        "AssistantMessage",
        content=[_named("ToolUseBlock", id=tool_id, name=name, input=tool_input)],
        usage={"input_tokens": 30, "output_tokens": 10},
    )


def tool_result_msg(tool_id: str, content: Any, is_error: bool = False) -> Any:
    return _named(
        "UserMessage",
        content=[_named("ToolResultBlock", tool_use_id=tool_id, content=content, is_error=is_error)],
    )


def result_msg(**over: Any) -> Any:
    base = {
        "subtype": "success", "is_error": False,
        "num_turns": 3, "usage": {"input_tokens": 300, "output_tokens": 60},
        "result": "done", "session_id": "sess-fake",
    }
    base.update(over)
    return _named("ResultMessage", **base)


def stream_text(text: str) -> Any:
    return _named(
        "StreamEvent",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    )


def _named(class_name: str, **attrs: Any) -> Any:
    cls = type(class_name, (types.SimpleNamespace,), {})
    return cls(**attrs)
