"""JSON Schema 工具：把 `#/$defs/...` 引用内联展开为纯字面 schema。

Pydantic 的 ``model_json_schema()`` 生成自包含文档（``$defs`` 在文档根），
但作为子 schema 嵌入外层工具 schema 后，``#`` 按规范指向最外层文档根，
引用悬空（DeepSeek 等兼容端点会报 ``Pointer '/$defs/...' does not exist``）。
部分模型端点对 ``$ref`` 的支持也不完整，统一展开为无引用字面 schema 最稳。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """递归内联 ``#/$defs/...`` 引用并移除顶层 ``$defs``。

    循环引用（本项目的契约模型中不存在）降级为空 schema 以免无限递归；
    同名多次引用（菱形）允许正常重复展开。
    """

    defs = schema.get("$defs", {})

    def resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                if name in defs and name not in seen:
                    merged = resolve(deepcopy(defs[name]), seen | {name})
                    # $ref 的兄弟属性（description 等）合并进展开结果
                    return {**merged, **{k: v for k, v in node.items() if k != "$ref"}}
                return {}
            return {k: resolve(v, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        return node

    out = resolve(schema, frozenset())
    if isinstance(out, dict):
        out.pop("$defs", None)
        return out
    return {}
