/** 全局设置（§13.1）：含访问密码（§11.4）。 */

import { useEffect, useState } from "react";
import { modelApi, settingsApi } from "../api/endpoints";
import type { ModelProfileRow, PlatformSettings } from "../api/types";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

export function SettingsPage() {
  const { apiError, toast } = useApp();
  const settingsAsync = useAsync(() => settingsApi.get(), []);
  const modelsAsync = useAsync(() => modelApi.list(), []);
  const s = settingsAsync.data;
  const models = modelsAsync.data ?? [];
  const [form, setForm] = useState<PlatformSettings | null>(null);
  const [password, setPassword] = useState("");
  const [dangerous, setDangerous] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (s && !form) {
      setForm(s);
      setDangerous((s.dangerous_command_list ?? []).join("\n"));
    }
  }, [s, form]);

  if (!form) {
    return (
      <div className="page">
        <div className="empty">加载设置中…</div>
      </div>
    );
  }

  const set = <K extends keyof PlatformSettings>(key: K, value: PlatformSettings[K]) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
  };

  const save = async () => {
    setBusy(true);
    try {
      const changes: Record<string, unknown> = {
        orchestrator_model_profile_id: form.orchestrator_model_profile_id,
        worker_default_model_profile_id: form.worker_default_model_profile_id,
        plan_approval_mode: form.plan_approval_mode,
        plan_auto_approve_cost_threshold: Number(form.plan_auto_approve_cost_threshold),
        global_parallel_limit: Number(form.global_parallel_limit),
        run_parallel_limit: Number(form.run_parallel_limit),
        max_tasks_per_plan: Number(form.max_tasks_per_plan),
        default_max_attempts: Number(form.default_max_attempts),
        approval_timeout_minutes: Number(form.approval_timeout_minutes),
        workspace_retention_days: Number(form.workspace_retention_days),
        allow_custom_python_tools: form.allow_custom_python_tools,
        dangerous_command_list: dangerous.split("\n").map((x) => x.trim()).filter(Boolean),
        default_run_budget: form.default_run_budget,
      };
      if (form.daily_cost_limit !== null && form.daily_cost_limit !== undefined && String(form.daily_cost_limit) !== "") {
        changes["daily_cost_limit"] = Number(form.daily_cost_limit);
      } else {
        changes["daily_cost_limit"] = null;
      }
      if (password.trim()) changes["access_password"] = password.trim();
      const updated = await settingsApi.patch(changes);
      setForm(updated);
      setPassword("");
      toast("设置已保存");
    } catch (e) {
      apiError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 760, margin: "0 auto" }}>
      <div className="page-head">
        <h1>设置</h1>
        <span className="page-sub">全局运行参数（§13.1）；改动即刻生效</span>
      </div>

      <section className="panel panel-pad settings-section">
        <div className="eyebrow">模型</div>
        <div className="form-row">
          <label className="field">
            <span className="field-label">协调者默认模型</span>
            <select
              value={form.orchestrator_model_profile_id ?? ""}
              onChange={(e) => set("orchestrator_model_profile_id", e.target.value || null)}
            >
              <option value="">（未设置）</option>
              {models.map((m: ModelProfileRow) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">执行 agent 默认模型</span>
            <select
              value={form.worker_default_model_profile_id ?? ""}
              onChange={(e) => set("worker_default_model_profile_id", e.target.value || null)}
            >
              <option value="">（未设置）</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>

      <section className="panel panel-pad settings-section">
        <div className="eyebrow">计划与审批</div>
        <div className="form-row">
          <label className="field">
            <span className="field-label">计划审批模式</span>
            <select
              value={form.plan_approval_mode}
              onChange={(e) =>
                set("plan_approval_mode", e.target.value as PlatformSettings["plan_approval_mode"])
              }
            >
              <option value="always">always（每次都要确认）</option>
              <option value="auto_if_under">auto_if_under（低于阈值自动通过）</option>
              <option value="never">never（从不打断）</option>
            </select>
          </label>
          <label className="field">
            <span className="field-label">自动通过阈值（预估费用）</span>
            <input
              type="number"
              step="0.1"
              value={String(form.plan_auto_approve_cost_threshold)}
              onChange={(e) => set("plan_auto_approve_cost_threshold", Number(e.target.value))}
            />
          </label>
        </div>
        <div className="form-row">
          <label className="field">
            <span className="field-label">审批超时（分钟，超时按拒绝）</span>
            <input
              type="number"
              value={String(form.approval_timeout_minutes)}
              onChange={(e) => set("approval_timeout_minutes", Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span className="field-label">默认运行预算（费用上限）</span>
            <input
              type="number"
              step="0.5"
              value={String(form.default_run_budget?.max_cost ?? 5)}
              onChange={(e) =>
                set("default_run_budget", {
                  ...(form.default_run_budget ?? {}),
                  max_cost: Number(e.target.value),
                })
              }
            />
          </label>
        </div>
      </section>

      <section className="panel panel-pad settings-section">
        <div className="eyebrow">调度</div>
        <div className="form-row">
          <label className="field">
            <span className="field-label">全局并行上限</span>
            <input
              type="number"
              value={String(form.global_parallel_limit)}
              onChange={(e) => set("global_parallel_limit", Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span className="field-label">单运行并行上限</span>
            <input
              type="number"
              value={String(form.run_parallel_limit)}
              onChange={(e) => set("run_parallel_limit", Number(e.target.value))}
            />
          </label>
        </div>
        <div className="form-row">
          <label className="field">
            <span className="field-label">计划任务数上限</span>
            <input
              type="number"
              value={String(form.max_tasks_per_plan)}
              onChange={(e) => set("max_tasks_per_plan", Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span className="field-label">任务默认最大尝试次数</span>
            <input
              type="number"
              value={String(form.default_max_attempts)}
              onChange={(e) => set("default_max_attempts", Number(e.target.value))}
            />
          </label>
        </div>
      </section>

      <section className="panel panel-pad settings-section">
        <div className="eyebrow">安全</div>
        <label className="field">
          <span className="field-label">
            访问密码 {form.access_password ? "（已启用；留空保持不变）" : "（未启用）"}
          </span>
          <input
            type="password"
            value={password}
            placeholder={form.access_password ? "输入新密码以更换" : "设置后所有 API 与 WS 需要密码"}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label className="field">
          <span className="field-label">高危命令表（每行一条，bash 类调用强制拒绝）</span>
          <textarea
            className="code"
            rows={5}
            value={dangerous}
            onChange={(e) => setDangerous(e.target.value)}
          />
        </label>
        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={form.allow_custom_python_tools}
            onChange={(e) => set("allow_custom_python_tools", e.target.checked)}
          />
          <span>允许 custom_python 工具（子进程执行代码，谨慎开启）</span>
        </label>
        <div className="form-row">
          <label className="field">
            <span className="field-label">日费用上限（空 = 关闭）</span>
            <input
              type="text"
              value={form.daily_cost_limit === null ? "" : String(form.daily_cost_limit)}
              onChange={(e) =>
                set("daily_cost_limit", e.target.value.trim() === "" ? null : Number(e.target.value))
              }
            />
          </label>
          <label className="field">
            <span className="field-label">工作目录保留天数</span>
            <input
              type="number"
              value={String(form.workspace_retention_days)}
              onChange={(e) => set("workspace_retention_days", Number(e.target.value))}
            />
          </label>
        </div>
      </section>

      <button className="btn-primary" disabled={busy} onClick={() => void save()}>
        {busy ? "保存中…" : "保存全部设置"}
      </button>
    </div>
  );
}
