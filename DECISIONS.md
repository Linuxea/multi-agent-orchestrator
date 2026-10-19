# DECISIONS.md — 实现决策记录

按规格书 §1.1 要求，凡本文未写明而实现者自行决定的细节，以及前后矛盾之处的裁决，记录于此。

## 编号规则

D-<期数>.<序号>，按开发顺序递增。

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
