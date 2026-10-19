/** 密钥库：写入加密存储；任何接口不回明文。 */

import { useState } from "react";
import { secretApi } from "../api/endpoints";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

export function SecretsPage() {
  const { apiError, toast } = useApp();
  const secretsAsync = useAsync(() => secretApi.list(), []);
  const secrets = secretsAsync.data ?? [];
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");

  const create = async () => {
    setBusy(true);
    try {
      await secretApi.create(name.trim(), value, description);
      toast("密钥已加密保存");
      setName("");
      setValue("");
      setDescription("");
      secretsAsync.reload();
    } catch (e) {
      apiError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 860, margin: "0 auto" }}>
      <div className="page-head">
        <h1>密钥库</h1>
        <span className="page-sub">
          工具配置内以 {"{"}"$secret": "名称"{"}"} 引用；仅在物化时解密，经内存下发
        </span>
      </div>

      <div className="panel panel-pad" style={{ marginBottom: 20 }}>
        <div className="eyebrow" style={{ marginBottom: 10 }}>新增密钥</div>
        <div className="form-row">
          <label className="field">
            <span className="field-label">名称</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="GITHUB_TOKEN" />
          </label>
          <label className="field">
            <span className="field-label">值（加密后丢弃明文）</span>
            <input type="password" value={value} onChange={(e) => setValue(e.target.value)} />
          </label>
        </div>
        <label className="field">
          <span className="field-label">说明</span>
          <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <button className="btn-primary btn-sm" disabled={busy || !name.trim() || !value} onClick={() => void create()}>
          加密保存
        </button>
      </div>

      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>说明</th>
              <th>最近使用</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {secrets.map((s) => (
              <tr key={s.id}>
                <td className="mono">{s.name}</td>
                <td className="st-rationale">{s.description || "—"}</td>
                <td className="mono">
                  {s.last_used_at
                    ? new Date(s.last_used_at).toLocaleString("zh-CN", { hour12: false })
                    : "从未"}
                </td>
                <td>
                  {editing === s.id ? (
                    <div className="inject-row">
                      <input
                        type="password"
                        placeholder="新值"
                        value={editValue}
                        onChange={(e) => setEditValue(e.target.value)}
                      />
                      <button
                        className="btn-sm btn-primary"
                        disabled={!editValue}
                        onClick={async () => {
                          try {
                            await secretApi.update(s.id, editValue, s.description);
                            toast("已更新");
                            setEditing(null);
                            setEditValue("");
                          } catch (e) {
                            apiError(e);
                          }
                        }}
                      >
                        保存
                      </button>
                      <button className="btn-ghost btn-sm" onClick={() => setEditing(null)}>
                        取消
                      </button>
                    </div>
                  ) : (
                    <div style={{ display: "flex", gap: 6 }}>
                      <button
                        className="btn-sm"
                        onClick={() => {
                          setEditing(s.id);
                          setEditValue("");
                        }}
                      >
                        换值
                      </button>
                      <button
                        className="btn-ghost btn-sm"
                        onClick={async () => {
                          try {
                            await secretApi.remove(s.id);
                            secretsAsync.reload();
                          } catch (e) {
                            apiError(e);
                          }
                        }}
                      >
                        删除
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {secrets.length === 0 ? <div className="empty">密钥库为空</div> : null}
      </div>
    </div>
  );
}
