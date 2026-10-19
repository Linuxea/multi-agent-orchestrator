/** 团队：基础管理（完整团队协作在第四期）。 */

import { useState } from "react";
import { agentApi, teamApi } from "../api/endpoints";
import type { AgentRow, TeamRow } from "../api/types";
import { Drawer } from "../components/Drawer";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

export function TeamsPage() {
  const { apiError } = useApp();
  const teamsAsync = useAsync(() => teamApi.list(), []);
  const agentsAsync = useAsync(() => agentApi.list(), []);
  const teams = teamsAsync.data ?? [];
  const agents = agentsAsync.data ?? [];
  const [editing, setEditing] = useState<TeamRow | "new" | null>(null);

  const agentName = (id: string) => agents.find((a) => a.id === id)?.name ?? id.slice(-6);

  return (
    <div className="page">
      <div className="page-head">
        <h1>团队</h1>
        <span className="page-sub">成员组合 + 剧本（playbook）提示协调者优先编队</span>
        <button className="btn-primary btn-sm" style={{ marginLeft: "auto" }} onClick={() => setEditing("new")}>
          + 新建团队
        </button>
      </div>
      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>slug</th>
              <th>成员</th>
              <th>剧本</th>
              <th>组长提示</th>
            </tr>
          </thead>
          <tbody>
            {teams.map((t) => (
              <tr key={t.id} className="clickable" onClick={() => setEditing(t)}>
                <td>{t.name}</td>
                <td className="mono">{t.slug}</td>
                <td>
                  {t.member_agent_ids.map((id, i) => (
                    <span key={id}>
                      {i > 0 ? "、" : ""}
                      {agentName(id)}
                    </span>
                  ))}
                </td>
                <td>{t.playbook?.name ?? "—"}</td>
                <td className="st-rationale">{t.lead_hint || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {teams.length === 0 ? <div className="empty">尚无团队</div> : null}
      </div>

      {editing ? (
        <TeamEditor
          team={editing === "new" ? null : editing}
          agents={agents}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            teamsAsync.reload();
          }}
          onError={apiError}
        />
      ) : null}
    </div>
  );
}

function TeamEditor({
  team,
  agents,
  onClose,
  onSaved,
  onError,
}: {
  team: TeamRow | null;
  agents: AgentRow[];
  onClose: () => void;
  onSaved: () => void;
  onError: (e: unknown) => void;
}) {
  const isNew = team === null;
  const [slug, setSlug] = useState(team?.slug ?? "");
  const [name, setName] = useState(team?.name ?? "");
  const [description, setDescription] = useState(team?.description ?? "");
  const [members, setMembers] = useState<string[]>(team?.member_agent_ids ?? []);
  const [leadHint, setLeadHint] = useState(team?.lead_hint ?? "");
  const [playbookName, setPlaybookName] = useState(team?.playbook?.name ?? `${team?.name ?? ""}剧本`);
  const [playbook, setPlaybook] = useState(
    team?.playbook ? JSON.stringify(team.playbook.stages, null, 2) : "[]",
  );
  const [busy, setBusy] = useState(false);

  const toggleMember = (id: string) => {
    setMembers((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const save = async () => {
    setBusy(true);
    try {
      let stages: Record<string, unknown>[] = [];
      try {
        stages = JSON.parse(playbook || "[]") as Record<string, unknown>[];
      } catch {
        throw new Error("剧本 stages 不是合法 JSON");
      }
      let teamId: string;
      if (isNew) {
        const created = await teamApi.create({
          slug,
          name,
          description,
          member_agent_ids: members,
          lead_hint: leadHint,
        });
        teamId = created.id;
      } else {
        teamId = team.id;
        await teamApi.update(teamId, {
          name,
          description,
          member_agent_ids: members,
          lead_hint: leadHint,
        });
      }
      if (stages.length > 0 || team?.playbook_id) {
        await teamApi.setPlaybook(teamId, { name: playbookName, trigger_hint: leadHint, stages });
      }
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer title={isNew ? "新建团队" : `编辑 ${team!.name}`} onClose={onClose}>
      <div className="form-row">
        <label className="field">
          <span className="field-label">slug</span>
          <input type="text" value={slug} disabled={!isNew} onChange={(e) => setSlug(e.target.value)} />
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
        <span className="field-label">组长提示（优先选为协调者的暗示）</span>
        <input type="text" value={leadHint} onChange={(e) => setLeadHint(e.target.value)} />
      </label>
      <div className="field">
        <span className="field-label">成员 agent</span>
        <div className="member-picker">
          {agents.map((a) => (
            <label key={a.id} className={`member-tag${members.includes(a.id) ? " on" : ""}`}>
              <input
                type="checkbox"
                checked={members.includes(a.id)}
                onChange={() => toggleMember(a.id)}
              />
              {a.name}
            </label>
          ))}
        </div>
      </div>
      {team?.playbook ? (
        <label className="field">
          <span className="field-label">剧本名称</span>
          <input type="text" value={playbookName} onChange={(e) => setPlaybookName(e.target.value)} />
        </label>
      ) : null}
      <label className="field">
        <span className="field-label">剧本 stages（JSON 数组；完整剧本编辑在第四期开放）</span>
        <textarea className="code" rows={8} value={playbook} onChange={(e) => setPlaybook(e.target.value)} />
      </label>
      <button className="btn-primary" disabled={busy || !name.trim() || (isNew && !slug.trim())} onClick={() => void save()}>
        {busy ? "保存中…" : "保存"}
      </button>
    </Drawer>
  );
}
