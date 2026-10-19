/** 团队：成员组合 + 结构化剧本（playbook）编辑 + 工厂编队（第四期）。 */

import { useState } from "react";
import { agentApi, factoryApi, teamApi } from "../api/endpoints";
import type { AgentRow, FactoryTeamDraft, PlaybookStage, TeamRow } from "../api/types";
import { Drawer } from "../components/Drawer";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

const EMPTY_STAGE: PlaybookStage = { key: "", name: "", goal: "", agent_hint: "", depends_on: [] };

export function TeamsPage() {
  const { apiError, toast } = useApp();
  const teamsAsync = useAsync(() => teamApi.list(), []);
  const agentsAsync = useAsync(() => agentApi.list(), []);
  const teams = teamsAsync.data ?? [];
  const agents = agentsAsync.data ?? [];
  const [editing, setEditing] = useState<TeamRow | "new" | null>(null);
  const [factoryDraft, setFactoryDraft] = useState<FactoryTeamDraft | null>(null);
  const [factoryOpen, setFactoryOpen] = useState(false);

  const agentName = (id: string) => agents.find((a) => a.id === id)?.name ?? id.slice(-6);

  return (
    <div className="page">
      <div className="page-head">
        <h1>团队</h1>
        <span className="page-sub">成员组合 + 剧本骨架；会话绑定编队后协调者优先按剧本推进</span>
        <button
          className="btn-sm"
          style={{ marginLeft: "auto" }}
          disabled={agents.length === 0}
          onClick={() => setFactoryOpen(true)}
          title="描述目标，模型从现有 agent 中挑选成员并起草剧本"
        >
          ⚙ 工厂编队
        </button>
        <button className="btn-primary btn-sm" onClick={() => setEditing("new")}>
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
              <th></th>
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
                <td>
                  {t.playbook ? (
                    <>
                      {t.playbook.name}
                      <span className="mono stage-tab-count">
                        {" "}
                        {t.playbook.stages.length} 阶段
                      </span>
                    </>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="st-rationale">{t.lead_hint || "—"}</td>
                <td>
                  <button
                    className="btn-ghost btn-sm"
                    onClick={async (e) => {
                      e.stopPropagation();
                      try {
                        await teamApi.remove(t.id);
                        toast(`已删除团队 ${t.name}`);
                        teamsAsync.reload();
                      } catch (err) {
                        apiError(err);
                      }
                    }}
                  >
                    删除
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {teams.length === 0 ? <div className="empty">尚无团队</div> : null}
      </div>

      {factoryOpen ? (
        <FactoryTeamDrawer
          onClose={() => setFactoryOpen(false)}
          onDrafted={(draft) => {
            setFactoryOpen(false);
            setFactoryDraft(draft);
            setEditing("new");
          }}
          onError={apiError}
        />
      ) : null}

      {editing ? (
        <TeamEditor
          team={editing === "new" ? null : editing}
          agents={agents}
          factoryDraft={editing === "new" ? factoryDraft : null}
          onClose={() => {
            setEditing(null);
            setFactoryDraft(null);
          }}
          onSaved={() => {
            setEditing(null);
            setFactoryDraft(null);
            teamsAsync.reload();
          }}
          onError={apiError}
        />
      ) : null}
    </div>
  );
}

function FactoryTeamDrawer({
  onClose,
  onDrafted,
  onError,
}: {
  onClose: () => void;
  onDrafted: (draft: FactoryTeamDraft) => void;
  onError: (e: unknown) => void;
}) {
  const [requirement, setRequirement] = useState("");
  const [busy, setBusy] = useState(false);

  const draft = async () => {
    setBusy(true);
    try {
      onDrafted(await factoryApi.draftTeam(requirement.trim()));
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer title="工厂编队" onClose={onClose}>
      <p className="page-sub" style={{ marginTop: 0 }}>
        描述团队目标，模型从现有 agent 花名册中挑选成员并起草剧本阶段；
        覆盖不了的能力会列入缺口提示。生成后进入编辑器审阅再保存。
      </p>
      <label className="field">
        <span className="field-label">团队目标</span>
        <textarea
          rows={4}
          placeholder="例如：组一个能做竞品分析并输出周报的小队"
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
        />
      </label>
      <button
        className="btn-primary"
        disabled={busy || requirement.trim().length < 2}
        onClick={() => void draft()}
      >
        {busy ? "编队中…" : "生成编队草稿"}
      </button>
    </Drawer>
  );
}

function TeamEditor({
  team,
  agents,
  factoryDraft,
  onClose,
  onSaved,
  onError,
}: {
  team: TeamRow | null;
  agents: AgentRow[];
  factoryDraft: FactoryTeamDraft | null;
  onClose: () => void;
  onSaved: () => void;
  onError: (e: unknown) => void;
}) {
  const isNew = team === null;
  const [slug, setSlug] = useState(team?.slug ?? factoryDraft?.slug ?? "");
  const [name, setName] = useState(team?.name ?? factoryDraft?.name ?? "");
  const [description, setDescription] = useState(
    team?.description ?? factoryDraft?.description ?? "",
  );
  const [members, setMembers] = useState<string[]>(
    team?.member_agent_ids ?? factoryDraft?.member_agent_ids ?? [],
  );
  const [leadHint, setLeadHint] = useState(team?.lead_hint ?? factoryDraft?.lead_hint ?? "");
  const [playbookName, setPlaybookName] = useState(
    team?.playbook?.name ?? factoryDraft?.playbook.name ?? `${name || "团队"}剧本`,
  );
  const [stages, setStages] = useState<PlaybookStage[]>(
    team?.playbook?.stages ?? factoryDraft?.playbook.stages ?? [],
  );
  const [rawMode, setRawMode] = useState(false);
  const [raw, setRaw] = useState("");
  const [busy, setBusy] = useState(false);

  const toggleMember = (id: string) => {
    setMembers((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const setStage = (i: number, patch: Partial<PlaybookStage>) => {
    setStages((prev) => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));
  };
  const addStage = () => setStages((prev) => [...prev, { ...EMPTY_STAGE }]);
  const removeStage = (i: number) => setStages((prev) => prev.filter((_, idx) => idx !== i));
  const moveStage = (i: number, dir: -1 | 1) => {
    setStages((prev) => {
      const j = i + dir;
      if (j < 0 || j >= prev.length) return prev;
      const a = prev[i];
      const b = prev[j];
      if (!a || !b) return prev;
      const next = [...prev];
      next[i] = b;
      next[j] = a;
      return next;
    });
  };

  const validate = (): string | null => {
    if (!name.trim() || (isNew && !slug.trim())) return "请填写名称与 slug";
    const keys = stages.map((s) => s.key.trim());
    const emptyIdx = keys.findIndex((k) => !k);
    if (emptyIdx >= 0) return `第 ${emptyIdx + 1} 个剧本阶段缺少 key`;
    if (new Set(keys).size !== keys.length) return "剧本阶段 key 重复";
    for (const s of stages) {
      for (const d of s.depends_on ?? []) {
        if (!keys.includes(d)) return `阶段 ${s.key} 依赖了不存在的阶段：${d}`;
      }
    }
    return null;
  };

  const save = async () => {
    const problem = validate();
    if (problem) {
      onError(new Error(problem));
      return;
    }
    setBusy(true);
    try {
      const normalized = stages.map((s) => ({
        key: s.key.trim(),
        name: s.name.trim() || s.key.trim(),
        goal: s.goal ?? "",
        agent_hint: s.agent_hint ?? "",
        depends_on: s.depends_on ?? [],
      }));
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
      if (normalized.length > 0 || team?.playbook_id) {
        await teamApi.setPlaybook(teamId, {
          name: playbookName,
          trigger_hint: leadHint,
          stages: normalized,
        });
      }
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  const toggleRaw = () => {
    if (!rawMode) setRaw(JSON.stringify(stages, null, 2));
    else {
      try {
        const parsed = JSON.parse(raw || "[]") as PlaybookStage[];
        if (!Array.isArray(parsed)) throw new Error("not array");
        setStages(parsed);
      } catch {
        onError(new Error("剧本 JSON 不合法（应为阶段数组）"));
        return;
      }
    }
    setRawMode(!rawMode);
  };

  return (
    <Drawer title={isNew ? "新建团队" : `编辑 ${team!.name}`} onClose={onClose}>
      {factoryDraft ? (
        <div className="factory-note">
          工厂草稿：成员 {factoryDraft.member_slugs.join("、") || "无"}
          {factoryDraft.dropped_member_slugs.length > 0
            ? `（已剔除花名册外：${factoryDraft.dropped_member_slugs.join("、")}）`
            : ""}
          {factoryDraft.missing_capabilities.length > 0
            ? `；能力缺口：${factoryDraft.missing_capabilities.join("、")}`
            : ""}
        </div>
      ) : null}
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
              {a.avatar ? `${a.avatar} ` : ""}
              {a.name}
            </label>
          ))}
        </div>
      </div>

      <div className="field">
        <div className="reassign-row" style={{ marginBottom: 8 }}>
          <span className="field-label" style={{ margin: 0 }}>
            剧本阶段（会话绑定本团队后，协调者按此骨架制定计划）
          </span>
          <span style={{ marginLeft: "auto", display: "inline-flex", gap: 8 }}>
            <button className="btn-ghost btn-sm" onClick={toggleRaw}>
              {rawMode ? "结构化编辑" : "JSON"}
            </button>
            {!rawMode ? (
              <button className="btn-sm" onClick={addStage}>
                + 阶段
              </button>
            ) : null}
          </span>
        </div>
        {!rawMode ? (
          <div className="stage-editor">
            {stages.length === 0 ? (
              <div className="empty">无阶段（团队仅作为成员集合时可不配剧本）</div>
            ) : null}
            {stages.map((s, i) => (
              <div className="stage-row" key={i}>
                <div className="form-row">
                  <label className="field">
                    <span className="field-label">key</span>
                    <input
                      type="text"
                      className="mono"
                      value={s.key}
                      placeholder="S1"
                      onChange={(e) => setStage(i, { key: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field-label">阶段名</span>
                    <input
                      type="text"
                      value={s.name}
                      onChange={(e) => setStage(i, { name: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field-label">适合（slug / 标签）</span>
                    <input
                      type="text"
                      className="mono"
                      value={s.agent_hint ?? ""}
                      placeholder="web-researcher"
                      onChange={(e) => setStage(i, { agent_hint: e.target.value })}
                    />
                  </label>
                </div>
                <div className="form-row">
                  <label className="field" style={{ flex: 2 }}>
                    <span className="field-label">目标</span>
                    <input
                      type="text"
                      value={s.goal ?? ""}
                      onChange={(e) => setStage(i, { goal: e.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field-label">依赖阶段（逗号分隔）</span>
                    <input
                      type="text"
                      className="mono"
                      value={(s.depends_on ?? []).join(",")}
                      placeholder="如 S1"
                      onChange={(e) =>
                        setStage(i, {
                          depends_on: e.target.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
                        })
                      }
                    />
                  </label>
                </div>
                <div className="stage-row-actions">
                  <button className="btn-ghost btn-sm" onClick={() => moveStage(i, -1)} disabled={i === 0}>
                    ↑
                  </button>
                  <button
                    className="btn-ghost btn-sm"
                    onClick={() => moveStage(i, 1)}
                    disabled={i === stages.length - 1}
                  >
                    ↓
                  </button>
                  <button className="btn-ghost btn-sm" onClick={() => removeStage(i)}>
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <textarea className="code" rows={10} value={raw} onChange={(e) => setRaw(e.target.value)} />
        )}
      </div>
      {stages.length > 0 || team?.playbook_id ? (
        <label className="field">
          <span className="field-label">剧本名称</span>
          <input type="text" value={playbookName} onChange={(e) => setPlaybookName(e.target.value)} />
        </label>
      ) : null}
      {team?.playbook_id ? (
        <button
          className="btn-ghost btn-sm"
          style={{ marginBottom: 12 }}
          onClick={async () => {
            try {
              await teamApi.removePlaybook(team.id);
              onSaved();
            } catch (e) {
              onError(e);
            }
          }}
        >
          删除剧本
        </button>
      ) : null}
      <button className="btn-primary" disabled={busy} onClick={() => void save()}>
        {busy ? "保存中…" : "保存"}
      </button>
    </Drawer>
  );
}
