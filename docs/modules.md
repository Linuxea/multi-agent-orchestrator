# 模块文档（第一期）

模块 README 索引；规格出处标注为 §x.y。

## frontend/src —— 完整界面（§12，第三期）

- `api/` REST 客户端（密钥头/错误形态/blob 下载）、端点封装、WS 客户端
  （订阅/补发/心跳 pong/seq 去重/指数退避重连/4401 处理，§12.3）。
- `state/` AppProvider（认证探测/登录/登出/WS 状态/toast）、useAsync 数据 hook。
- `components/` 事件遥测栏（签名元素：seq 刻度+分类色标+载荷展开+在播光标）、
  运行时间线（消息/计划卡/提问卡/终局卡组装）、任务板+任务抽屉（§10.9 干预）、
  状态徽章（中文映射）、用量条、抽屉。
- `pages/` 运行工作台（会话轨+舞台+遥测三栏、输入区编队选择）、审批中心（remember 三档）、
  Agent 注册表（版本/回滚/绑定/工厂起草）、工具库、模型配置（体检）、密钥、
  团队（结构化剧本编辑器+工厂编队）、设置、登录。
- `styles/` 设计 token「夜航调度室」+ 布局样式。

## app/api/auth —— 访问密码（§11.4，第三期）

- 设置 access_password 后 /api/v1/* 需密钥头（Bearer / X-Platform-Key）；
  auth/status 免认证探测、auth/verify 校验；WS 走 ?key=，错误以 4401 关闭。
  设置读取 5s 缓存 + 常数时间比较（D-3.2）。

## app/orchestrator —— 协调引擎（§5）

- `engine.py` CoordinationEngine：主 agent 事件驱动唤醒循环（§5.11）、14 个控制工具
  的校验与执行（§5.2）、计划与审批流（§5.6）、预算跟踪、恢复入口（§5.10）。
- `states.py` 任务/运行状态机（§5.5、§5.6），非法转换抛 InvalidTransition。
- `helpers.py` 状态转换 + 决策记录 + 事件写入（同一事务）。
- `scheduler.py` 三级并行上限调度（§5.10）、执行事件翻译为平台事件（§9.7，含脱敏）、
  崩溃/external_error 自动重试（不计入尝试次数）、工具门 `_tool_gate`
  （§8.9 权限链 + §11 审批接入）、尝试物化（工作目录 + 工具上下文）。
- `context.py` 运行状态摘要（§5.11）与主 agent 系统提示词（§15.1）；
  会话编队段（团队/成员/剧本阶段注入，第四期 D-4.4）。
- `tools.py` 控制工具 JSON Schema（agent_slug 动态枚举）。
- `replay.py` 仅凭事件流重建状态（§12.5，回放基础）。

## app/tools —— 工具库（§8，第二期）

- `permission.py` 权限判定链（§8.9）：平台/只读放行 → 注册与停用 → 绑定要求 →
  子工具范围 → 运行级记忆 → 绑定权限覆盖默认 → 高危命令表。
- `mcp_client.py` 真实 MCP 客户端封装：stdio / streamable HTTP / SSE 连接、
  tools 列举与调用、连接测试。
- `service.py` ToolService：绑定 CRUD、连接测试、MCP 子工具发现、
  agent 工具物化（defs / mcp_specs / skills / commands / hooks + 密钥解析）、
  `.claude` 工作目录资产落盘；内置工具种子（bash/write/edit/web_fetch/web_search）。
- `secrets_runtime.py` `{"$secret": name}` 引用解析（更新 last_used_at）与
  SecretMasker 脱敏器。

## app/approvals —— 审批门（§11，第二期）

- ask 权限调用 → Approval(pending) + attempt/task → awaiting_approval；
  等待 future，支持 resolve（用户决定唤醒）与超时 expired；
  remember = once / this_run（运行内放行集）/ always_for_agent（绑定持久化 allow）。

## app/runtime —— Agent 运行时（§9）

- `executor.py` Executor 协议 + 注册表（新增执行器零侵入）。
- `lite.py` LiteExecutor：自写工具调用循环；submit_report + 绑定工具
  （经权限门 + Toolbox 执行）；预算兜底回报（§9.8、§5.4）。
- `sdk.py` SdkExecutor：claude-agent-sdk 0.2.x 适配（选项装配、事件翻译、
  can_use_tool 权限门、submit_report 进程内 MCP 服务、运行时可用性检测）。
- `toolbox.py` 工具执行箱：MCP 惰性连接复用、custom_http、custom_python（受设置开关）。
- `reporting.py` §5.4 兜底回报（预算耗尽 / 未提交回报）。
- `model_client.py` anthropic SDK 封装：任意兼容端点、429/5xx 退避重试、流式（§9.5）。
- `workspace.py` 工作目录物化与提示词四段拼装（§9.2、§9.4）。

## app/events —— 事件存储与总线（§5.9、§12.3）

- `service.py` 载荷校验、运行内序号、事务提交后广播。
- `bus.py` 进程内发布订阅（WebSocket/CLI 消费）。

## app/factory —— Agent 工厂（§14.5，第四期）

- `service.py` FactoryService：模型起草 agent / 团队定义（草稿不落库，D-4.2）——
  agent 草稿（提示词/职责/标签/预算/契约 + slug 冲突检测）、团队编队草稿
  （现有花名册选成员 + 剧本阶段 + 能力缺口）；JSON 解析容忍代码块；
  每次生成发 factory.generated（全局通道）。模型解析链与编排器共享
  （`app/runtime/model_config.py`）。

## app/registry —— 注册表（§6）

agent/团队/模板 CRUD、版本快照（§4.2）、检索；`presets.py` 11 个预置 agent；
`validate_playbook_stages` 剧本阶段校验（key 唯一/依赖存在/无环，第四期 D-4.5）。

## app/artifacts —— 交付物仓库（§5.8）

原子写入、sha256、状态流转、按引用传递、分段读取。

## app/api —— REST + WebSocket（§12）

路由按资源分组；`ws.py` 实现 §12.3 协议（订阅、补发、心跳、仅推送事件）；
`auth.py` 访问密码中间件（§11.4）；`app.py` 启动种子（D-3.4）+ 前端 dist 直出（D-3.3）。

## tests —— 测试基础设施与用例（§14.1、§14.2、§14.3）

- `mockmodel/` 剧本驱动的 Anthropic 兼容模拟模型（YAML 作用域+步骤）。
- `mockmcp/` stdio/HTTP 模拟 MCP（echo/add/write_file/fail/slow_timeout）；
  已对照真实 MCP 客户端 SDK 验证（发现并修复 HTTP 端点注解缺陷，D-2.3）。
- `fakesdk.py` 伪 claude-agent-sdk（SdkExecutor 单元测试）。
- `scripts/multi_domain_research.yaml` 端到端剧本。
- `unit/` 状态机、执行器（lite/sdk）、工作目录、权限链、密钥、MCP 客户端验证、
  访问密码、Agent 工厂（起草/过滤/校验）；
  `e2e/` 验收清单（第一期 §14.2 / 第二期 §14.3）、协调规则逐行测试、工具流与审批、
  界面链路冒烟（第三期，真实进程：dist 直出→种子→会话→计划批准→终态→WS→密码强制）；
  团队编队（第四期：阶段校验/偏离记录/团队过滤）与其界面链路冒烟（工厂→编队→剧本映射）。
