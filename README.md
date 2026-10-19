# 多 Agent 协调平台

规格书驱动的开发仓库。需求唯一依据：`docs/spec.md`（第 14 节分期）。
实现过程中的自行决策与规格歧义裁决见 `DECISIONS.md`。

## 结构

```
backend/   Python 3.12 + FastAPI + SQLAlchemy(async) + anthropic SDK
frontend/  React 19 + TypeScript + Vite（第三期界面）
data/      运行时数据（工作目录、交付物、SQLite）——git 忽略
docs/      规格与模块文档
```

## 快速开始（第一期：后端 + CLI）

```bash
cd backend
uv sync --extra dev
```

### 本地演示（模拟模型，无需密钥）

```bash
# 终端 1：启动剧本驱动的模拟模型服务
uv run python -m tests.mockmodel.server_main --port 8765 --script tests/scripts/multi_domain_research.yaml

# 终端 2：初始化（建库 + 11 个预置 agent + 模拟模型配置）并运行
MOCK_MODEL_URL=http://127.0.0.1:8765 uv run platform init
MOCK_MODEL_URL=http://127.0.0.1:8765 uv run platform run --auto-approve "调研 2026 年 AI 编程工具市场并写一份简报"
```

终端会实时打印事件流（计划→派发→执行→复核→汇总），结束输出最终答复、缺口与用量。

### 接真实模型（DeepSeek Anthropic 兼容端点）

```bash
# 先通过 API/DB 创建指向 https://api.deepseek.com/anthropic 的模型配置并存入密钥，
# 或设置环境变量后用模型配置接口。详见 docs/modules.md
uv run uvicorn app.main:app --port 8000    # OpenAPI 文档: http://127.0.0.1:8000/docs
```

### CLI

```
platform init                       初始化数据库 + 预置 agent
platform run "需求" [--auto-approve] 运行一次需求，终端打印事件流
platform gc                         手动执行维护清理（过期工作目录，第五期）
```

## 第二期新增：执行引擎与工具库

- **SdkExecutor**（`app/runtime/sdk.py`）：基于 claude-agent-sdk 的 Claude Code 运行时。
  需要环境内有 `claude` CLI（PATH / `~/.claude/local` / `PLATFORM_CLAUDE_CLI_PATH`）；
  不可用时尝试以明确错误崩溃（演示模式自动降级 lite，见 D-2.5）。
  - 模型经 `ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL` 环境变量指向 agent 的模型配置（任意 Anthropic 兼容端点）；
  - 工作目录内文件编辑免审（acceptEdits），其余工具全部经平台权限链与审批。
- **工具库**（`app/tools/`）：MCP（stdio / streamable HTTP）、Skills、Commands、Hooks、
  custom_http / custom_python 工具；注册 → 测试 → 发现 → 绑定（`/tools`、
  `/tools/{id}/test|discover`、`/agents/{id}/tool-bindings`）。
- **权限与审批**（§8.9、§11）：allow / ask / deny 三级判定链 + 高危命令表；
  ask 触发审批单（任务转 awaiting_approval），`POST /approvals/{id}/decide`
  支持 remember = once / this_run / always_for_agent；超时自动 expired。
- **密钥运行时**（§8.8、§11）：工具配置内 `{"$secret": "名称"}` 引用物化时解密、
  仅经内存下发；事件流与审批参数一律脱敏；任何接口不回明文。

### 第三期新增：完整界面

**界面化运行（推荐演示路径）**：

```bash
# 终端 1：模拟模型服务（同上）
cd backend
uv run python -m tests.mockmodel.server_main --port 8765 --script tests/scripts/multi_domain_research.yaml

# 终端 2：后端（首次启动自动建库 + 种子；MOCK_MODEL_URL 指向模拟模型）
cd backend
MOCK_MODEL_URL=http://127.0.0.1:8765 uv run uvicorn app.main:app --port 8000

# 终端 3：前端开发服务（http://localhost:5173，/api 自动代理到 8000）
cd frontend
npm install && npm run dev
```

打开 http://localhost:5173 ：左侧会话轨输入需求 → 协调者提出计划（计划卡上确认）→
任务板观察派发/复核 → 右侧「事件遥测」栏实时滚动全部事件（可按类别过滤、展开载荷、
切回历史运行自动补发）→ 终局答复卡含 outcome 与缺口清单。

- **运行工作台**：暂停/继续/中止/追加预算；任务抽屉内取消/跳过/强制采纳/重派/改派/
  追加尝试次数/向运行中 agent 注入指令；计划版本、决策日志、交付物（可下载/上传）分页签。
- **审批中心**：ask 权限调用待办清单，批准/拒绝 + 记忆范围（仅本次/本运行内/始终允许该 agent）。
- **注册表**：Agent（版本快照、回滚、工具绑定与权限覆盖）、团队、工具库
  （注册 → 连接测试 → MCP 子工具发现）、模型配置（四项体检 + 时延）、密钥库。
- **设置**：计划审批模式/阈值、并行上限、审批超时、高危命令表、访问密码（保存后所有
  API 与 WS 需要密码，登录页生效）、custom_python 开关等，保存即生效。

**生产构建**：`npm run build` 后由后端直接服务 `frontend/dist`（单容器），
或 `docker compose up`（frontend: nginx 8080 → backend 8000 → PostgreSQL 16）。

### 前端质量门

```bash
cd frontend
npm run build     # tsc -b（strict）+ vite build
npm test          # vitest：WS 协议/REST 客户端/时间线组装/审批交互
npx eslint src
```

### 第四期新增：Agent 工厂与团队

- **Agent 工厂**（`app/factory/`，`POST /factory/agents|teams`）：描述需求 → 模型起草
  完整 agent 定义（提示词/职责/标签/预算/契约）或团队编队（从现有 agent 花名册选成员 +
  剧本阶段骨架 + 能力缺口清单）。草稿不落库，编辑器审阅后保存（agent 以 origin=factory
  留痕）；每次生成发 `factory.generated` 事件（全局通道，审计可查）。
- **会话编队**：工作台输入区可为会话绑定默认团队；绑定后协调者每次唤醒都能看到
  「编队」段（成员清单 + 组长提示 + 剧本阶段），优先从成员中选人、以剧本为骨架
  （任务带 playbook_stage_key），偏离记入 plan.proposed 的 deviates_from_playbook。
- **剧本（playbook）结构化**：阶段 = key/目标/适合/依赖；前端行编辑器 + JSON 切换，
  后端校验（key 唯一、依赖存在、无环）；编排器 `search_agents(team=)`、
  `list_playbooks(team=)` 按团队过滤。

## 真实 Claude Code 运行时冒烟（可选）

```bash
npm install -g @anthropic-ai/claude-code   # 或已有 claude CLI
export PLATFORM_CLAUDE_CLI_PATH=$(which claude)
# agent 的模型配置指向兼容端点后，executor=sdk 的 agent 即走 Claude Code 运行时
```

## 测试与质量

测试分两层：**mock 层**（快速回归，免费、确定）与 **真实模型层**（`real_model` 标记，
走真实端点，防「mock 测不出的兼容性缺陷」——$defs 引用、消息形状、thinking 回传、
状态机死局等均由该层捕获）。

```bash
cd backend
uv run pytest                     # 快速回归层（状态机/权限/调度等纯逻辑）
DEEPSEEK_API_KEY=sk-... uv run pytest -m real_model   # 真实模型层（约 4 分钟，<$0.05）
DEEPSEEK_API_KEY=sk-... uv run python -m tests.e2e.test_phase3_ui_smoke   # 界面链路冒烟（真实进程 + 真实模型）
DEEPSEEK_API_KEY=sk-... uv run python -m tests.e2e.test_phase4_ui_smoke   # 第四期链路冒烟（工厂/编队/剧本，真实模型）
uv run ruff check app tests       # 代码风格
uv run mypy app                   # strict 类型检查
uv run alembic upgrade head       # 数据库迁移
```

验证原则：**所有业务链路（协议往返、编排闭环、执行、工厂、UI API 面）一律由真实端点驱动**；
快速回归层仅覆盖无模型交互的纯逻辑（状态机、权限链、预算、熔断计数），不作为链路正确性的依据。
真实层覆盖矩阵：协议形状（双协议 × 混合轮/多工具/长循环/thinking 回传）、完整编排闭环
（计划→派发→真实 worker 执行→复核→终答）、驳回修订循环、多轮澄清推进、工厂起草、
UI 全 API 面（静态服务/计划批准/事件补发/WS/认证/编队/剧本阶段映射）。
可用 `DEEPSEEK_MODEL` 换模型、`DEEPSEEK_BASE_URL`/`DEEPSEEK_OPENAI_BASE_URL` 换端点；
session 结束打印用量与成本估算（`[real-model cost]`）。CI 建议：PR 跑快速层，nightly 带 key 跑全部真实层。

第一期验收清单（§14.2）全部有对应测试：状态机逐条转换、非法转换留痕、
越界派发拒绝、rationale 强制、并行上限、依赖解锁、协调规则逐行剧本测试、
最大尝试次数、重启恢复、事件流重建一致性。

第二期验收清单（§14.3）：SdkExecutor 装配与运行时缺失降级、MCP 双传输
注册/测试/发现/绑定/执行、权限链全路径（默认/绑定/停用/子工具/高危/记忆）、
审批 approve / reject / remember 三档 / 超时 expired、密钥解析与事件脱敏、
模型体检（四项 + 时延）、mockmcp 对照真实 MCP 客户端 SDK 验证、
工作目录物化。

第三期验收清单（§14.4）：完整界面覆盖 §12 全部 API 面——运行工作台（会话/计划卡/
提问卡/任务板/决策/交付物/事件遥测/预算与控制）、审批中心（remember 三档）、
Agent 注册表（版本/回滚/工具绑定）、工具库（注册/测试/发现）、模型配置（体检）、
密钥库、设置（含访问密码启用强制 §11.4）、WS 订阅/补发/心跳/重连（§12.3）、
登录门与密钥失效回退；`tests/e2e/test_phase3_ui_smoke.py` 以真实进程走通
静态服务→种子→会话→计划批准→终态→事件补发→WS→密码强制全链路（16 项）。

第四期验收清单（§14.5）：Agent 工厂（起草→人审→origin=factory 落库，
`factory.generated` 留痕）、团队完整化（会话编队、剧本阶段校验、
`deviates_from_playbook` 偏离记录、任务 `playbook_stage_key` 落库、
编排器 team 过滤工具）、结构化剧本编辑器与工厂编队界面；
单元 + E2E（编队运行含阶段引用试错）+ `test_phase4_ui_smoke.py` 真实进程 15 项 +
前端剧本编辑器 vitest。

第五期验收清单（§14.6）：SdkExecutor 会话恢复（自动重试凭
`attempt.executor_session_id` + `options.resume` 续跑，EvSessionBound 落库）、
`openai_chat` 双协议（`/chat/completions` 兼容实现 + 模拟模型双端点 + 模型页协议
选择器 + 全链路 E2E）、引擎熔断（窗口崩溃计数 → `engine.circuit_open` 开闸 →
冷却 `engine.recovered` 合闸，`/metrics` gauge）、日成本限额（`daily_cost_limit`
超限停启 + 当日一次 `budget.exceeded(daily)`）、工作目录清理（
`workspace_retention_days` 到期回收 + 活跃尝试保护 + `platform gc`）；单元 25 项 +
第五期 E2E 2 项 + 三/四期冒烟回归，无 schema 迁移。

## 部署

- Docker：`docker-compose.yml`（frontend: nginx + backend + PostgreSQL 16）
- 单容器：`frontend/` 内 `npm run build` 后，后端自动服务 `frontend/dist`
- 本地无 Docker：默认 SQLite（`data/platform.db`），见上

## 分期状态

- [x] 第 0 期 测试基础设施（模拟模型服务 / 模拟 MCP / 冒烟测试框架）
- [x] 第一期 协调引擎骨架
- [x] 第二期 执行引擎与工具库（SdkExecutor、MCP、Skills、审批链路）
- [x] 第三期 完整界面（运行工作台 / 审批中心 / 注册表 / 设置 / 访问密码）
- [x] 第四期 Agent 工厂与团队（工厂起草 / 会话编队 / 剧本骨架）
- [x] 第五期 增强功能（会话恢复 / openai_chat 双协议 / 引擎熔断 / 日成本限额 / 工作目录清理）
