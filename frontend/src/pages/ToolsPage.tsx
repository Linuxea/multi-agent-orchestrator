/** 工具库：注册 → 测试 → 发现 →（绑定在 Agent 页）。 */

import { useState } from "react";
import { toolApi } from "../api/endpoints";
import { Drawer } from "../components/Drawer";
import { StatusChip } from "../components/StatusChip";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

const KIND_LABEL: Record<string, string> = {
  builtin: "CLI 内建",
  mcp: "MCP",
  skill: "Skill",
  command: "Command",
  hook: "Hook",
  custom_http: "自定义 HTTP",
  custom_python: "自定义 Python",
};

const KIND_CONFIG_TEMPLATE: Record<string, () => string> = {
  mcp: () => JSON.stringify({ transport: "stdio", command: "", args: [] }, null, 2),
  skill: () => JSON.stringify({ path: "" }, null, 2),
  command: () => JSON.stringify({ command: "" }, null, 2),
  hook: () => JSON.stringify({ event: "PreToolUse", command: "" }, null, 2),
  custom_http: () => JSON.stringify({ method: "GET", url: "" }, null, 2),
  custom_python: () => JSON.stringify({ code: "" }, null, 2),
};

export function ToolsPage() {
  const { apiError, toast } = useApp();
  const toolsAsync = useAsync(() => toolApi.list(), []);
  const tools = toolsAsync.data ?? [];
  const [creating, setCreating] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, unknown> | null>(null);
  const [discovered, setDiscovered] = useState<{ name: string; description?: string }[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  return (
    <div className="page">
      <div className="page-head">
        <h1>工具库</h1>
        <span className="page-sub">
          MCP / Skills / Commands / Hooks / 自定义工具；先测试再发现子工具
        </span>
        <button className="btn-primary btn-sm" style={{ marginLeft: "auto" }} onClick={() => setCreating(true)}>
          + 注册工具
        </button>
      </div>

      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>默认权限</th>
              <th>状态</th>
              <th>最近测试</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tools.map((t) => (
              <tr key={t.id}>
                <td>
                  {t.name} <span className="mono st-taskkey">{t.slug}</span>
                </td>
                <td>{KIND_LABEL[t.kind] ?? t.kind}</td>
                <td className="mono">{t.default_permission}</td>
                <td>
                  <StatusChip kind="tool" status={t.status} />
                </td>
                <td className="mono st-rationale">
                  {t.last_test ? testSummary(t.last_test) : "—"}
                </td>
                <td>
                  <div style={{ display: "flex", gap: 6 }}>
                    <button
                      className="btn-sm"
                      disabled={busyId === t.id}
                      onClick={async () => {
                        setBusyId(t.id);
                        setDiscovered(null);
                        setTestResult(null);
                        try {
                          setTestResult(await toolApi.test(t.id));
                          toolsAsync.reload();
                        } catch (e) {
                          apiError(e);
                        } finally {
                          setBusyId(null);
                        }
                      }}
                    >
                      测试
                    </button>
                    {t.kind === "mcp" ? (
                      <button
                        className="btn-sm"
                        disabled={busyId === t.id}
                        onClick={async () => {
                          setBusyId(t.id);
                          setTestResult(null);
                          try {
                            const r = await toolApi.discover(t.id);
                            setDiscovered(r.tools);
                            toast(`发现 ${r.tools.length} 个子工具`);
                            toolsAsync.reload();
                          } catch (e) {
                            apiError(e);
                          } finally {
                            setBusyId(null);
                          }
                        }}
                      >
                        发现
                      </button>
                    ) : null}
                    {t.kind !== "builtin" ? (
                      <button
                        className="btn-ghost btn-sm"
                        onClick={async () => {
                          try {
                            await toolApi.remove(t.id);
                            toolsAsync.reload();
                          } catch (e) {
                            apiError(e);
                          }
                        }}
                      >
                        删除
                      </button>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {tools.length === 0 ? <div className="empty">尚未注册工具</div> : null}
      </div>

      {testResult || discovered ? (
        <div className="panel panel-pad" style={{ marginTop: 16 }}>
          {testResult ? (
            <>
              <div className="eyebrow" style={{ marginBottom: 8 }}>测试结果</div>
              <pre className="mono tool-pre">{JSON.stringify(testResult, null, 2)}</pre>
            </>
          ) : null}
          {discovered ? (
            <>
              <div className="eyebrow" style={{ margin: "12px 0 8px" }}>发现的子工具（已缓存到 discovered）</div>
              <ul className="disc-list">
                {discovered.map((d) => (
                  <li key={d.name}>
                    <span className="mono">{d.name}</span>
                    {d.description ? <span className="st-rationale"> — {d.description}</span> : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </div>
      ) : null}

      {creating ? (
        <ToolCreateDrawer
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            toolsAsync.reload();
          }}
          onError={apiError}
        />
      ) : null}
    </div>
  );
}

function testSummary(lastTest: Record<string, unknown>): string {
  const ok = lastTest["ok"];
  const detail = lastTest["detail"] ?? lastTest["error"] ?? "";
  return `${ok ? "通过" : "失败"}${detail ? `：${String(detail).slice(0, 60)}` : ""}`;
}

function ToolCreateDrawer({
  onClose,
  onCreated,
  onError,
}: {
  onClose: () => void;
  onCreated: () => void;
  onError: (e: unknown) => void;
}) {
  const [kind, setKind] = useState("mcp");
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [permission, setPermission] = useState("ask");
  const [config, setConfig] = useState(KIND_CONFIG_TEMPLATE["mcp"]!());
  const [busy, setBusy] = useState(false);

  const pickKind = (k: string) => {
    setKind(k);
    setConfig((KIND_CONFIG_TEMPLATE[k] ?? (() => "{}"))());
  };

  const save = async () => {
    setBusy(true);
    try {
      await toolApi.create({
        kind,
        slug,
        name,
        description,
        default_permission: permission,
        config: JSON.parse(config || "{}") as Record<string, unknown>,
      });
      onCreated();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer title="注册工具" onClose={onClose}>
      <div className="form-row">
        <label className="field">
          <span className="field-label">类型</span>
          <select value={kind} onChange={(e) => pickKind(e.target.value)}>
            {Object.keys(KIND_CONFIG_TEMPLATE).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">默认权限</span>
          <select value={permission} onChange={(e) => setPermission(e.target.value)}>
            <option value="allow">allow（放行）</option>
            <option value="ask">ask（需审批）</option>
            <option value="deny">deny（拒绝）</option>
          </select>
        </label>
      </div>
      <div className="form-row">
        <label className="field">
          <span className="field-label">slug（唯一标识）</span>
          <input type="text" value={slug} onChange={(e) => setSlug(e.target.value)} />
        </label>
        <label className="field">
          <span className="field-label">名称</span>
          <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
      </div>
      <label className="field">
        <span className="field-label">描述</span>
        <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label className="field">
        <span className="field-label">
          配置（JSON{kind === "mcp" ? "：stdio 用 command/args，HTTP 用 transport/url" : ""}；
          密钥写 {"{"}"$secret": "名称"{"}"} 引用）
        </span>
        <textarea className="code" rows={10} value={config} onChange={(e) => setConfig(e.target.value)} />
      </label>
      <button className="btn-primary" disabled={busy || !slug.trim() || !name.trim()} onClick={() => void save()}>
        {busy ? "注册中…" : "注册"}
      </button>
    </Drawer>
  );
}
