/** 审批中心：待办审批的批准/拒绝（remember 三档）。 */

import { useCallback, useEffect, useState } from "react";
import { approvalApi } from "../api/endpoints";
import type { ApprovalRow, RememberScope } from "../api/types";
import { useApp } from "../state/app";
import { statusLabel, StatusChip } from "../components/StatusChip";

const REMEMBER_LABEL: Record<RememberScope, string> = {
  once: "仅本次",
  this_run: "本次运行内放行",
  always_for_agent: "始终允许该 agent",
};

export function ApprovalsPage() {
  const { apiError, toast, onEvent } = useApp();
  const [status, setStatus] = useState("pending");
  const [rows, setRows] = useState<ApprovalRow[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    approvalApi
      .list(status)
      .then(setRows)
      .catch(apiError);
  }, [status, apiError]);

  useEffect(load, [load]);

  // 审批事件到达时刷新（含 global 通道）
  useEffect(() => {
    const off = onEvent((ev) => {
      if (ev.type.startsWith("approval.")) load();
    });
    return off;
  }, [onEvent, load]);

  const decide = async (row: ApprovalRow, approved: boolean) => {
    setBusyId(row.id);
    try {
      const remember = readRemember(row.id);
      await approvalApi.decide(row.id, approved, "", remember);
      toast(approved ? "已批准" : "已拒绝");
      load();
    } catch (e) {
      apiError(e);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 900, margin: "0 auto" }}>
      <div className="page-head">
        <h1>审批中心</h1>
        <span className="page-sub">
          ask 权限的工具调用会在此等待决定；超时未决定将按拒绝处理
        </span>
      </div>

      <div className="approval-filters">
        {["pending", "approved", "rejected", "expired"].map((s) => (
          <button
            key={s}
            className={`btn btn-sm${status === s ? " btn-primary" : ""}`}
            onClick={() => setStatus(s)}
          >
            {statusLabel("approval", s)[0]}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <div className="panel panel-pad empty">
          {status === "pending" ? "没有待审批的调用" : "暂无记录"}
        </div>
      ) : (
        <div className="approval-list">
          {rows.map((row) => (
            <div className="panel panel-pad approval-card" key={row.id}>
              <div className="approval-card-head">
                <StatusChip kind="approval" status={row.status} />
                <span className="mono approval-tool">{row.tool_slug}</span>
                <span className="approval-action">{row.action}</span>
                <span className="mono approval-meta">
                  {row.requested_by} · 运行 {row.run_id.slice(-6)}
                </span>
              </div>
              {Object.keys(row.arguments).length > 0 ? (
                <details>
                  <summary>调用参数（已脱敏）</summary>
                  <pre className="mono">{JSON.stringify(row.arguments, null, 2)}</pre>
                </details>
              ) : null}
              {row.risk_note ? <p className="approval-risk">⚠ {row.risk_note}</p> : null}
              {row.reason ? <p className="approval-reason">判定依据：{row.reason}</p> : null}
              {row.status === "pending" ? (
                <div className="approval-decide">
                  <label className="field remember-field">
                    <span className="field-label">批准时记忆范围</span>
                    <div className="remember-options">
                      {(["once", "this_run", "always_for_agent"] as RememberScope[]).map((r) => (
                        <label key={r} className="remember-opt">
                          <input
                            type="radio"
                            name={`remember-${row.id}`}
                            value={r}
                            defaultChecked={r === "once"}
                            onChange={() => setRemember(row.id, r)}
                          />
                          {REMEMBER_LABEL[r]}
                        </label>
                      ))}
                    </div>
                  </label>
                  <div className="approval-buttons">
                    <button
                      className="btn-primary"
                      disabled={busyId === row.id}
                      onClick={() => decide(row, true)}
                    >
                      批准
                    </button>
                    <button
                      className="btn-danger"
                      disabled={busyId === row.id}
                      onClick={() => decide(row, false)}
                    >
                      拒绝
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// remember 单选值按审批单暂存（组件外 Map，避免受控渲染开销）
const rememberStore = new Map<string, RememberScope>();
function setRemember(id: string, scope: RememberScope) {
  rememberStore.set(id, scope);
}
function readRemember(id: string): RememberScope {
  return rememberStore.get(id) ?? "once";
}
