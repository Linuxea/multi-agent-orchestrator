import { useState } from "react";
import { useApp } from "../state/app";

export function LoginPage() {
  const { login } = useApp();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const ok = await login(password);
    setBusy(false);
    if (!ok) setError("访问密码不正确");
  };

  return (
    <div className="login-wrap">
      <form className="login-card panel" onSubmit={submit}>
        <div className="brand-login">
          <span className="brand-mark big" aria-hidden />
          <h1>多 Agent 协调平台</h1>
        </div>
        <p className="login-sub">本平台已启用访问密码保护，请输入后进入调度台。</p>
        <label className="field">
          <span className="field-label">访问密码</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoFocus
            autoComplete="current-password"
          />
        </label>
        {error ? <p className="login-err">{error}</p> : null}
        <button className="btn-primary" type="submit" disabled={busy || !password}>
          {busy ? "验证中…" : "进入调度台"}
        </button>
      </form>
    </div>
  );
}
