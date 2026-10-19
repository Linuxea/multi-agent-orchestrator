# DECISIONS.md — 实现决策记录

按规格书 §1.1 要求，凡本文未写明而实现者自行决定的细节，以及前后矛盾之处的裁决，记录于此。

## 编号规则

D-<期数>.<序号>，按开发顺序递增。

## 第四期（Agent 工厂与团队）——设计与实现决策

- **D-4.1 第四期范围重建**：规格书不在手边，按第一期预留的契约线索重建 §14.5 范围——
  `AgentOrigin.factory` 枚举、`factory.generated` 事件载荷、`Session.default_team_id`、
  `Task.playbook_stage_key`、`PlanProposedPayload.deviates_from_playbook`、
  `search_agents/list_playbooks` 的 team 参数、TeamsPage「完整剧本编辑在第四期开放」注释。
  落为两块：① Agent 工厂（模型起草 agent/团队定义）② 团队完整化（会话编队 +
  剧本骨架 + 编排器团队感知 + 结构化剧本编辑器）。
- **D-4.2 工厂语义：草稿不落库**：`POST /factory/agents`、`POST /factory/teams` 只产出
  草稿（完整字段 + slug 可用性检查），用户在编辑器审阅修改后经既有创建接口保存；
  agent 保存时携带 origin=factory（API 只接受 manual/factory，preset 保留给种子）。
  理由：agent 定义含提示词/预算/权限面，属高风险配置，必须人审。每次成功生成发
  factory.generated（全局通道 run_id=None，kind=agent_draft/team_draft + 输入输出摘要）。
- **D-4.3 工厂模型链与解析**：起草用与主 agent 相同的模型解析链（设置 → 默认编排 →
  库中首个，可显式传 model_profile_id），为此把解析逻辑抽到 `app/runtime/model_config.py`
  共享（engine/scheduler/model_profiles.check 同步改引）。提示词要求仅输出 JSON；
  解析容忍 markdown 代码块与前后杂文；slug 规范化（非法字符 slugify）+ 冲突检测
  （slug_available=false 时附 slug_suggestion）。团队编队草稿只从现有 active agent
  花名册选成员，花名册外 slug 剔除并记录 dropped_member_slugs + missing_capabilities。
- **D-4.4 会话编队（default_team_id 生效）**：`POST/PATCH /sessions` 支持 default_team_id
  （clear_team 解绑；绑定校验团队存在）。绑定后每次唤醒的运行状态摘要注入「编队」段：
  团队名/描述/组长提示/成员清单（slug+名+描述+标签）/剧本阶段（key+名+目标+适合+依赖），
  并附编队要求（优先成员、以剧本为骨架、偏离须在 rationale 说明）。
- **D-4.5 剧本阶段 schema 规范化**：stage = {key, name, goal, agent_hint, depends_on}；
  key 必填唯一、depends_on 只能引用已知 key、禁自依赖与成环（registry.
  validate_playbook_stages，创建/更新/工厂共用）。propose_plan/revise_plan 新增任务可带
  playbook_stage_key：绑定剧本时校验引用存在（不存在 → orchestrator.tool_rejected），
  未绑定剧本时禁止填写；Task 落库与计划快照均带阶段；plan.proposed 载荷
  deviates_from_playbook = 剧本中无任务映射的阶段 key（对齐 §5.6「偏离之处说明理由」）。
- **D-4.6 编排器团队工具**：search_agents(team=slug) 改为返回该团队成员（按团队顺序，
  仅限注册表内 active 者）；list_playbooks(team=slug) 只返回该团队绑定剧本；team 参数
  同时接受 slug 或 id（find_team 回退）；团队不存在 → 控制工具拒绝。
- **D-4.7 前端落地**：AgentsPage「工厂起草」抽屉（需求+执行器+起草模型）→ 草稿预填
  编辑器（来源徽章 工厂/预置/手动；契约字段入表单）；TeamsPage 重写——结构化剧本阶段
  编辑器（key/名/目标/适合/依赖 行编辑 + 排序 + JSON 切换）、「工厂编队」抽屉（草稿含
  剔除成员与能力缺口提示）、删除团队/剧本；工作台输入区「编队」选择器（会话级，
  PATCH 即时生效）；事件遥测加 factory.generated 标签。
- **D-4.8 第四期验收面**：单元（工厂解析/成员过滤/slug 冲突/坏 JSON/阶段校验）+
  E2E（编队运行：错误阶段被拒→修正→阶段落库→偏离记录→快照映射；团队过滤工具；
  成员校验）+ 界面链路冒烟 `test_phase4_ui_smoke.py`（真实进程 dist：工厂起草→
  origin=factory 保存→编队→绑定→待批批准→阶段/偏离/快照断言，15 项）；前端 vitest
  增加剧本编辑器交互（保存编排/依赖拦截）。无 schema 变更（列均已在一期迁移中）。

第三期（完整界面）——设计与实现决策

- **D-3.1 前端栈与目录**：`frontend/` = React 19 + TypeScript(strict) + Vite + react-router 7；
  不引 UI 组件库，token 化手写 CSS（`src/styles/`）。UI 文案中文、代码英文 snake/camel 各随其主。
  质量门：`npm run build`（tsc -b + vite build）、eslint、vitest + testing-library（jsdom）。
- **D-3.2 访问密码落地（兑现 D-1.11 / §11.4）**：无状态密钥头（`Authorization: Bearer` 或
  `X-Platform-Key`），前端存 localStorage；`GET /api/v1/auth/status` 免认证探测是否启用，
  `POST /api/v1/auth/verify` 校验。设置读取带 5s 缓存（免每请求读库）、secrets.compare_digest
  常数时间比较；改密后最长 5s 生效。WS 经 `?key=`（浏览器 WS 无法自定义头部），密钥错误以
  关闭码 4401 断开、前端回登录页且不重连；/healthz、/readyz、/metrics、/docs 不拦截。
- **D-3.3 前端产物服务**：`frontend/dist` 存在时后端挂载 SPA 直出（`PLATFORM_FRONTEND_DIST`
  可覆盖路径）；开发态 Vite dev server 代理 /api（含 WS）到 127.0.0.1:8000；Docker 部署用
  nginx（frontend 服务，8080→80）反代 API 到 backend。
- **D-3.4 服务启动即种子**：API startup 调 `bootstrap.seed_initial_data()`（内置工具 + 预置
  agent + 演示模型配置 + 演示降级，全部幂等），界面化部署不再依赖先跑 `platform init`；
  种子逻辑自 cli.py 抽至 bootstrap，CLI 与 API 共用。
- **D-3.5 事件驱动刷新策略**：WS 事件按类型白名单触发 350ms 去抖的定向 refetch（运行详情/
  计划/决策/交付物/消息/运行列表）；attempt.output.delta 仅推送不触发刷新；事件按 run 通道
  seq 去重（补发与实时重叠时取大）；global 通道仅用于审批角标。
- **D-3.6 设计语言「夜航调度室」**：深蓝石墨分层底色（#0E141B→#26323F）+ 六语义色
  （信号青=运行/数据流、琥珀=待办/审批、苔绿=采纳/成功、岚紫=协调者、落日红=失败、
  灰烬=中性终态）；中文系统字体做 UI、等宽字体承担全部遥测（seq/ID/费用/时间戳/枚举）；
  签名元素 = 右侧「事件遥测」栏（seq 刻度 + 分类色标 + 载荷展开 + 在播呼吸光标）；
  状态枚举一律映射中文徽章。避免模板化三件套（奶油衬线/纯黑酸绿/报纸细线）。
- **D-3.7 新增端点**：`GET /runs/{run_id}/artifacts`（运行维度交付物清单）——原接口只有按 ID
  取单个，界面交付物页无法枚举；交付物下载经 blob（`<a href>` 无法携带密钥头）。
- **D-3.8 终局答复去重**：协调者终答同时存在于持久化 Message 与 run.final_answer；活跃运行的
  终答气泡被过滤，统一由 FinalAnswerCard（含 outcome 徽章 + 缺口清单）呈现。

## 第二期（SdkExecutor + 工具库）——设计与实现决策

- **D-2.1 claude-agent-sdk 版本线**：PyPI 当前版本线为 0.2.x（无 1.x），实测 0.2.168。适配层按真实 API 编写：自定义进程内工具经 `@sdk.tool` + `create_sdk_mcp_server`（options.tools 已不接受 Tool 列表）；会话用 `ClaudeSDKClient`（connect / receive_response / query / interrupt）。
- **D-2.2 mcp 客户端包差异**：本版本 `streamable_http_client` 不收 headers —— 头部经自有 httpx2 AsyncClient 注入并由连接负责关闭；`sse_client` 直接收 headers；两传输上下文均产出二元组。
- **D-2.3 mockmcp HTTP 缺陷修复**：`from __future__ import annotations` + Request 仅在 serve_http 函数内导入，导致路由参数注解无法在模块全局解析，FastAPI 把 request 误判为查询参数（422）；stdio 不受影响。该缺陷由"真实 MCP 客户端验证 mock"一步暴露（§14 验收要求的价值证明）。修复：拆出 `build_http_app`，FastAPI 导入上移模块级。
- **D-2.4 SdkExecutor 权限模式**：`permission_mode="acceptEdits"` + `can_use_tool` 全量接平台权限链：工作目录内文件编辑由 CLI 沙箱免审；Bash/WebFetch/MCP/自定义工具走 §8.9 链 + §11 审批。`setting_sources=["project"]`，不读宿主用户级 ~/.claude 配置，防止宿主配置泄漏进 agent 会话。
- **D-2.5 演示降级条件化**（修订 D-1.15）：CLI 演示模式不再无条件降级；`sdk_runtime_available()`（SDK 可导入 + claude CLI 在 PATH / ~/.claude/local / PLATFORM_CLAUDE_CLI_PATH）不可用才降级为 lite。SdkExecutor 恒注册（缺失运行时在尝试级以明确错误崩溃并走 §5.7 自动重试/阻塞路径）。
- **D-2.6 权限链判定顺序**（§8.9 细化）：平台工具（submit_report）与 CLI 只读工具放行 → 未注册/已停用 deny → 非 builtin 工具必须绑定 → enabled_subtools 范围 → 运行级记忆命中放行 → 绑定权限覆盖工具默认 → bash 类调用命中高危命令表强制 deny（优先于 ask）。
- **D-2.7 内置工具注册行**：CLI 内建能力（bash/write/edit/web_fetch/web_search）建 kind=builtin 的 Tool 行，binding 可选；无绑定时用 default_permission（bash/web_* 默认 ask，write/edit 默认 allow——写文件被限制在工作目录内，见 D-2.4）。
- **D-2.8 MCP 子工具 schema 来源**：lite 执行器的工具定义来自 Tool.discovered（经 /test 或 /discover 刷新）；物化时不做在线发现（避免运行时网络依赖与不稳定）；未发现则该工具不暴露。sdk 执行器经 CLI 自行发现，enabled_subtools 由权限链把守。
- **D-2.9 密钥下发面**：MCP 配置物化时解密，仅经进程内存传给执行器（sdk：options.mcp_servers；lite：Toolbox 连接），不写入工作目录任何文件；skills/commands/hooks 落盘 .claude/（hooks 命令中的密钥经 CLI env 注入而非写入 settings.json）。
- **D-2.10 脱敏边界**：tool.call.* 事件参数与结果摘要、审批单入参数据经 SecretMasker（≤4 字符跳过）；lite 执行器返回给模型的工具结果同样脱敏；sdk 执行器内 CLI 自行产生的工具结果不脱敏（密钥本就供其调用外部服务使用）。
- **D-2.11 审批等待语义**：ask → Approval(pending) + attempt/task → awaiting_approval，执行协程挂起于 future（占用并发上限但可被 quiesce 观测为"未静止"）；用户决定经 ApprovalGate.resolve 唤醒；超时（approval_timeout_minutes）→ expired + 按拒绝恢复。remember 三档：once 不记忆；this_run 记 (run, agent, tool) 运行内放行；always_for_agent 把绑定权限持久化为 allow。
- **D-2.12 Secret.last_used_at 列类型修正**：第一期模型误声明 String，第二期权限链开始真正写入；迁移 a2f7c31d9e04 改为 DateTime（SQLite batch_alter）。
- **D-2.13 TaskReport 校验增强**：blocked/failed 回报必须附 blocker（pydantic model_validator，与工具描述及规格契约一致）。
- **D-2.14 工具调用事件序**：被拒调用只发 tool.call.blocked（不发 started/finished）；放行调用 started → 执行 → finished。lite 由执行器发；sdk 由 AssistantMessage(tool_use) → UserMessage(tool_result) 翻译，can_use_tool 仅裁权限不发事件。
- **D-2.15 SdkExecutor 兜底回报**：ResultMessage 无捕获回报时，预算耗尽走 budget_exhausted 自动回报，否则走 no_report 兜底（均 §5.4 与 lite 一致）；usage 逐轮累计（AssistantMessage）+ 终值取 max（ResultMessage 为累计总量）。
- **D-2.16 模型体检扩展（§9.6）**：四项检查每项附 latency_ms，响应增加汇总 ok / base_url / model。
- **D-2.17 custom_python 工具**：默认停用（allow_custom_python_tools=False）；启用后以子进程 `python -c` 执行（cwd=工作目录、stdin 传参、60s 超时），返回值同样脱敏。
- **D-2.18 审批/权限链测试策略**：SdkExecutor 单元测试用伪 SDK 模块（tests/fakesdk.py，monkeypatch _load_sdk）覆盖选项装配/事件翻译/权限回调/兜底；真实 Claude Code 运行时不在本环境，真机链路留待有 claude CLI 的环境冒烟（同 real_model 跳过策略可扩展）。

## 第 0–1 期（测试基础设施 + 协调引擎骨架）——设计与实现决策

### 实现过程中的补充决策（D-1.15 起）

- **D-1.15 演示模式降级执行器**：CLI 在 `MOCK_MODEL_URL` 演示模式下把预置 sdk 执行器临时改为 lite（SdkExecutor 属第二期 §14.3），只影响本地演示库。
- **D-1.16 review/read_artifact 支持 task_key 定位**：规格的工具签名只收 attempt_id / artifact_id（运行时生成的 ULID），静态剧本与真实模型都无法预知。两个工具增加可选 task_key 参数（review 取该任务最近一次待复核尝试；read_artifact 取该任务最近交付物），与规格语义一致且不破坏原签名。
- **D-1.17 唤醒语义化跳过**：`_should_orchestrate` 对无实际可推进事项的唤醒（如任务已派发后的残留 tasks_ready、运行等待用户）直接跳过，不调用模型；"自动重试待执行"的 blocked 任务也视为不可推进，防止陈旧唤醒与自动重试的竞态。语义唤醒（用户消息/回答/预算/恢复）始终处理。
- **D-1.18 调度串行化**：`schedule()` 用进程内互斥锁 + 已启动二次检查，消除并发 kick 导致的同一尝试重复启动（check-then-act 竞态）。
- **D-1.19 尝试结束不再发通用唤醒**：各结果处理器（回报/崩溃/取消）按需精准唤醒主 agent；自动重试路径刻意不唤醒，由重试结束后的处理再决定。
- **D-1.20 DecisionType.split 成员名**：`split` 与 `str.split` 冲突（mypy strict），枚举成员改为 `split_task`，值仍为 `"split"`。
- **D-1.21 outcome 判定**：没有任何任务被采纳时 outcome=failed；有采纳但有失败/跳过/缺口为 partial；全部采纳且无缺口为 success。E2E-6 的"按现有结果汇总"场景通常落在 partial。
- **D-1.22 anthropic SDK 兼容**：安装版本（1.15.0）底层使用 httpx2 且构造参数不接受 httpx.Timeout 对象；ModelCaller 以 float 秒传 timeout，保持版本无关。SDK 客户端构造需要 api_key 非空（mock 场景填占位值）。
- **D-1.23 blocked 任务的复核**：blocked 任务只接受 fail 结论（report=failed 且 orchestrator 认定不可完成 → 直接 failed）；改派/重派走 dispatch，避免 blocked→accepted 的非法转换。
- **D-1.24 环境代理**：开发环境存在 SOCKS 全局代理（all_proxy），httpx 客户端构造需要 socksio；本地回环已通过 NO_PROXY 处理。socksio 列入依赖。
- **D-1.25 事件序号缓存**：EventService 按运行缓存下一个 seq（进程内互斥锁），服务重启时从数据库回填；单实例假设与 D-1.7 一致。

### 设计期决策（D-0.1 至 D-1.14）
- **D-0.1 目录**：后端落在 `backend/`，前端（第三期开始）落在 `frontend/`，运行时数据落在 `data/`（git 忽略）。与 §3.3 一致。
- **D-0.2 ULID**：自实现 `app/ids.py`（48bit 毫秒时间戳 + 80bit 随机，同一毫秒内单调递增），避免额外依赖。事件排序另依赖运行内 `seq`，不依赖 ULID。
- **D-0.3 模拟模型服务**：实现为独立 FastAPI 应用 `backend/tests/mockmodel/`，支持非流式与 SSE 流式、工具调用、usage 字段，剧本按"作用域匹配（system/最后一条消息包含的关键字）+ 顺序步骤"返回预设响应。测试通过 `ModelProfile(base_url=http://127.0.0.1:<port>)` 指向它，完全走真实 HTTP，不 mock SDK 客户端。
- **D-0.4 流式策略**：第一期 LiteExecutor 使用流式输出（产生 `attempt.output.delta` 仅推送事件 + 每轮合并的 `attempt.output` 持久化事件）；主 agent 控制循环用非流式（控制工具调用无需打字机效果），`orchestrator.thinking`/`orchestrator.message` 事件在每轮结束后发出。
- **D-0.5 数据库初始化**：提供 Alembic（初始迁移覆盖全部第一期表）；应用启动与测试分别用 `alembic upgrade head` 与 `Base.metadata.create_all`（测试每用例全新库，用 create_all 提速）。两者 schema 由同一 metadata 生成，保证一致。
- **D-1.1 dispatch 与状态机表的融合**：§5.2 写 dispatch 校验"任务必须是 ready 或 needs_rework"，而 §5.5 转换表另有 `blocked → queued`（触发：改派或补充信息后重新派发，执行者：主 agent）。裁决：转换表是穷尽枚举且更具体，dispatch 允许 `ready / needs_rework / blocked` 三种来源状态；其余状态拒绝。
- **D-1.2 pending → blocked**：§5.5 正文规定"上游 failed/cancelled 时下游标记 blocked"，但转换表未列 `pending → blocked`。裁决：补充该转换（正文要求隐含它），并记入 `engine.invalid_transition` 之外的合法集合。
- **D-1.3 ask_user 的阻塞范围**：转换表只允许 `blocked / reported → awaiting_user`，故 `ask_user(blocking_task_keys)` 中处于其他状态的任务会被拒绝并提示；实践中需要补充信息的任务已处于 blocked/reported，语义自洽。
- **D-1.4 报告 status=failed 的落点**：§5.5 规定 running → blocked 的触发含"子 agent 提交 blocked 或 failed"，因此 failed 回报先将任务置为 blocked，再由主 agent 按规则决定改派/拆分/终止。
- **D-1.5 尝试计数**：`attempt.number` 每次执行递增（含自动重试）；`task.attempt_count` 只计"计入次数"的尝试（正常派发与返工），崩溃自动重试、external_error 自动重试不计入。`max_attempts` 与后者比较。
- **D-1.6 主 agent 对话线程**：第一期在内存中维护每次运行的对话线程（重启后以运行状态摘要重建），超过上下文 60%（按字符数估算 token）压缩旧轮次为摘要。线程不持久化，事件流已足够重建审计视图。
- **D-1.7 事件 seq 分配**：单进程内按 run 维护 asyncio 互斥锁，`max(seq)+1` 分配；事件写入与状态更新在同一 DB 事务。多实例部署（超出第一版范围）需改为数据库序列。
- **D-1.8 计划预估费用**：brief 未产生前无法精确预估，采用"每任务取其 agent default_budget.max_cost（无则全局默认单任务费用）求和"作为 `auto_if_under` 阈值判断与计划卡片展示的估计值，界面上标注"预估"。
- **D-1.9 高危/审批/工具权限**：第一期 LiteExecutor 仅有 `submit_report` 一个工具，不触发审批；Approval/Tool/Secret 数据模型与 REST 骨架已就位，完整判定链路在第二期实现。
- **D-1.10 预置 agent**：`preset` 种子数据随首启创建（§6.6 全部 11 个），提示词为初版，可在注册表编辑。
- **D-1.11 认证**：第一期仅监听 127.0.0.1，无鉴权（§11.4 允许）；访问密码在设置页落地（第三期界面）后启用强制。
- **D-1.12 文本相似度**：死循环防护（§11.3）用 `difflib.SequenceMatcher.ratio()` 近似，阈值 0.9。
- **D-1.13 预算校验**：`Budget` 金额按 float 计算与累计（单价 Numeric(12,6) 存储，读取转 float）；统计用途下精度足够。
- **D-1.14 回合上限**：主 agent 单次唤醒最多 15 次控制工具调用（§5.11）；LiteExecutor 轮次上限取 brief.budget.max_turns 或 agent 默认预算，缺省 24。
