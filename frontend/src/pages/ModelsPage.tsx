/** 模型配置：CRUD + 兼容性体检（四项 + 时延）。 */

import { useState } from "react";
import { modelApi } from "../api/endpoints";
import type { ModelCheckResult, ModelProfileRow } from "../api/types";
import { Drawer } from "../components/Drawer";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

export function ModelsPage() {
  const { apiError } = useApp();
  const profilesAsync = useAsync(() => modelApi.list(), []);
  const profiles = profilesAsync.data ?? [];
  const [editing, setEditing] = useState<ModelProfileRow | "new" | null>(null);
  const [checks, setChecks] = useState<Record<string, ModelCheckResult | "running">>({});

  const runCheck = async (p: ModelProfileRow) => {
    setChecks((prev) => ({ ...prev, [p.id]: "running" }));
    try {
      const r = await modelApi.check(p.id);
      setChecks((prev) => ({ ...prev, [p.id]: r }));
    } catch (e) {
      apiError(e);
      setChecks((prev) => {
        const next = { ...prev };
        delete next[p.id];
        return next;
      });
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>模型配置</h1>
        <span className="page-sub">Anthropic 兼容或 OpenAI 兼容端点；密钥加密存储、接口不回明文</span>
        <button
          className="btn-primary btn-sm"
          style={{ marginLeft: "auto" }}
          onClick={() => setEditing("new")}
        >
          + 新建配置
        </button>
      </div>

      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>模型</th>
              <th>端点</th>
              <th>单价（入/出）</th>
              <th>默认</th>
              <th>体检</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((p) => {
              const c = checks[p.id];
              return (
                <tr key={p.id}>
                  <td className="clickable" onClick={() => setEditing(p)}>
                    {p.name}
                  </td>
                  <td className="mono">{p.model}</td>
                  <td className="mono st-rationale">
                    {p.base_url}{" "}
                    {p.protocol === "openai_chat" ? (
                      <span className="chip chip-dusk">OpenAI 协议</span>
                    ) : null}
                  </td>
                  <td className="mono">
                    {p.price_input}/{p.price_output}
                  </td>
                  <td>
                    {p.is_default_orchestrator ? <span className="chip chip-dusk">协调</span> : null}{" "}
                    {p.is_default_worker ? <span className="chip chip-signal">执行</span> : null}
                  </td>
                  <td>
                    {c === "running" ? (
                      <span className="mono">检测中…</span>
                    ) : c ? (
                      <span className={`chip ${c.ok ? "chip-moss" : "chip-ember"}`}>
                        {c.ok ? "全部通过" : "存在失败项"}
                      </span>
                    ) : (
                      <button className="btn-sm" onClick={() => void runCheck(p)}>
                        体检
                      </button>
                    )}
                  </td>
                  <td>
                    <button className="btn-ghost btn-sm" onClick={() => setEditing(p)}>
                      编辑
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {profiles.length === 0 ? <div className="empty">尚无模型配置</div> : null}
      </div>

      {Object.entries(checks).map(([id, c]) =>
        c === "running" || !c ? null : (
          <div className="panel panel-pad" style={{ marginTop: 16 }} key={id}>
            <div className="eyebrow" style={{ marginBottom: 10 }}>
              体检报告 · {c.model} @ {c.base_url}
            </div>
            <table className="data">
              <thead>
                <tr>
                  <th>检查项</th>
                  <th>结果</th>
                  <th>时延</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {c.checks.map((k) => (
                  <tr key={k.name}>
                    <td>{k.name}</td>
                    <td>
                      <span className={`chip ${k.passed ? "chip-moss" : "chip-ember"}`}>
                        {k.passed ? "通过" : "失败"}
                      </span>
                    </td>
                    <td className="mono">{k.latency_ms} ms</td>
                    <td className="st-rationale">{k.detail || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ),
      )}

      {editing ? (
        <ModelEditor
          profile={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            profilesAsync.reload();
          }}
          onError={apiError}
        />
      ) : null}
    </div>
  );
}

function ModelEditor({
  profile,
  onClose,
  onSaved,
  onError,
}: {
  profile: ModelProfileRow | null;
  onClose: () => void;
  onSaved: () => void;
  onError: (e: unknown) => void;
}) {
  const isNew = profile === null;
  const [name, setName] = useState(profile?.name ?? "");
  const [protocol, setProtocol] = useState(profile?.protocol ?? "anthropic_messages");
  const [baseUrl, setBaseUrl] = useState(profile?.base_url ?? "");
  const [model, setModel] = useState(profile?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [maxOutput, setMaxOutput] = useState(profile?.max_output_tokens ?? 8192);
  const [temperature, setTemperature] = useState(String(profile?.temperature ?? ""));
  const [contextWindow, setContextWindow] = useState(profile?.context_window ?? 128000);
  const [priceInput, setPriceInput] = useState(String(profile?.price_input ?? 0));
  const [priceOutput, setPriceOutput] = useState(String(profile?.price_output ?? 0));
  const [isOrch, setIsOrch] = useState(profile?.is_default_orchestrator ?? false);
  const [isWorker, setIsWorker] = useState(profile?.is_default_worker ?? false);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        name,
        protocol,
        base_url: baseUrl,
        model,
        max_output_tokens: Number(maxOutput),
        context_window: Number(contextWindow),
        price_input: Number(priceInput),
        price_output: Number(priceOutput),
        is_default_orchestrator: isOrch,
        is_default_worker: isWorker,
      };
      if (temperature.trim()) body["temperature"] = Number(temperature);
      if (apiKey.trim()) body["api_key"] = apiKey;
      if (isNew) await modelApi.create(body);
      else await modelApi.update(profile.id, body);
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer title={isNew ? "新建模型配置" : `编辑 ${profile!.name}`} onClose={onClose}>
      <div className="form-row">
        <label className="field">
          <span className="field-label">名称</span>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">模型 ID</span>
          <input type="text" value={model} onChange={(e) => setModel(e.target.value)} />
        </label>
      </div>
      <label className="field">
        <span className="field-label">协议</span>
        <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
          <option value="anthropic_messages">Anthropic Messages（/v1/messages）</option>
          <option value="openai_chat">OpenAI 兼容（/v1/chat/completions）</option>
        </select>
      </label>
      <label className="field">
        <span className="field-label">
          端点 base_url（{protocol === "openai_chat" ? "如 https://api.openai.com/v1" : "Anthropic 兼容"}）
        </span>
        <input
          className="code"
          type="text"
          value={baseUrl}
          placeholder={
            protocol === "openai_chat"
              ? "https://api.openai.com/v1"
              : "https://api.deepseek.com/anthropic"
          }
          onChange={(e) => setBaseUrl(e.target.value)}
        />
      </label>
      <label className="field">
        <span className="field-label">
          API Key {isNew ? "（将加密存入密钥库）" : "（留空表示不修改）"}
        </span>
        <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
      </label>
      <div className="form-row">
        <label className="field">
          <span className="field-label">最大输出 tokens</span>
          <input type="number" value={maxOutput} onChange={(e) => setMaxOutput(Number(e.target.value))} />
        </label>
        <label className="field">
          <span className="field-label">上下文窗口</span>
          <input
            type="number"
            value={contextWindow}
            onChange={(e) => setContextWindow(Number(e.target.value))}
          />
        </label>
      </div>
      <div className="form-row">
        <label className="field">
          <span className="field-label">温度（可空）</span>
          <input type="text" value={temperature} onChange={(e) => setTemperature(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">单价（元 / 百万 tokens）</span>
          <div style={{ display: "flex", gap: 8 }}>
            <input type="text" value={priceInput} onChange={(e) => setPriceInput(e.target.value)} />
            <input type="text" value={priceOutput} onChange={(e) => setPriceOutput(e.target.value)} />
          </div>
        </label>
      </div>
      <div className="form-row">
        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={isOrch}
            onChange={(e) => setIsOrch(e.target.checked)}
          />
          <span>默认协调者模型</span>
        </label>
        <label className="field checkbox-field">
          <input
            type="checkbox"
            checked={isWorker}
            onChange={(e) => setIsWorker(e.target.checked)}
          />
          <span>默认执行 agent 模型</span>
        </label>
      </div>
      <button
        className="btn-primary"
        disabled={busy || !name.trim() || !baseUrl.trim() || !model.trim()}
        onClick={() => void save()}
      >
        {busy ? "保存中…" : "保存"}
      </button>
    </Drawer>
  );
}
