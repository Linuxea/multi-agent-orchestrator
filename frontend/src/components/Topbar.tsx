import { useEffect, useState } from "react";
import { Link, NavLink } from "react-router-dom";
import { approvalApi } from "../api/endpoints";
import { useApp } from "../state/app";

const NAV = [
  { to: "/", label: "运行工作台" },
  { to: "/approvals", label: "审批中心" },
  { to: "/agents", label: "Agent 注册表" },
  { to: "/teams", label: "团队" },
  { to: "/tools", label: "工具库" },
  { to: "/models", label: "模型配置" },
  { to: "/secrets", label: "密钥" },
  { to: "/settings", label: "设置" },
];

const WS_LABEL: Record<string, [string, string]> = {
  connecting: ["连接中…", "var(--c-amber)"],
  open: ["实时通道", "var(--c-moss)"],
  closed: ["已离线", "var(--ink-faint)"],
  unauthorized: ["密钥失效", "var(--c-ember)"],
};

export function Topbar() {
  const { wsStatus, logout, onEvent, subscribe } = useApp();
  const [pending, setPending] = useState<number | null>(null);

  const refresh = () => {
    approvalApi
      .list("pending")
      .then((rows) => setPending(rows.length))
      .catch(() => undefined);
  };

  useEffect(() => {
    refresh();
    subscribe("global");
    const off = onEvent((ev) => {
      if (ev.type === "approval.requested" || ev.type === "approval.decided" || ev.type === "approval.expired") {
        refresh();
      }
    });
    return off;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [wsLabel, wsColor] = WS_LABEL[wsStatus] ?? ["未知", "var(--ink-faint)"];

  return (
    <header className="topbar">
      <Link to="/" className="brand">
        <span className="brand-mark" aria-hidden />
        <span className="brand-name">协调平台</span>
      </Link>
      <nav className="topnav" aria-label="主导航">
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === "/"}
            className={({ isActive }) =>
              `topnav-item${isActive ? " on" : ""}${n.to === "/approvals" && pending ? " has-badge" : ""}`
            }
          >
            {n.label}
            {n.to === "/approvals" && pending ? (
              <span className="topnav-badge">{pending}</span>
            ) : null}
          </NavLink>
        ))}
      </nav>
      <div className="topbar-right">
        <span className="ws-state mono" style={{ color: wsColor }}>
          ● {wsLabel}
        </span>
        <button className="btn-ghost btn-sm" onClick={logout}>
          退出
        </button>
      </div>
    </header>
  );
}
