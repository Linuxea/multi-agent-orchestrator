# 模块文档（第一期）

模块 README 索引；规格出处标注为 §x.y。

## app/orchestrator —— 协调引擎（§5）

- `engine.py` CoordinationEngine：主 agent 事件驱动唤醒循环（§5.11）、14 个控制工具
  的校验与执行（§5.2）、计划与审批流（§5.6）、预算跟踪、恢复入口（§5.10）。
- `states.py` 任务/运行状态机（§5.5、§5.6），非法转换抛 InvalidTransition。
- `helpers.py` 状态转换 + 决策记录 + 事件写入（同一事务）。
- `scheduler.py` 三级并行上限调度（§5.10）、执行事件翻译为平台事件（§9.7）、
  崩溃/external_error 自动重试（不计入尝试次数）。
- `context.py` 运行状态摘要（§5.11）与主 agent 系统提示词（§15.1）。
- `tools.py` 控制工具 JSON Schema（agent_slug 动态枚举）。
- `replay.py` 仅凭事件流重建状态（§12.5，回放基础）。

## app/runtime —— Agent 运行时（§9）

- `executor.py` Executor 协议 + 注册表（新增执行器零侵入）。
- `lite.py` LiteExecutor：自写工具调用循环，预算兜底回报（§9.8、§5.4）。
- `model_client.py` anthropic SDK 封装：任意兼容端点、429/5xx 退避重试、流式（§9.5）。
- `workspace.py` 工作目录物化与提示词四段拼装（§9.2、§9.4）。

## app/events —— 事件存储与总线（§5.9、§12.3）

- `service.py` 载荷校验、运行内序号、事务提交后广播。
- `bus.py` 进程内发布订阅（WebSocket/CLI 消费）。

## app/registry —— 注册表（§6）

agent/团队/模板 CRUD、版本快照（§4.2）、检索；`presets.py` 11 个预置 agent。

## app/artifacts —— 交付物仓库（§5.8）

原子写入、sha256、状态流转、按引用传递、分段读取。

## app/api —— REST + WebSocket（§12）

路由按资源分组；`ws.py` 实现 §12.3 协议（订阅、补发、心跳、仅推送事件）。

## tests —— 测试基础设施与用例（§14.1、§14.2）

- `mockmodel/` 剧本驱动的 Anthropic 兼容模拟模型（YAML 作用域+步骤）。
- `mockmcp/` stdio/HTTP 模拟 MCP（echo/add/write_file/fail/slow_timeout）。
- `scripts/multi_domain_research.yaml` 端到端剧本。
- `unit/` 状态机、执行器、工作目录；`e2e/` 验收清单与协调规则逐行测试。
