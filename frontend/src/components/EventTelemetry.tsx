/** 事件遥测栏（签名元素）：每事件一行 = seq 刻度 + 分类色标 + 类型 + 执行者。
 *  订阅 run:<id> 时带 after_seq 先补发历史再接实时；底部呼吸光标表示在播。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { PlatformEvent } from "../api/types";
import type { StreamEvent } from "../api/ws";
import { runApi } from "../api/endpoints";

export type EventCategory = "run" | "task" | "attempt" | "tool" | "approval" | "user" | "budget" | "engine";

const CATEGORY_STYLE: Record<EventCategory, { color: string; label: string }> = {
  run: { color: "var(--c-dusk)", label: "运行" },
  task: { color: "var(--c-signal)", label: "任务" },
  attempt: { color: "var(--c-signal)", label: "尝试" },
  tool: { color: "var(--c-moss)", label: "工具" },
  approval: { color: "var(--c-amber)", label: "审批" },
  user: { color: "var(--ink)", label: "用户" },
  budget: { color: "var(--c-amber)", label: "预算" },
  engine: { color: "var(--c-ember)", label: "引擎" },
};

export function eventCategory(type: string): EventCategory {
  if (type.startsWith("task.") || type.startsWith("plan.")) return "task";
  if (type.startsWith("attempt.")) return "attempt";
  if (type.startsWith("tool.")) return "tool";
  if (type.startsWith("approval.")) return "approval";
  if (type.startsWith("user.")) return "user";
  if (type.startsWith("budget.")) return "budget";
  if (type.startsWith("engine.")) return "engine";
  return "run";
}

const EVENT_LABELS: Record<string, string> = {
  "run.created": "运行创建",
  "run.status_changed": "状态变更",
  "run.completed": "运行完成",
  "run.failed": "运行失败",
  "run.cancelled": "运行中止",
  "run.status": "状态",
  "orchestrator.woke": "协调者唤醒",
  "orchestrator.thinking": "思考",
  "orchestrator.message": "协调者",
  "orchestrator.tool_called": "控制调用",
  "orchestrator.tool_rejected": "调用被拒",
  "task.created": "任务创建",
  "task.status_changed": "任务状态",
  "task.split": "任务拆分",
  "task.skipped": "任务跳过",
  "task.reassigned": "任务改派",
  "plan.proposed": "提出计划",
  "plan.approved": "计划通过",
  "plan.rejected": "计划否决",
  "plan.revised": "计划修订",
  "attempt.queued": "尝试排队",
  "attempt.started": "尝试开始",
  "attempt.reported": "尝试回报",
  "attempt.crashed": "尝试崩溃",
  "attempt.cancelled": "尝试取消",
  "attempt.output": "输出回合",
  "attempt.output.delta": "输出增量",
  "attempt.thinking": "子思考",
  "attempt.usage": "用量",
  "attempt.input_injected": "追加指令",
  "tool.call.started": "工具调用",
  "tool.call.finished": "工具完成",
  "tool.call.blocked": "调用拦截",
  "approval.requested": "审批请求",
  "approval.decided": "审批决定",
  "approval.expired": "审批过期",
  "user.question_asked": "向用户提问",
  "user.answered": "用户回答",
  "user.intervened": "用户介入",
  "budget.warning": "预算预警",
  "budget.exceeded": "预算超限",
  "budget.extended": "预算追加",
  "artifact.created": "交付物产出",
  "artifact.status_changed": "交付物流转",
  "review.submitted": "复核提交",
  "decision.recorded": "决策记录",
  "engine.invalid_transition": "非法转换",
  "engine.recovered": "引擎恢复",
  "engine.circuit_open": "引擎熔断",
  "config.settings_changed": "配置变更",
  "factory.generated": "工厂产出",
};

export function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type;
}

const DELTA_TYPES = new Set(["attempt.output.delta"]);

/** 事件行渲染。 */
function EventRow({ ev, onToggle, open }: {
  ev: PlatformEvent | StreamEvent;
  onToggle: () => void;
  open: boolean;
}) {
  const cat = eventCategory(ev.type);
  const style = CATEGORY_STYLE[cat];
  const payload = ev.payload ?? {};
  const hasPayload = Object.keys(payload).length > 0;
  const brief = typeof payload["text"] === "string" ? String(payload["text"]).slice(0, 120) : null;

  return (
    <div className="ev-row" data-open={open}>
      <button
        className={`ev-seq mono${DELTA_TYPES.has(ev.type) ? " ev-delta" : ""}`}
        onClick={onToggle}
        title={`seq ${ev.seq}`}
      >
        {String(ev.seq).padStart(3, "0")}
      </button>
      <div className="ev-main">
        <div className="ev-head" onClick={onToggle}>
          <span className="ev-tick" style={{ background: style.color }} />
          <span className="ev-type">{eventLabel(ev.type)}</span>
          {ev.task_key ? <span className="ev-task mono">{ev.task_key}</span> : null}
          {ev.actor ? <span className="ev-actor mono">{ev.actor}</span> : null}
          <span className="ev-ts mono">{new Date(ev.ts).toLocaleTimeString("zh-CN", { hour12: false })}</span>
        </div>
        {brief && !open ? <div className="ev-brief">{brief}</div> : null}
        {open && hasPayload ? (
          <pre className="ev-json mono">{JSON.stringify(payload, null, 2)}</pre>
        ) : null}
      </div>
    </div>
  );
}

export function EventTelemetry({
  runId,
  events,
  live,
}: {
  runId: string | null;
  events: (PlatformEvent | StreamEvent)[];
  live: boolean;
}) {
  const [openSeqs, setOpenSeqs] = useState<Set<number>>(new Set());
  const [filter, setFilter] = useState<EventCategory | "all">("all");
  const [paused, setPaused] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // 历史补发（切运行 / 打开面板时对齐持久化事件）
  const [history, setHistory] = useState<PlatformEvent[]>([]);
  useEffect(() => {
    setHistory([]);
    setOpenSeqs(new Set());
    pinnedRef.current = true;
    if (!runId) return;
    let alive = true;
    runApi
      .events(runId, 0)
      .then((r) => {
        if (alive) setHistory(r.items);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [runId]);

  // 合并历史与实时（seq 去重、升序）
  const merged = useMemo(() => {
    const seen = new Set<number>();
    const all = [...history, ...events];
    const unique = all.filter((e) => {
      if (seen.has(e.seq)) return false;
      seen.add(e.seq);
      return true;
    });
    return unique.sort((a, b) => a.seq - b.seq);
  }, [history, events]);

  const shown = useMemo(
    () => (filter === "all" ? merged : merged.filter((e) => eventCategory(e.type) === filter)),
    [merged, filter],
  );

  // 暂停时不滚动；恢复时贴底
  const onScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  useEffect(() => {
    if (paused) return;
    const el = bodyRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [shown.length, paused]);

  if (!runId) {
    return (
      <div className="telemetry panel">
        <div className="telemetry-head eyebrow">事件遥测</div>
        <div className="empty">选择会话后订阅运行事件流</div>
      </div>
    );
  }

  return (
    <div className="telemetry panel">
      <div className="telemetry-head">
        <span className="eyebrow">事件遥测</span>
        <span className={`telemetry-live${live ? " on" : ""}`}>{live ? "● 在播" : "○ 静止"}</span>
        <button className="btn-ghost btn-sm" onClick={() => setPaused((p) => !p)}>
          {paused ? "恢复滚动" : "暂停滚动"}
        </button>
      </div>
      <div className="telemetry-filters">
        {(["all", ...(Object.keys(CATEGORY_STYLE) as EventCategory[])] as const).map((c) => (
          <button
            key={c}
            className={`ev-filter${filter === c ? " on" : ""}`}
            style={c !== "all" ? { "--tick": CATEGORY_STYLE[c].color } as React.CSSProperties : undefined}
            onClick={() => setFilter(c)}
          >
            {c === "all" ? "全部" : CATEGORY_STYLE[c].label}
          </button>
        ))}
      </div>
      <div className="telemetry-body" ref={bodyRef} onScroll={onScroll}>
        {shown.length === 0 ? <div className="empty">暂无事件</div> : null}
        {shown.map((ev) => (
          <EventRow
            key={`${ev.seq}-${ev.id}`}
            ev={ev}
            open={openSeqs.has(ev.seq)}
            onToggle={() =>
              setOpenSeqs((prev) => {
                const next = new Set(prev);
                if (next.has(ev.seq)) next.delete(ev.seq);
                else next.add(ev.seq);
                return next;
              })
            }
          />
        ))}
        {live && !paused ? (
          <div className="telemetry-cursor" aria-hidden>
            <span />
          </div>
        ) : null}
      </div>
    </div>
  );
}
