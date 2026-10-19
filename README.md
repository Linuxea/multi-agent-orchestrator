# 多 Agent 协调平台

规格书驱动的开发仓库。需求唯一依据：`docs/spec.md`（第 14 节分期）。
实现过程中的自行决策与规格歧义裁决见 `DECISIONS.md`。

## 结构

```
backend/   Python 3.12 + FastAPI + SQLAlchemy(async) + anthropic SDK
frontend/  第三期开始（React + TS + Vite）
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

### 真实 Claude Code 运行时冒烟（可选）

```bash
npm install -g @anthropic-ai/claude-code   # 或已有 claude CLI
export PLATFORM_CLAUDE_CLI_PATH=$(which claude)
# agent 的模型配置指向兼容端点后，executor=sdk 的 agent 即走 Claude Code 运行时
```

## 测试与质量

```bash
cd backend
uv run pytest                     # 全部自动化测试（模拟模型 + 模拟 MCP，无需密钥）
uv run ruff check app tests       # 代码风格
uv run mypy app                   # strict 类型检查
DEEPSEEK_API_KEY=sk-... uv run pytest -m real_model   # 真实模型冒烟（可选）
uv run alembic upgrade head       # 数据库迁移
```

第一期验收清单（§14.2）全部有对应测试：状态机逐条转换、非法转换留痕、
越界派发拒绝、rationale 强制、并行上限、依赖解锁、协调规则逐行剧本测试、
最大尝试次数、重启恢复、事件流重建一致性。

第二期验收清单（§14.3）：SdkExecutor 装配与运行时缺失降级、MCP 双传输
注册/测试/发现/绑定/执行、权限链全路径（默认/绑定/停用/子工具/高危/记忆）、
审批 approve / reject / remember 三档 / 超时 expired、密钥解析与事件脱敏、
模型体检（四项 + 时延）、mockmcp 对照真实 MCP 客户端 SDK 验证、
工作目录物化。

## 部署

- Docker：`docker-compose.yml`（backend + PostgreSQL 16；前端第三期接入）
- 本地无 Docker：默认 SQLite（`data/platform.db`），见上

## 分期状态

- [x] 第 0 期 测试基础设施（模拟模型服务 / 模拟 MCP / 冒烟测试框架）
- [x] 第一期 协调引擎骨架
- [x] 第二期 执行引擎与工具库（SdkExecutor、MCP、Skills、审批链路）
- [ ] 第三期 完整界面
- [ ] 第四期 Agent 工厂与团队
- [ ] 第五期 增强功能
