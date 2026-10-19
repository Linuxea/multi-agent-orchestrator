/** 运行舞台的流式渲染：用户/协调者消息 + 计划卡 + 提问卡 + 终局卡 + 里程碑。 */

import { useState } from "react";
import { runApi } from "../api/endpoints";
import type { MessageInfo, PlanVersion, RunDetail, StreamEventLike } from "./streamTypes";
import { OutcomeChip, StatusChip } from "./StatusChip";

export interface StreamItem {
  key: string;
  ts: string;
  node: React.ReactNode;
}

interface PlanEventPayload {
  version?: number;
  estimated_cost?: number;
  rationale?: string;
  task_count?: number;
}

interface QuestionEventPayload {
  question_id?: string;
  question?: string;
  options?: string[];
  blocking_task_keys?: string[];
}

/** 组装时间线：会话消息 + 活跃运行的关键事件卡片。 */
export function buildTimeline(
  messages: MessageInfo[],
  events: StreamEventLike[],
  runs: { id: string; status: string; outcome: string | null }[],
  activeRunId: string | null,
  plans: PlanVersion[],
): StreamItem[] {
  const items: StreamItem[] = [];
  const seenRunMarkers = new Set<string>();

  const runMarker = (runId: string | null) => {
    if (!runId || seenRunMarkers.has(runId)) return;
    seenRunMarkers.add(runId);
    const r = runs.find((x) => x.id === runId);
    if (!r) return;
    items.push({
      key: `runmark-${runId}`,
      ts: "",
      node: (
        <div className="st-runmark">
          <span className="st-runmark-line" aria-hidden />
          <span className="mono st-runmark-label">
            运行 {runId.slice(-6)}
          </span>
          <StatusChip kind="run" status={r.status} live={!isTerminalRun(r.status)} />
          {r.outcome ? <OutcomeChip outcome={r.outcome} /> : null}
          <span className="st-runmark-line" aria-hidden />
        </div>
      ),
    });
  };

  for (const m of messages) {
    runMarker(m.run_id);
    items.push({
      key: `msg-${m.id}`,
      ts: m.created_at,
      node:
        m.role === "user" ? (
          <div className="st-user">
            <div className="st-who">你</div>
            <div className="st-bubble user">{m.content}</div>
          </div>
        ) : (
          <div className="st-orch">
            <div className="st-who">协调者</div>
            <div className="st-bubble orch">{m.content}</div>
          </div>
        ),
    });
  }

  // 活跃运行的事件卡片（按 ts 与消息交错）
  if (activeRunId) {
    const answered = new Set(
      events
        .filter((e) => e.type === "user.answered")
        .map((e) => String((e.payload as Record<string, unknown>)["question_id"] ?? "")),
    );

    for (const ev of events) {
      const p = (ev.payload ?? {}) as Record<string, unknown>;
      if (ev.type === "orchestrator.message") {
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: (
            <div className="st-orch">
              <div className="st-who">协调者</div>
              <div className="st-bubble orch">{String(p["text"] ?? "")}</div>
            </div>
          ),
        });
      } else if (ev.type === "orchestrator.thinking") {
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: (
            <details className="st-thinking">
              <summary>思考过程</summary>
              <div className="st-thinking-body">{String(p["text"] ?? "")}</div>
            </details>
          ),
        });
      } else if (ev.type === "plan.proposed") {
        const pp = p as unknown as PlanEventPayload;
        const plan = plans.find((pl) => pl.version === pp.version);
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: (
            <PlanCard
              plan={plan ?? null}
              version={pp.version ?? 0}
              estimatedCost={pp.estimated_cost}
              rationale={pp.rationale ?? ""}
              taskCount={pp.task_count ?? plan?.tasks_snapshot.length ?? 0}
            />
          ),
        });
      } else if (ev.type === "user.question_asked") {
        const qp = p as unknown as QuestionEventPayload;
        if (answered.has(qp.question_id ?? "")) continue;
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: (
            <QuestionCard
              runId={activeRunId}
              questionId={qp.question_id ?? null}
              question={qp.question ?? ""}
              options={qp.options ?? []}
            />
          ),
        });
      } else if (ev.type === "run.completed") {
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: <FinalMark outcome={String(p["outcome"] ?? "")} />,
        });
      } else if (ev.type === "run.failed") {
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: <div className="st-marker err">运行失败：{String(p["error"] ?? "")}</div>,
        });
      } else if (ev.type === "budget.warning" || ev.type === "budget.exceeded") {
        items.push({
          key: `ev-${ev.seq}`,
          ts: ev.ts,
          node: (
            <div className={`st-marker warn`}>
              {ev.type === "budget.exceeded" ? "预算超限" : "预算预警"}
              {p["message"] ? `：${String(p["message"])}` : ""}
            </div>
          ),
        });
      }
    }
  }

  items.sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0));
  return items;
}

function isTerminalRun(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function PlanCard({
  plan,
  version,
  estimatedCost,
  rationale,
  taskCount,
}: {
  plan: PlanVersion | null;
  version: number;
  estimatedCost?: number;
  rationale: string;
  taskCount: number;
}) {
  return (
    <div className="st-card plan">
      <div className="st-card-head">
        <span className="eyebrow">计划</span>
        <span className="mono">v{version}</span>
        <span className="plan-est">
          {taskCount} 个任务
          {estimatedCost !== undefined ? ` · 预估 ¥${estimatedCost.toFixed(2)}` : ""}
        </span>
        {plan ? (
          plan.approved ? (
            <span className="chip chip-moss">已通过</span>
          ) : (
            <span className="chip chip-ash">未生效</span>
          )
        ) : null}
      </div>
      {rationale ? <p className="st-card-why">{rationale}</p> : null}
      {plan ? (
        <table className="data st-plan-tasks">
          <thead>
            <tr>
              <th>任务</th>
              <th>负责</th>
              <th>依赖</th>
            </tr>
          </thead>
          <tbody>
            {plan.tasks_snapshot.map((t) => (
              <tr key={t.key}>
                <td>
                  <span className="mono st-taskkey">{t.key}</span> {t.title}
                </td>
                <td className="mono">{t.agent_slug}</td>
                <td className="mono">{t.depends_on.join(", ") || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function QuestionCard({
  runId,
  questionId,
  question,
  options,
}: {
  runId: string;
  questionId: string | null;
  question: string;
  options: string[];
}) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (answer: string) => {
    if (!answer.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      await runApi.answer(runId, answer.trim(), questionId);
      setSent(true);
    } catch (e) {
      // 此前无 catch：提交失败静默（按钮恢复可点、无任何提示），用户以为点了没反应
      setError(e instanceof Error ? e.message : "提交失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  if (sent) {
    return (
      <div className="st-card question answered">
        <div className="st-card-head">
          <span className="eyebrow">提问</span>
          <span className="st-q">{question}</span>
        </div>
        <p className="st-answered">已回答，等待协调者继续。</p>
      </div>
    );
  }

  return (
    <div className="st-card question">
      <div className="st-card-head">
        <span className="eyebrow">协调者提问</span>
      </div>
      <p className="st-q">{question}</p>
      {error ? <p className="st-q-error" role="alert">{error}</p> : null}
      {options.length > 0 ? (
        <div className="st-options">
          {options.map((o) => (
            <button key={o} className="btn btn-sm" disabled={busy} onClick={() => submit(o)}>
              {o}
            </button>
          ))}
        </div>
      ) : null}
      <div className="st-answer-row">
        <input
          type="text"
          value={text}
          placeholder="输入回答…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit(text)}
        />
        <button className="btn-primary btn-sm" disabled={busy || !text.trim()} onClick={() => submit(text)}>
          回答
        </button>
      </div>
    </div>
  );
}

function FinalMark({ outcome }: { outcome: string }) {
  return (
    <div className={`st-final ${outcome}`}>
      <div className="st-final-head">
        <OutcomeChip outcome={outcome} />
        <span className="st-final-label">
          {outcome === "success" ? "目标达成" : outcome === "partial" ? "部分达成" : "未能达成"}
        </span>
      </div>
    </div>
  );
}

/** 终局答复卡（run.final_answer）。 */
export function FinalAnswerCard({ run }: { run: RunDetail }) {
  if (!run.final_answer && !run.outcome) return null;
  return (
    <div className={`st-card final ${run.outcome ?? ""}`}>
      <div className="st-card-head">
        <span className="eyebrow">最终答复</span>
        {run.outcome ? <OutcomeChip outcome={run.outcome} /> : null}
      </div>
      {run.final_answer ? <div className="st-final-text">{run.final_answer}</div> : null}
      {run.summary_of_gaps ? (
        <div className="st-gaps">
          <div className="eyebrow">缺口</div>
          <pre>{run.summary_of_gaps}</pre>
        </div>
      ) : null}
    </div>
  );
}
