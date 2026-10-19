/** 运行工作台：会话轨 + 运行舞台（流/任务板/计划/决策/交付物）+ 事件遥测。 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { artifactApi, runApi, sessionApi } from "../api/endpoints";
import type { ArtifactRow, DecisionRow, MessageInfo, RunDetail, RunListItem, SessionInfo } from "../api/types";
import type { StreamEvent } from "../api/ws";
import { EventTelemetry } from "../components/EventTelemetry";
import { buildTimeline, FinalAnswerCard } from "../components/RunStream";
import { OutcomeChip, StatusChip } from "../components/StatusChip";
import { TaskBoard } from "../components/TaskBoard";
import { UsageBar } from "../components/UsageBar";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";
import { downloadFile } from "../api/client";

const RELOAD_TYPES = new Set([
  "task.status_changed",
  "task.created",
  "task.split",
  "attempt.queued",
  "attempt.started",
  "attempt.reported",
  "attempt.crashed",
  "attempt.cancelled",
  "plan.approved",
  "plan.rejected",
  "run.status_changed",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "orchestrator.message",
  "review.submitted",
  "artifact.created",
  "artifact.status_changed",
  "budget.warning",
  "budget.exceeded",
  "budget.extended",
  "user.answered",
  "user.intervened",
]);

type Tab = "stream" | "tasks" | "plans" | "decisions" | "artifacts";

const TABS: [Tab, string][] = [
  ["stream", "动态"],
  ["tasks", "任务板"],
  ["plans", "计划"],
  ["decisions", "决策"],
  ["artifacts", "交付物"],
];

export function WorkspacePage() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const { subscribe, unsubscribe, onEvent, apiError, toast, wsStatus } = useApp();

  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const effectiveId = sessionId ?? sessions[0]?.id ?? null;

  const loadSessions = useCallback(() => {
    sessionApi
      .list()
      .then(setSessions)
      .catch(apiError);
  }, [apiError]);
  useEffect(loadSessions, [loadSessions]);

  const msgs = useAsync<MessageInfo[]>(
    () => (effectiveId ? sessionApi.messages(effectiveId) : Promise.resolve([])),
    [effectiveId],
  );
  const runsAsync = useAsync<RunListItem[]>(
    () => (effectiveId ? sessionApi.runs(effectiveId) : Promise.resolve([])),
    [effectiveId],
  );
  const runs = useMemo(() => runsAsync.data ?? [], [runsAsync.data]);
  const activeRun: RunListItem | null = runs[0] ?? null;
  const runId = activeRun?.id ?? null;

  const runAsync = useAsync<RunDetail | null>(
    () => (runId ? runApi.get(runId) : Promise.resolve(null)),
    [runId],
  );
  const run = runAsync.data;
  const plansAsync = useAsync(runId ? () => runApi.plans(runId) : () => Promise.resolve([]), [runId]);
  const decisionsAsync = useAsync(
    runId ? () => runApi.decisions(runId) : () => Promise.resolve([] as DecisionRow[]),
    [runId],
  );
  const artifactsAsync = useAsync(
    runId ? () => artifactApi.listByRun(runId) : () => Promise.resolve([] as ArtifactRow[]),
    [runId],
  );

  // ---------- WS 订阅与事件 ----------
  const [events, setEvents] = useState<StreamEvent[]>([]);
  useEffect(() => {
    if (!runId) return;
    setEvents([]);
    subscribe(`run:${runId}`);
    return () => unsubscribe(`run:${runId}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const reloadTimer = useRef<number | null>(null);
  useEffect(() => {
    const off = onEvent((ev, channel) => {
      if (runId && channel === `run:${runId}`) {
        setEvents((prev) => (prev.length > 3000 ? [...prev.slice(-2500), ev] : [...prev, ev]));
        if (RELOAD_TYPES.has(ev.type)) {
          if (reloadTimer.current !== null) window.clearTimeout(reloadTimer.current);
          reloadTimer.current = window.setTimeout(() => {
            runAsync.reload();
            plansAsync.reload();
            decisionsAsync.reload();
            artifactsAsync.reload();
            msgs.reload();
            runsAsync.reload();
          }, 350);
        }
      }
    });
    return () => {
      off();
      if (reloadTimer.current !== null) window.clearTimeout(reloadTimer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const live = activeRun !== null && !isTerminal(activeRun.status);

  // ---------- 动作 ----------
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);

  const send = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      let sid = effectiveId;
      if (!sid) {
        const s = await sessionApi.create(text.slice(0, 24));
        sid = s.id;
        setSessions((prev) => [s, ...prev]);
      }
      await sessionApi.send(sid, text);
      setDraft("");
      msgs.reload();
      runsAsync.reload();
      if (sid !== effectiveId) navigate(`/s/${sid}`);
      if (sid === effectiveId) loadSessions();
    } catch (e) {
      apiError(e);
    } finally {
      setSending(false);
    }
  };

  const runAction = async (label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      toast(`${label} 已下发`);
      runAsync.reload();
      runsAsync.reload();
    } catch (e) {
      apiError(e);
    }
  };

  // ---------- 流时间线 ----------
  const timeline = useMemo(() => {
    // 活跃运行的终答由 FinalAnswerCard 呈现，过滤持久化消息中的重复气泡
    const filtered = (msgs.data ?? []).filter(
      (m) => !(m.role === "orchestrator" && m.run_id === runId && run?.final_answer),
    );
    return buildTimeline(filtered, events, runs, runId, plansAsync.data ?? []);
  }, [msgs.data, events, runs, runId, plansAsync.data, run?.final_answer]);

  const [tab, setTab] = useState<Tab>("stream");
  const streamRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const onStreamScroll = () => {
    const el = streamRef.current;
    if (!el) return;
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  };
  useEffect(() => {
    if (tab !== "stream") return;
    const el = streamRef.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [timeline.length, run?.final_answer, tab]);

  return (
    <div className="workspace">
      {/* -------- 会话轨 -------- */}
      <aside className="sessions-rail panel">
        <div className="rail-head">
          <span className="eyebrow">会话</span>
          <button
            className="btn-ghost btn-sm"
            onClick={async () => {
              try {
                const s = await sessionApi.create("新会话");
                setSessions((prev) => [s, ...prev]);
                navigate(`/s/${s.id}`);
              } catch (e) {
                apiError(e);
              }
            }}
          >
            + 新会话
          </button>
        </div>
        <div className="rail-list">
          {sessions.length === 0 ? (
            <div className="empty">还没有会话，输入需求开始第一次运行</div>
          ) : null}
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`rail-item${s.id === effectiveId ? " on" : ""}`}
              onClick={() => navigate(`/s/${s.id}`)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && navigate(`/s/${s.id}`)}
            >
              <span className="rail-item-title">{s.title || "未命名会话"}</span>
              <span className="mono rail-item-id">{s.id.slice(-6)}</span>
              <button
                className="rail-item-del"
                title="删除会话"
                onClick={async (e) => {
                  e.stopPropagation();
                  try {
                    await sessionApi.remove(s.id);
                    setSessions((prev) => prev.filter((x) => x.id !== s.id));
                    if (s.id === effectiveId) navigate("/");
                  } catch (err) {
                    apiError(err);
                  }
                }}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </aside>

      {/* -------- 运行舞台 -------- */}
      <main className="run-stage">
        {run ? (
          <>
            <header className="stage-head panel">
              <div className="stage-status">
                <StatusChip kind="run" status={run.status} live={live} />
                {run.outcome ? <OutcomeChip outcome={run.outcome} /> : null}
                <span className="mono stage-runid">{run.id}</span>
              </div>
              <UsageBar usage={run.usage} budget={run.budget} />
              <div className="stage-actions">
                {run.status === "running" || run.status === "planning" || run.status === "synthesizing" ? (
                  <button className="btn-sm" onClick={() => runAction("暂停", () => runApi.pause(run.id))}>
                    暂停
                  </button>
                ) : null}
                {run.status === "paused" ? (
                  <button className="btn-sm" onClick={() => runAction("继续", () => runApi.resume(run.id))}>
                    继续
                  </button>
                ) : null}
                {live ? (
                  <>
                    <button
                      className="btn-sm"
                      onClick={() =>
                        runAction("追加预算", () => runApi.extendBudget(run.id, { max_cost: 1 }))
                      }
                      title="预算上限 +¥1"
                    >
                      +预算
                    </button>
                    <button
                      className="btn-danger btn-sm"
                      onClick={() => runAction("中止", () => runApi.cancel(run.id, "用户手动中止"))}
                    >
                      中止
                    </button>
                  </>
                ) : null}
              </div>
            </header>
            <FinalAnswerCard run={run} />
            <nav className="stage-tabs" role="tablist">
              {TABS.map(([t, label]) => (
                <button
                  key={t}
                  role="tab"
                  aria-selected={tab === t}
                  className={`stage-tab${tab === t ? " on" : ""}`}
                  onClick={() => setTab(t)}
                >
                  {label}
                  {t === "tasks" && run.tasks.length ? (
                    <span className="mono stage-tab-count">{run.tasks.length}</span>
                  ) : null}
                  {t === "artifacts" && (artifactsAsync.data?.length ?? 0) > 0 ? (
                    <span className="mono stage-tab-count">{artifactsAsync.data?.length}</span>
                  ) : null}
                </button>
              ))}
            </nav>
            <div className="stage-body" ref={streamRef} onScroll={onStreamScroll}>
              {tab === "stream" ? (
                timeline.length === 0 ? (
                  <div className="stage-hero">
                    <div className="brand-mark big" aria-hidden />
                    <h2>提出一次需求</h2>
                    <p>
                      协调者将拆解计划、派发子 agent 执行、复核产出并给出最终答复。
                      运行全程事件可在右侧遥测栏回放。
                    </p>
                  </div>
                ) : (
                  <>
                    {timeline.map((item) => (
                      <div key={item.key} className="st-item">
                        {item.node}
                      </div>
                    ))}
                  </>
                )
              ) : null}
              {tab === "tasks" ? (
                <TaskBoard run={run} onChanged={runAsync.reload} />
              ) : null}
              {tab === "plans" ? (
                <PlansView plans={plansAsync.data ?? []} />
              ) : null}
              {tab === "decisions" ? (
                <DecisionsView rows={decisionsAsync.data ?? []} />
              ) : null}
              {tab === "artifacts" ? (
                <ArtifactsView
                  rows={artifactsAsync.data ?? []}
                  onDownload={(a) => downloadFile(`/artifacts/${a.id}/content`, a.name).catch(apiError)}
                  onUpload={async (file) => {
                    try {
                      await runApi.uploadArtifact(run.id, file);
                      toast("已上传");
                      artifactsAsync.reload();
                    } catch (e) {
                      apiError(e);
                    }
                  }}
                />
              ) : null}
            </div>
          </>
        ) : (
          <div className="stage-body">
            <div className="stage-hero">
              <div className="brand-mark big" aria-hidden />
              <h2>提出一次需求</h2>
              <p>
                例如：「调研 2026 年 AI 编程工具市场并写一份简报」。协调者会提出计划，
                需要确认时将在此处出现计划卡。
              </p>
            </div>
          </div>
        )}

        {/* -------- 输入区 -------- */}
        <div className="composer panel">
          <textarea
            id="composer-input"
            rows={2}
            placeholder={effectiveId ? "补充说明，或提出新的需求（Enter 发送 / Shift+Enter 换行）" : "输入需求，开启第一次运行（Enter 发送）"}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-foot">
            <span className="composer-hint mono">
              {wsStatus === "open" ? "" : "实时通道未连接，事件可能延迟 · "}
              有进行中的运行时，消息将作为补充说明注入
            </span>
            <button className="btn-primary" disabled={!draft.trim() || sending} onClick={() => void send()}>
              {sending ? "发送中…" : "发送"}
            </button>
          </div>
        </div>
      </main>

      {/* -------- 事件遥测 -------- */}
      <EventTelemetry runId={runId} events={events} live={live && wsStatus === "open"} />
    </div>
  );
}

function isTerminal(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}

function PlansView({ plans }: { plans: Awaited<ReturnType<typeof runApi.plans>> }) {
  if (plans.length === 0) return <div className="empty">尚无计划版本</div>;
  return (
    <>
      {plans.map((p) => (
        <div className="st-card plan" key={p.id}>
          <div className="st-card-head">
            <span className="eyebrow">计划</span>
            <span className="mono">v{p.version}</span>
            <span className="mono">{p.proposed_by}</span>
            {p.approved ? <span className="chip chip-moss">已通过</span> : <span className="chip chip-ash">未生效</span>}
          </div>
          {p.change_reason ? <p className="st-card-why">{p.change_reason}</p> : null}
          <table className="data st-plan-tasks">
            <thead>
              <tr>
                <th>任务</th>
                <th>负责</th>
                <th>依赖</th>
              </tr>
            </thead>
            <tbody>
              {p.tasks_snapshot.map((t) => (
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
        </div>
      ))}
    </>
  );
}

function DecisionsView({ rows }: { rows: DecisionRow[] }) {
  const LABELS: Record<string, string> = {
    plan_created: "提出计划",
    plan_revised: "修订计划",
    agent_selected: "选定执行者",
    dispatched: "派发",
    review_passed: "复核通过",
    review_failed: "复核不通过",
    reassigned: "改派",
    split: "拆分任务",
    asked_user: "询问用户",
    skipped: "跳过",
    retried: "重试",
    escalated: "上报",
    budget_requested: "请求预算",
    finalized: "终局",
  };
  const MAKER: Record<string, string> = { orchestrator: "协调者", user: "用户", rule: "规则" };
  if (rows.length === 0) return <div className="empty">尚无决策记录</div>;
  return (
    <table className="data">
      <thead>
        <tr>
          <th>时间</th>
          <th>决策</th>
          <th>说明</th>
          <th>依据</th>
          <th>作出者</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.id}>
            <td className="mono">{new Date(d.created_at).toLocaleTimeString("zh-CN", { hour12: false })}</td>
            <td>{LABELS[d.type] ?? d.type}</td>
            <td>{d.summary}</td>
            <td className="st-rationale">{d.rationale}</td>
            <td>{MAKER[d.made_by] ?? d.made_by}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ArtifactsView({
  rows,
  onDownload,
  onUpload,
}: {
  rows: ArtifactRow[];
  onDownload: (a: ArtifactRow) => void;
  onUpload: (f: File) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <div className="artifact-upload">
        <input
          ref={fileRef}
          type="file"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) onUpload(f);
            e.target.value = "";
          }}
        />
        <button className="btn-sm" onClick={() => fileRef.current?.click()}>
          上传输入交付物
        </button>
      </div>
      {rows.length === 0 ? (
        <div className="empty">本次运行尚无交付物</div>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>状态</th>
              <th>大小</th>
              <th>摘要</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id}>
                <td>{a.name}</td>
                <td className="mono">{a.kind}</td>
                <td>
                  <span className="chip chip-ash">{a.status}</span>
                </td>
                <td className="mono">{(a.size / 1024).toFixed(1)} KB</td>
                <td className="st-rationale">{a.summary}</td>
                <td>
                  <button className="btn-sm" onClick={() => onDownload(a)}>
                    下载
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
