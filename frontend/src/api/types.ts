/** 后端 API 实体类型（与 app/api/routes 响应一一对应）。 */

export type RunStatus =
  | "created"
  | "planning"
  | "clarifying"
  | "awaiting_plan_approval"
  | "running"
  | "paused"
  | "awaiting_user"
  | "synthesizing"
  | "completed"
  | "failed"
  | "cancelled";

export type TaskStatus =
  | "pending"
  | "ready"
  | "queued"
  | "running"
  | "awaiting_approval"
  | "reported"
  | "needs_rework"
  | "blocked"
  | "awaiting_user"
  | "accepted"
  | "failed"
  | "skipped"
  | "superseded"
  | "cancelled";

export type AttemptStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  | "reported"
  | "timed_out"
  | "crashed"
  | "cancelled";

export type Outcome = "success" | "partial" | "failed" | "cancelled";
export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";
export type RememberScope = "once" | "this_run" | "always_for_agent";
export type AgentStatus = "draft" | "active" | "disabled" | "archived";
export type ToolStatus = "untested" | "ok" | "error" | "disabled";
export type ToolPermission = "allow" | "ask" | "deny";

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cost: number;
  wall_seconds?: number;
  turns?: number;
}

export interface Budget {
  max_cost?: number | null;
  max_wall_seconds?: number | null;
  max_turns?: number | null;
}

export interface SessionInfo {
  id: string;
  title: string;
  status: string;
  default_team_id: string | null;
  created_at: string;
}

export interface MessageInfo {
  id: string;
  session_id: string;
  role: "user" | "orchestrator";
  content: string;
  run_id: string | null;
  created_at: string;
}

export interface RunTaskBrief {
  id: string;
  key: string;
  title: string;
  status: TaskStatus;
  attempt_count: number;
  result_summary: string | null;
}

export interface RunDetail {
  id: string;
  session_id: string;
  status: RunStatus;
  current_plan_version: number | null;
  budget: Budget | null;
  usage: Partial<Usage> | null;
  final_answer: string | null;
  outcome: Outcome | null;
  summary_of_gaps: string | null;
  tasks: RunTaskBrief[];
  created_at: string;
}

export interface RunListItem {
  id: string;
  session_id: string;
  status: RunStatus;
  outcome: Outcome | null;
  created_at: string;
}

export interface TaskAttempt {
  id: string;
  number: number;
  status: AttemptStatus;
  report: Record<string, unknown> | null;
  review: Record<string, unknown> | null;
  brief: Record<string, unknown> | null;
  error: string | null;
}

export interface TaskDetail {
  id: string;
  run_id: string;
  key: string;
  title: string;
  goal: string;
  status: TaskStatus;
  depends_on: string[];
  attempt_count: number;
  max_attempts: number;
  result_summary: string | null;
  attempts: TaskAttempt[];
}

export interface PlanVersion {
  id: string;
  version: number;
  tasks_snapshot: PlanTaskSnapshot[];
  change_reason: string | null;
  proposed_by: string;
  approved: boolean;
}

export interface PlanTaskSnapshot {
  key: string;
  title: string;
  goal?: string;
  agent_slug: string;
  depends_on: string[];
  priority?: number;
  optional?: boolean;
  budget?: Budget | null;
}

export interface DecisionRow {
  id: string;
  type: string;
  summary: string;
  rationale: string | null;
  made_by: "orchestrator" | "user" | "rule";
  task_id: string | null;
  created_at: string;
}

export interface PlatformEvent {
  id: string;
  seq: number;
  type: string;
  actor: string;
  task_key: string | null;
  attempt_id: string | null;
  payload: Record<string, unknown>;
  ts: string;
}

export interface ApprovalRow {
  id: string;
  run_id: string;
  attempt_id: string | null;
  requested_by: string;
  tool_slug: string;
  action: string;
  arguments: Record<string, unknown>;
  reason: string;
  risk_note: string;
  status: ApprovalStatus;
}

export interface AgentRow {
  id: string;
  slug: string;
  name: string;
  description: string;
  role_detail: string;
  capability_tags: string[];
  system_prompt: string;
  executor: "sdk" | "lite";
  model_profile_id: string;
  input_contract: string;
  output_contract: string;
  default_budget: Budget | null;
  max_concurrency: number;
  status: AgentStatus;
  version: number;
  origin: string;
  avatar: string | null;
  created_at: string;
}

export interface AgentVersionRow {
  version: number;
  snapshot: Record<string, unknown>;
  change_note: string;
  created_at: string;
}

export interface ToolRow {
  id: string;
  kind: string;
  slug: string;
  name: string;
  description: string;
  config: Record<string, unknown>;
  default_permission: ToolPermission;
  status: ToolStatus;
  last_test: Record<string, unknown> | null;
  discovered: unknown[] | null;
  tags: string[];
}

export interface ToolBinding {
  tool_id: string;
  tool_slug: string;
  tool_name: string;
  kind: string;
  permission: ToolPermission | null;
  enabled_subtools: string[] | null;
}

export interface ModelProfileRow {
  id: string;
  name: string;
  protocol: string;
  base_url: string;
  api_key_secret_id: string | null;
  model: string;
  max_output_tokens: number;
  temperature: number | null;
  thinking: string;
  context_window: number;
  price_input: number;
  price_output: number;
  is_default_orchestrator: boolean;
  is_default_worker: boolean;
}

export interface ModelCheckResult {
  ok: boolean;
  base_url: string;
  model: string;
  checks: { name: string; passed: boolean; detail: string; latency_ms: number }[];
}

export interface SecretRow {
  id: string;
  name: string;
  description: string;
  created_at: string;
  last_used_at: string | null;
}

export interface PlaybookStage {
  key: string;
  name: string;
  goal?: string;
  agent_hint?: string;
  depends_on?: string[];
}

export interface TeamRow {
  id: string;
  slug: string;
  name: string;
  description: string;
  member_agent_ids: string[];
  playbook_id: string | null;
  lead_hint: string;
  playbook: {
    id: string;
    name: string;
    trigger_hint: string;
    stages: PlaybookStage[];
  } | null;
}

export interface FactoryAgentDraft {
  slug: string;
  name: string;
  description: string;
  role_detail: string;
  capability_tags: string[];
  system_prompt: string;
  input_contract: string;
  output_contract: string;
  default_budget: Record<string, number>;
  avatar: string | null;
  executor: string;
  slug_available: boolean;
  slug_suggestion?: string;
}

export interface FactoryTeamDraft {
  slug: string;
  name: string;
  description: string;
  lead_hint: string;
  member_agent_ids: string[];
  member_slugs: string[];
  dropped_member_slugs: string[];
  missing_capabilities: string[];
  playbook: { name: string; trigger_hint: string; stages: PlaybookStage[] };
  slug_available: boolean;
  slug_suggestion?: string;
}

export interface ArtifactRow {
  id: string;
  run_id: string;
  task_id: string | null;
  attempt_id: string | null;
  name: string;
  kind: string;
  mime: string;
  size: number;
  sha256: string;
  summary: string | null;
  status: string;
  derived_from: string[];
}

export interface PlatformSettings {
  orchestrator_model_profile_id: string | null;
  worker_default_model_profile_id: string | null;
  plan_approval_mode: "always" | "auto_if_under" | "never";
  plan_auto_approve_cost_threshold: number;
  currency: string;
  default_run_budget: Budget | null;
  global_parallel_limit: number;
  run_parallel_limit: number;
  max_tasks_per_plan: number;
  default_max_attempts: number;
  approval_timeout_minutes: number;
  daily_cost_limit: number | null;
  workspace_retention_days: number;
  allow_custom_python_tools: boolean;
  dangerous_command_list: string[];
  access_password: string | null;
  [key: string]: unknown;
}
