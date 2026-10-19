import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AppProvider, useApp } from "./state/app";
import { Topbar } from "./components/Topbar";
import { LoginPage } from "./pages/LoginPage";
import { WorkspacePage } from "./pages/WorkspacePage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AgentsPage } from "./pages/AgentsPage";
import { ToolsPage } from "./pages/ToolsPage";
import { ModelsPage } from "./pages/ModelsPage";
import { SecretsPage } from "./pages/SecretsPage";
import { TeamsPage } from "./pages/TeamsPage";
import { SettingsPage } from "./pages/SettingsPage";

function Shell() {
  const { authRequired, authChecked, toasts } = useApp();

  if (!authChecked) {
    return (
      <div className="app-boot">
        <span className="mono">正在连接协调平台…</span>
      </div>
    );
  }
  if (authRequired) return <LoginPage />;

  return (
    <div className="app-shell">
      <Topbar />
      <div className="app-body">
        <Routes>
          <Route path="/" element={<WorkspacePage />} />
          <Route path="/s/:sessionId" element={<WorkspacePage />} />
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/tools" element={<ToolsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/secrets" element={<SecretsPage />} />
          <Route path="/teams" element={<TeamsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      <div className="toaster" role="status">
        {toasts.map((t) => (
          <div key={t.id} className={`toast${t.kind === "err" ? " toast-err" : ""}`}>
            {t.text}
          </div>
        ))}
      </div>
    </div>
  );
}

export function App() {
  return (
    <AppProvider>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </AppProvider>
  );
}
