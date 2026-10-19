/** 任务板：运行内任务表 + 任务抽屉（尝试详情与人工干预 §10.9）。 */

import { useEffect, useState } from "react";
import { agentApi, attemptApi, taskApi } from "../api/endpoints";
import type { AgentRow, RunDetail, TaskDetail } from "../api/types";
import { Drawer } from "./Drawer";
import { StatusChip } from "./StatusChip";
import { useApp } from "../state/app";

export function TaskBoard({ run, onChanged }: { run: RunDetail; onChanged: () => void }) {
  const { apiError } = useApp();
  const [taskId, setTaskId] = useState<string | null>(null);

  if (run.tasks.length === 0) {
    return <div className="empty">计划尚未提出，暂无任务</div>;
  }

  return (
    <>
      <table className="data">
        <thead>
          <tr>
            <th>任务</th>
            <th>状态</th>
            <th>尝试</th>
            <th>结果摘要</th>
          </tr>
        </thead>
        <tbody>
          {run.tasks.map((t) => (
            <tr key={t.id} className="clickable" onClick={() => setTaskId(t.id)}>
              <td>
                <span className="mono st-taskkey">{t.key}</span> {t.title}
              </td>
              <td>
                <StatusChip kind="task" status={t.status} />
              </td>
              <td className="mono">{t.attempt_count}</td>
              <td className="st-rationale">{t.result_summary ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {taskId ? (
        <TaskDrawer
          taskId={taskId}
          onClose={() => setTaskId(null)}
          onChanged={() => {
            onChanged();
          }}
          onError={apiError}
        />
      ) : null}
    </>
  );
}

function TaskDrawer({
  taskId,
  onClose,
  onChanged,
  onError,
}: {
  taskId: string;
  onClose: () => void;
  onChanged: () => void;
  onError: (e: unknown) => void;
}) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [reassignTo, setReassignTo] = useState("");
  const [inject, setInject] = useState("");
  const [attemptId, setAttemptId] = useState<string | null>(null);

  const load = () => {
    taskApi.get(taskId).then(setTask).catch(onError);
  };
  useEffect(load, [taskId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    agentApi
      .list()
      .then((list) => setAgents(list.filter((a) => a.status === "active")))
      .catch(() => undefined);
  }, []);

  const act = async (_label: string, fn: () => Promise<unknown>) => {
    try {
      await fn();
      load();
      onChanged();
    } catch (e) {
      onError(e);
    }
  };

  const nonTerminal = task ? !isTerminalTask(task.status) : false;
  const canRedispatch = task
    ? ["reported", "blocked", "needs_rework"].includes(task.status)
    : false;

  return (
    <Drawer
      title={
        task ? (
          <>
            <span className="mono st-taskkey">{task.key}</span> {task.title}
          </>
        ) : (
          "任务详情"
        )
      }
      onClose={onClose}
    >
      {!task ? (
        <div className="empty">加载中…</div>
      ) : (
        <>
          <div className="task-actions">
            {nonTerminal ? (
              <>
                <button className="btn-sm" onClick={() => act("取消", () => taskApi.cancel(task.id))}>
                  取消任务
                </button>
                <button className="btn-sm" onClick={() => act("跳过", () => taskApi.skip(task.id))}>
                  跳过任务
                </button>
              </>
            ) : null}
            {task.status === "reported" ? (
              <button className="btn-sm" onClick={() => act("采纳", () => taskApi.accept(task.id))}>
                强制采纳
              </button>
            ) : null}
            {canRedispatch ? (
              <button
                className="btn-sm"
                onClick={() => act("重派", () => taskApi.redispatch(task.id))}
              >
                重派
              </button>
            ) : null}
            <button
              className="btn-sm"
              onClick={() => act("追加次数", () => taskApi.extend(task.id, 1))}
              title="最大尝试次数 +1"
            >
              +尝试次数
            </button>
          </div>

          <dl className="kv" style={{ marginBottom: 16 }}>
            <dt>状态</dt>
            <dd>
              <StatusChip kind="task" status={task.status} />
            </dd>
            <dt>目标</dt>
            <dd>{task.goal}</dd>
            <dt>依赖</dt>
            <dd className="mono">{task.depends_on.join(", ") || "—"}</dd>
            <dt>尝试</dt>
            <dd className="mono">
              {task.attempt_count} / max {task.max_attempts}
            </dd>
          </dl>

          <div className="eyebrow" style={{ marginBottom: 8 }}>
            尝试记录
          </div>
          <table className="data">
            <thead>
              <tr>
                <th>#</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {task.attempts.map((a) => (
                <tr key={a.id}>
                  <td className="mono">{a.number}</td>
                  <td>
                    <StatusChip kind="attempt" status={a.status} />
                  </td>
                  <td>
                    <button className="btn-ghost btn-sm" onClick={() => setAttemptId(a.id)}>
                      详情
                    </button>
                    {a.status === "running" ? (
                      <div className="inject-row">
                        <input
                          type="text"
                          placeholder="向运行中 agent 追加指令"
                          value={inject}
                          onChange={(e) => setInject(e.target.value)}
                        />
                        <button
                          className="btn-sm"
                          onClick={async () => {
                            await act("追加指令", () => attemptApi.inject(a.id, inject));
                            setInject("");
                          }}
                          disabled={!inject.trim()}
                        >
                          注入
                        </button>
                      </div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <div className="eyebrow" style={{ margin: "20px 0 8px" }}>
            改派给其他 agent
          </div>
          <div className="reassign-row">
            <select value={reassignTo} onChange={(e) => setReassignTo(e.target.value)}>
              <option value="">选择 agent…</option>
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}（{a.slug}）
                </option>
              ))}
            </select>
            <button
              className="btn-sm"
              disabled={!reassignTo}
              onClick={() => {
                if (!reassignTo) return;
                void act("改派", () => taskApi.reassign(task.id, reassignTo));
              }}
            >
              改派
            </button>
          </div>

          {attemptId ? (
            <AttemptDetail
              attemptId={attemptId}
              onClose={() => setAttemptId(null)}
              onError={onError}
            />
          ) : null}
        </>
      )}
    </Drawer>
  );
}

function AttemptDetail({
  attemptId,
  onClose,
  onError,
}: {
  attemptId: string;
  onClose: () => void;
  onError: (e: unknown) => void;
}) {
  const [data, setData] = useState<Awaited<ReturnType<typeof attemptApi.get>> | null>(null);
  useEffect(() => {
    attemptApi.get(attemptId).then(setData).catch(onError);
  }, [attemptId]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return null;
  return (
    <div className="attempt-detail">
      <div className="attempt-detail-head">
        <span className="eyebrow">尝试 #{data.number} 详情</span>
        <button className="btn-ghost btn-sm" onClick={onClose}>
          收起
        </button>
      </div>
      <dl className="kv">
        <dt>状态</dt>
        <dd>
          <StatusChip kind="attempt" status={data.status} />
        </dd>
        <dt>工作目录</dt>
        <dd className="mono">{data.workspace_path}</dd>
        {data.usage ? (
          <>
            <dt>用量</dt>
            <dd className="mono">{JSON.stringify(data.usage)}</dd>
          </>
        ) : null}
        {data.error ? (
          <>
            <dt>错误</dt>
            <dd className="st-error">{data.error}</dd>
          </>
        ) : null}
      </dl>
      {data.brief ? (
        <details open>
          <summary>任务单</summary>
          <pre className="mono">{JSON.stringify(data.brief, null, 2)}</pre>
        </details>
      ) : null}
      {data.report ? (
        <details open>
          <summary>回报</summary>
          <pre className="mono">{JSON.stringify(data.report, null, 2)}</pre>
        </details>
      ) : null}
      {data.review ? (
        <details>
          <summary>复核</summary>
          <pre className="mono">{JSON.stringify(data.review, null, 2)}</pre>
        </details>
      ) : null}
    </div>
  );
}

function isTerminalTask(status: string): boolean {
  return ["accepted", "failed", "skipped", "superseded", "cancelled"].includes(status);
}
