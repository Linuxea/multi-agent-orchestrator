/** Agent 注册表：列表 + 创建/编辑抽屉 + 工厂起草 + 版本回滚 + 工具绑定。 */

import { useEffect, useState } from "react";
import { agentApi, factoryApi, modelApi, toolApi } from "../api/endpoints";
import type { AgentRow, AgentVersionRow, FactoryAgentDraft, ModelProfileRow, ToolBinding, ToolRow } from "../api/types";
import { Drawer } from "../components/Drawer";
import { StatusChip } from "../components/StatusChip";
import { useApp } from "../state/app";
import { useAsync } from "../state/useAsync";

const ORIGIN_LABEL: Record<string, string> = {
  preset: "预置",
  manual: "手动",
  factory: "工厂",
};

export function AgentsPage() {
  const { apiError } = useApp();
  const agentsAsync = useAsync(() => agentApi.list(), []);
  const modelsAsync = useAsync(() => modelApi.list(), []);
  const [editing, setEditing] = useState<AgentRow | "new" | null>(null);
  const [prefill, setPrefill] = useState<FactoryAgentDraft | null>(null);
  const [factoryOpen, setFactoryOpen] = useState(false);

  const agents = agentsAsync.data ?? [];
  const models = modelsAsync.data ?? [];
  const modelName = (id: string) =>
    models.find((m) => m.id === id)?.name ?? id.slice(-6);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Agent 注册表</h1>
        <span className="page-sub">{agents.length} 个 agent · 修改会生成新版本快照</span>
        <button
          className="btn-sm"
          style={{ marginLeft: "auto" }}
          disabled={models.length === 0}
          onClick={() => setFactoryOpen(true)}
          title="描述需求，由模型起草完整 agent 定义，审阅后保存"
        >
          ⚙ 工厂起草
        </button>
        <button
          className="btn-primary btn-sm"
          disabled={models.length === 0}
          onClick={() => {
            setPrefill(null);
            setEditing("new");
          }}
        >
          + 新建 agent
        </button>
      </div>

      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>名称</th>
              <th>slug</th>
              <th>执行器</th>
              <th>模型</th>
              <th>并发</th>
              <th>状态</th>
              <th>版本</th>
              <th>来源</th>
            </tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr key={a.id} className="clickable" onClick={() => setEditing(a)}>
                <td>
                  {a.avatar ? <span aria-hidden>{a.avatar} </span> : null}
                  {a.name}
                </td>
                <td className="mono">{a.slug}</td>
                <td className="mono">{a.executor}</td>
                <td>{modelName(a.model_profile_id)}</td>
                <td className="mono">{a.max_concurrency}</td>
                <td>
                  <StatusChip kind="tool" status={AGENT_STATUS_MAP[a.status] ?? "untested"} />
                </td>
                <td className="mono">v{a.version}</td>
                <td>
                  <span className={`chip${a.origin === "factory" ? " chip-dusk" : " chip-ash"}`}>
                    {ORIGIN_LABEL[a.origin] ?? a.origin}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {agents.length === 0 ? <div className="empty">尚无 agent</div> : null}
      </div>

      {factoryOpen ? (
        <FactoryAgentDrawer
          models={models}
          onClose={() => setFactoryOpen(false)}
          onDrafted={(draft) => {
            setFactoryOpen(false);
            setPrefill(draft);
            setEditing("new");
          }}
          onError={apiError}
        />
      ) : null}

      {editing ? (
        <AgentEditor
          agent={editing === "new" ? null : editing}
          prefill={editing === "new" ? prefill : null}
          models={models}
          onClose={() => {
            setEditing(null);
            setPrefill(null);
          }}
          onSaved={() => {
            setEditing(null);
            setPrefill(null);
            agentsAsync.reload();
          }}
          onError={apiError}
        />
      ) : null}
    </div>
  );
}

function FactoryAgentDrawer({
  models,
  onClose,
  onDrafted,
  onError,
}: {
  models: ModelProfileRow[];
  onClose: () => void;
  onDrafted: (draft: FactoryAgentDraft) => void;
  onError: (e: unknown) => void;
}) {
  const [requirement, setRequirement] = useState("");
  const [executor, setExecutor] = useState("lite");
  const [modelProfileId, setModelProfileId] = useState("");
  const [busy, setBusy] = useState(false);

  const draft = async () => {
    setBusy(true);
    try {
      const result = await factoryApi.draftAgent(
        requirement.trim(),
        executor,
        modelProfileId || null,
      );
      onDrafted(result);
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer title="工厂起草 agent" onClose={onClose}>
      <p className="page-sub" style={{ marginTop: 0 }}>
        描述你需要的能力，模型起草完整定义（提示词 / 职责 / 标签 / 预算），生成后进入编辑器审阅再保存。
      </p>
      <label className="field">
        <span className="field-label">需求描述</span>
        <textarea
          rows={4}
          placeholder="例如：需要一个能检索行业报告并做摘要的调研 agent，重点是来源可靠"
          value={requirement}
          onChange={(e) => setRequirement(e.target.value)}
        />
      </label>
      <div className="form-row">
        <label className="field">
          <span className="field-label">执行器</span>
          <select value={executor} onChange={(e) => setExecutor(e.target.value)}>
            <option value="lite">lite（内置循环）</option>
            <option value="sdk">sdk（Claude Code 运行时）</option>
          </select>
        </label>
        <label className="field">
          <span className="field-label">起草模型（默认用编排模型）</span>
          <select value={modelProfileId} onChange={(e) => setModelProfileId(e.target.value)}>
            <option value="">默认（编排模型）</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}（{m.model}）
              </option>
            ))}
          </select>
        </label>
      </div>
      <button
        className="btn-primary"
        disabled={busy || requirement.trim().length < 2}
        onClick={() => void draft()}
      >
        {busy ? "起草中…" : "生成草稿"}
      </button>
    </Drawer>
  );
}

const AGENT_STATUS_MAP: Record<string, string> = {
  draft: "untested",
  active: "ok",
  disabled: "disabled",
  archived: "disabled",
};

function AgentEditor({
  agent,
  prefill,
  models,
  onClose,
  onSaved,
  onError,
}: {
  agent: AgentRow | null;
  prefill: FactoryAgentDraft | null;
  models: ModelProfileRow[];
  onClose: () => void;
  onSaved: () => void;
  onError: (e: unknown) => void;
}) {
  const isNew = agent === null;
  const [slug, setSlug] = useState(agent?.slug ?? prefill?.slug ?? "");
  const [name, setName] = useState(agent?.name ?? prefill?.name ?? "");
  const [description, setDescription] = useState(agent?.description ?? prefill?.description ?? "");
  const [roleDetail, setRoleDetail] = useState(agent?.role_detail ?? prefill?.role_detail ?? "");
  const [tags, setTags] = useState(
    (agent?.capability_tags ?? prefill?.capability_tags ?? []).join(", "),
  );
  const [systemPrompt, setSystemPrompt] = useState(agent?.system_prompt ?? prefill?.system_prompt ?? "");
  const [executor, setExecutor] = useState<string>(agent?.executor ?? prefill?.executor ?? "lite");
  const [modelProfileId, setModelProfileId] = useState(
    agent?.model_profile_id ?? models[0]?.id ?? "",
  );
  const [maxConcurrency, setMaxConcurrency] = useState(agent?.max_concurrency ?? 2);
  const [status, setStatus] = useState<string>(agent?.status ?? "draft");
  const [budget, setBudget] = useState(
    JSON.stringify(
      agent?.default_budget ?? prefill?.default_budget ?? {},
      null,
      2,
    ),
  );
  const [changeNote, setChangeNote] = useState(prefill ? "工厂起草初版" : "");
  const [inputContract, setInputContract] = useState(
    agent?.input_contract ?? prefill?.input_contract ?? "",
  );
  const [outputContract, setOutputContract] = useState(
    agent?.output_contract ?? prefill?.output_contract ?? "",
  );
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"edit" | "versions" | "bindings">("edit");
  const [versions, setVersions] = useState<AgentVersionRow[]>([]);
  const [bindings, setBindings] = useState<ToolBinding[]>([]);
  const [tools, setTools] = useState<ToolRow[]>([]);

  const loadSide = async () => {
    if (!agent) return;
    try {
      setVersions(await agentApi.versions(agent.id));
      setBindings(await toolApi.bindings(agent.id));
      setTools(await toolApi.list());
    } catch (e) {
      onError(e);
    }
  };
  useEffect(() => {
    if (agent && tab !== "edit") void loadSide();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent?.id, tab]);

  const save = async () => {
    setBusy(true);
    try {
      let budgetObj: Record<string, unknown> | null = null;
      try {
        budgetObj = JSON.parse(budget || "null") as Record<string, unknown> | null;
      } catch {
        throw new Error("默认预算不是合法 JSON");
      }
      const tagsList = tags.split(/[,,\s]+/).filter(Boolean);
      if (isNew) {
        await agentApi.create({
          slug,
          name,
          description,
          role_detail: roleDetail,
          capability_tags: tagsList,
          system_prompt: systemPrompt,
          executor,
          model_profile_id: modelProfileId,
          max_concurrency: Number(maxConcurrency),
          status,
          default_budget: budgetObj,
          input_contract: inputContract,
          output_contract: outputContract,
          ...(prefill ? { origin: "factory", avatar: prefill.avatar } : {}),
        });
      } else {
        await agentApi.update(agent.id, {
          version: agent.version,
          change_note: changeNote,
          name,
          description,
          role_detail: roleDetail,
          capability_tags: tagsList,
          system_prompt: systemPrompt,
          executor,
          model_profile_id: modelProfileId,
          max_concurrency: Number(maxConcurrency),
          status,
          default_budget: budgetObj,
          input_contract: inputContract,
          output_contract: outputContract,
        });
      }
      onSaved();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      title={isNew ? "新建 agent" : `编辑 ${agent!.name}`}
      onClose={onClose}
    >
      {agent ? (
        <div className="stage-tabs" style={{ marginBottom: 16 }}>
          {(
            [
              ["edit", "资料"],
              ["versions", `版本 (v${agent.version})`],
              ["bindings", "工具绑定"],
            ] as const
          ).map(([t, label]) => (
            <button key={t} className={`stage-tab${tab === t ? " on" : ""}`} onClick={() => setTab(t)}>
              {label}
            </button>
          ))}
        </div>
      ) : null}

      {tab === "edit" ? (
        <>
          <div className="form-row">
            <label className="field">
              <span className="field-label">slug（唯一标识）</span>
              <input type="text" value={slug} disabled={!isNew} onChange={(e) => setSlug(e.target.value)} />
            </label>
            <label className="field">
              <span className="field-label">名称</span>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
            </label>
          </div>
          <label className="field">
            <span className="field-label">一句话描述</span>
            <input type="text" value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">职责细节</span>
            <textarea rows={2} value={roleDetail} onChange={(e) => setRoleDetail(e.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">能力标签（逗号分隔）</span>
              <input type="text" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="research, writing" />
          </label>
          <label className="field">
            <span className="field-label">系统提示词</span>
            <textarea
              className="code"
              rows={10}
              value={systemPrompt}
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
          </label>
          <div className="form-row">
            <label className="field">
              <span className="field-label">输入约定</span>
              <input
                type="text"
                value={inputContract}
                onChange={(e) => setInputContract(e.target.value)}
                placeholder="期望的任务单输入"
              />
            </label>
            <label className="field">
              <span className="field-label">交付约定</span>
              <input
                type="text"
                value={outputContract}
                onChange={(e) => setOutputContract(e.target.value)}
                placeholder="期望的交付物形态"
              />
            </label>
          </div>
          <div className="form-row">
            <label className="field">
              <span className="field-label">执行器</span>
              <select value={executor} onChange={(e) => setExecutor(e.target.value)}>
                <option value="lite">lite（内置循环）</option>
                <option value="sdk">sdk（Claude Code 运行时）</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">模型配置</span>
              <select value={modelProfileId} onChange={(e) => setModelProfileId(e.target.value)}>
                {models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name}（{m.model}）
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="form-row">
            <label className="field">
              <span className="field-label">单 agent 并发上限</span>
              <input
                type="number"
                min={1}
                value={maxConcurrency}
                onChange={(e) => setMaxConcurrency(Number(e.target.value))}
              />
            </label>
            <label className="field">
              <span className="field-label">状态</span>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="draft">draft（草稿）</option>
                <option value="active">active（启用）</option>
                <option value="disabled">disabled（停用）</option>
                <option value="archived">archived（归档）</option>
              </select>
            </label>
          </div>
          <label className="field">
            <span className="field-label">默认预算（JSON：max_cost / max_turns / max_wall_seconds）</span>
            <textarea className="code" rows={4} value={budget} onChange={(e) => setBudget(e.target.value)} />
          </label>
          {!isNew ? (
            <label className="field">
              <span className="field-label">变更说明（记入版本历史）</span>
              <input type="text" value={changeNote} onChange={(e) => setChangeNote(e.target.value)} />
            </label>
          ) : null}
          <button
            className="btn-primary"
            disabled={busy || !name.trim() || (isNew && !slug.trim()) || !modelProfileId}
            onClick={() => void save()}
          >
            {busy ? "保存中…" : isNew ? "创建" : "保存（生成新版本）"}
          </button>
        </>
      ) : null}

      {tab === "versions" && agent ? (
        <div>
          {versions.map((v) => (
            <div className="ver-row" key={v.version}>
              <span className="mono">v{v.version}</span>
              <span className="ver-note">{v.change_note || "（无说明）"}</span>
              <span className="mono ver-time">{new Date(v.created_at).toLocaleString("zh-CN", { hour12: false })}</span>
              <button
                className="btn-sm"
                disabled={v.version === agent.version}
                onClick={async () => {
                  try {
                    await agentApi.rollback(agent.id, v.version);
                    onSaved();
                  } catch (e) {
                    onError(e);
                  }
                }}
              >
                {v.version === agent.version ? "当前" : "回滚到此版"}
              </button>
            </div>
          ))}
          {versions.length === 0 ? <div className="empty">加载中…</div> : null}
        </div>
      ) : null}

      {tab === "bindings" && agent ? (
        <BindingsEditor
          agentId={agent.id}
          bindings={bindings}
          tools={tools}
          onChanged={loadSide}
          onError={onError}
        />
      ) : null}
    </Drawer>
  );
}

function BindingsEditor({
  agentId,
  bindings,
  tools,
  onChanged,
  onError,
}: {
  agentId: string;
  bindings: ToolBinding[];
  tools: ToolRow[];
  onChanged: () => void;
  onError: (e: unknown) => void;
}) {
  const unbound = tools.filter((t) => !bindings.some((b) => b.tool_id === t.id) && t.kind !== "builtin");
  const [addTool, setAddTool] = useState("");

  const setPerm = async (toolId: string, permission: string | null) => {
    try {
      const b = bindings.find((x) => x.tool_id === toolId);
      await toolApi.putBinding(agentId, {
        tool_id: toolId,
        permission,
        enabled_subtools: b?.enabled_subtools ?? null,
      });
      onChanged();
    } catch (e) {
      onError(e);
    }
  };

  return (
    <div>
      <p className="page-sub" style={{ marginTop: 0 }}>
        绑定权限覆盖工具默认权限（§8.9）；builtin 工具用默认权限即可，无需绑定。
      </p>
      <table className="data">
        <thead>
          <tr>
            <th>工具</th>
            <th>类型</th>
            <th>权限</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {bindings.map((b) => (
            <tr key={b.tool_id}>
              <td>
                {b.tool_name} <span className="mono st-taskkey">{b.tool_slug}</span>
              </td>
              <td className="mono">{b.kind}</td>
              <td>
                <select
                  value={b.permission ?? ""}
                  onChange={(e) => void setPerm(b.tool_id, e.target.value || null)}
                  style={{ width: 120 }}
                >
                  <option value="">工具默认</option>
                  <option value="allow">allow</option>
                  <option value="ask">ask</option>
                  <option value="deny">deny</option>
                </select>
              </td>
              <td>
                <button
                  className="btn-ghost btn-sm"
                  onClick={async () => {
                    try {
                      await toolApi.removeBinding(agentId, b.tool_id);
                      onChanged();
                    } catch (e) {
                      onError(e);
                    }
                  }}
                >
                  解绑
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {bindings.length === 0 ? <div className="empty">尚未绑定任何工具</div> : null}

      <div className="reassign-row" style={{ marginTop: 16 }}>
        <select value={addTool} onChange={(e) => setAddTool(e.target.value)}>
          <option value="">选择要绑定的工具…</option>
          {unbound.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}（{t.slug}）
            </option>
          ))}
        </select>
        <button
          className="btn-sm"
          disabled={!addTool}
          onClick={async () => {
            try {
              await toolApi.putBinding(agentId, {
                tool_id: addTool,
                permission: null,
                enabled_subtools: null,
              });
              setAddTool("");
              onChanged();
            } catch (e) {
              onError(e);
            }
          }}
        >
          绑定
        </button>
      </div>
    </div>
  );
}
