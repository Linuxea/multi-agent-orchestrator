/** 端点封装：按资源分组，供页面直接调用。 */

import { api, jsonBody } from "./client";
import type {
  AgentRow,
  AgentVersionRow,
  ApprovalRow,
  ArtifactRow,
  DecisionRow,
  FactoryAgentDraft,
  FactoryTeamDraft,
  MessageInfo,
  ModelCheckResult,
  ModelProfileRow,
  PlatformEvent,
  PlatformSettings,
  RememberScope,
  RunDetail,
  RunListItem,
  SecretRow,
  SessionInfo,
  TaskDetail,
  TeamRow,
  ToolBinding,
  ToolRow,
} from "./types";

export const authApi = {
  status: () => api<{ required: boolean }>("/auth/status"),
  verify: (password: string) =>
    api<{ ok: boolean; required: boolean }>("/auth/verify", jsonBody({ password })),
};

export const sessionApi = {
  list: () => api<SessionInfo[]>("/sessions"),
  create: (title: string, defaultTeamId?: string | null) =>
    api<SessionInfo>("/sessions", jsonBody({ title, default_team_id: defaultTeamId ?? null })),
  patch: (id: string, changes: { title?: string; default_team_id?: string; clear_team?: boolean }) =>
    api<SessionInfo>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(changes) }),
  remove: (id: string) => api<void>(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) => api<MessageInfo[]>(`/sessions/${id}/messages`),
  runs: (id: string) => api<RunListItem[]>(`/sessions/${id}/runs`),
  send: (id: string, content: string) =>
    api<MessageInfo>(`/sessions/${id}/messages`, jsonBody({ content })),
};

export const runApi = {
  get: (id: string) => api<RunDetail>(`/runs/${id}`),
  list: (limit = 50) => api<{ items: RunListItem[] }>(`/runs?limit=${limit}`),
  pause: (id: string) => api<void>(`/runs/${id}/pause`, { method: "POST" }),
  resume: (id: string) => api<void>(`/runs/${id}/resume`, { method: "POST" }),
  cancel: (id: string, reason = "") =>
    api<void>(`/runs/${id}/cancel`, jsonBody({ reason })),
  extendBudget: (
    id: string,
    extra: { max_cost?: number; max_wall_seconds?: number; max_turns?: number },
  ) => api<void>(`/runs/${id}/budget`, jsonBody(extra)),
  plans: (id: string) => api<PlanVersionLite[]>(`/runs/${id}/plans`),
  decisions: (id: string) => api<DecisionRow[]>(`/runs/${id}/decisions`),
  events: (id: string, afterSeq = 0) =>
    api<{ items: PlatformEvent[]; next_cursor: number | null }>(
      `/runs/${id}/events?after_seq=${afterSeq}`,
    ),
  approvePlan: (id: string, version: number) =>
    api<void>(`/runs/${id}/plans/${version}/approve`, jsonBody({})),
  rejectPlan: (id: string, version: number, note: string) =>
    api<void>(`/runs/${id}/plans/${version}/reject`, jsonBody({ note })),
  answer: (id: string, answer: string, questionId: string | null) =>
    api<void>(`/runs/${id}/answers`, jsonBody({ answer, question_id: questionId })),
  uploadArtifact: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api<{ id: string; name: string }>(`/runs/${id}/artifacts`, {
      method: "POST",
      body: form,
    });
  },
};

export type PlanVersionLite = {
  id: string;
  version: number;
  tasks_snapshot: {
    key: string;
    title: string;
    agent_slug: string;
    depends_on: string[];
  }[];
  change_reason: string | null;
  proposed_by: string;
  approved: boolean;
};

export const taskApi = {
  list: (runId: string) => api<unknown[]>(`/runs/${runId}/tasks`),
  get: (id: string) => api<TaskDetail>(`/tasks/${id}`),
  cancel: (id: string) => api<void>(`/tasks/${id}/cancel`, { method: "POST" }),
  skip: (id: string) => api<void>(`/tasks/${id}/skip`, { method: "POST" }),
  accept: (id: string) => api<void>(`/tasks/${id}/accept`, { method: "POST" }),
  extend: (id: string, addAttempts: number) =>
    api<void>(`/tasks/${id}/extend`, jsonBody({ add_attempts: addAttempts })),
  reassign: (id: string, agentId: string) =>
    api<void>(`/tasks/${id}/reassign`, jsonBody({ agent_id: agentId })),
  redispatch: (id: string) => api<void>(`/tasks/${id}/redispatch`, { method: "POST" }),
};

export const attemptApi = {
  get: (id: string) =>
    api<{
      id: string;
      task_id: string;
      number: number;
      agent_id: string;
      status: string;
      brief: Record<string, unknown> | null;
      report: Record<string, unknown> | null;
      review: Record<string, unknown> | null;
      usage: Record<string, number> | null;
      workspace_path: string;
      error: string | null;
    }>(`/attempts/${id}`),
  inject: (id: string, text: string) =>
    api<void>(`/attempts/${id}/input`, jsonBody({ text })),
};

export const approvalApi = {
  list: (status: string) => api<ApprovalRow[]>(`/approvals?status=${status}`),
  decide: (
    id: string,
    approved: boolean,
    note: string,
    remember: RememberScope,
  ) => api<void>(`/approvals/${id}/decide`, jsonBody({ approved, note, remember })),
};

export const agentApi = {
  list: () => api<AgentRow[]>("/agents"),
  get: (id: string) => api<AgentRow>(`/agents/${id}`),
  create: (data: Record<string, unknown>) => api<AgentRow>("/agents", jsonBody(data)),
  update: (id: string, data: Record<string, unknown>) =>
    api<AgentRow>(`/agents/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  remove: (id: string) => api<void>(`/agents/${id}`, { method: "DELETE" }),
  versions: (id: string) => api<AgentVersionRow[]>(`/agents/${id}/versions`),
  rollback: (id: string, version: number) =>
    api<void>(`/agents/${id}/rollback`, jsonBody({ version })),
};

export const toolApi = {
  list: () => api<ToolRow[]>("/tools"),
  create: (data: Record<string, unknown>) => api<ToolRow>("/tools", jsonBody(data)),
  update: (id: string, data: Record<string, unknown>) =>
    api<ToolRow>(`/tools/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  remove: (id: string) => api<void>(`/tools/${id}`, { method: "DELETE" }),
  test: (id: string) => api<Record<string, unknown>>(`/tools/${id}/test`, { method: "POST" }),
  discover: (id: string) =>
    api<{ tools: { name: string; description?: string }[] }>(`/tools/${id}/discover`, {
      method: "POST",
    }),
  bindings: (agentId: string) => api<ToolBinding[]>(`/agents/${agentId}/tool-bindings`),
  putBinding: (
    agentId: string,
    data: { tool_id: string; permission: string | null; enabled_subtools: string[] | null },
  ) => api<ToolBinding>(`/agents/${agentId}/tool-bindings`, jsonBody(data)),
  removeBinding: (agentId: string, toolId: string) =>
    api<void>(`/agents/${agentId}/tool-bindings/${toolId}`, { method: "DELETE" }),
};

export const modelApi = {
  list: () => api<ModelProfileRow[]>("/model-profiles"),
  create: (data: Record<string, unknown>) =>
    api<ModelProfileRow>("/model-profiles", jsonBody(data)),
  update: (id: string, data: Record<string, unknown>) =>
    api<ModelProfileRow>(`/model-profiles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),
  remove: (id: string) => api<void>(`/model-profiles/${id}`, { method: "DELETE" }),
  check: (id: string) => api<ModelCheckResult>(`/model-profiles/${id}/check`, { method: "POST" }),
};

export const secretApi = {
  list: () => api<SecretRow[]>("/secrets"),
  create: (name: string, value: string, description: string) =>
    api<{ id: string; name: string }>("/secrets", jsonBody({ name, value, description })),
  update: (id: string, value: string, description: string) =>
    api<{ id: string }>(`/secrets/${id}`, { method: "PUT", body: JSON.stringify({ name: "", value, description }) }),
  remove: (id: string) => api<void>(`/secrets/${id}`, { method: "DELETE" }),
};

export const teamApi = {
  list: () => api<TeamRow[]>("/teams"),
  create: (data: Record<string, unknown>) => api<TeamRow>("/teams", jsonBody(data)),
  update: (id: string, data: Record<string, unknown>) =>
    api<TeamRow>(`/teams/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  remove: (id: string) => api<void>(`/teams/${id}`, { method: "DELETE" }),
  setPlaybook: (
    id: string,
    data: { name: string; trigger_hint: string; stages: Record<string, unknown>[] },
  ) => api<{ id: string; name: string }>(`/teams/${id}/playbook`, jsonBody(data)),
  removePlaybook: (id: string) =>
    api<void>(`/teams/${id}/playbook`, { method: "DELETE" }),
};

export const factoryApi = {
  draftAgent: (requirement: string, executor: string, modelProfileId?: string | null) =>
    api<FactoryAgentDraft>(
      "/factory/agents",
      jsonBody({ requirement, executor, model_profile_id: modelProfileId ?? null }),
    ),
  draftTeam: (requirement: string) =>
    api<FactoryTeamDraft>("/factory/teams", jsonBody({ requirement })),
};

export const artifactApi = {
  get: (id: string) => api<ArtifactRow>(`/artifacts/${id}`),
  listByRun: (runId: string) => api<ArtifactRow[]>(`/runs/${runId}/artifacts`),
  contentUrl: (id: string) => `/api/v1/artifacts/${id}/content`,
};

export const settingsApi = {
  get: () => api<PlatformSettings>("/settings"),
  patch: (changes: Record<string, unknown>) =>
    api<PlatformSettings>("/settings", jsonBody({ changes })),
};
