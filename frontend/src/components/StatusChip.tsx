/** 状态枚举 → 中文标签与色调（run / task / attempt / approval / tool）。 */

const RUN: Record<string, [string, string]> = {
  created: ["已创建", "ash"],
  planning: ["规划中", "dusk"],
  clarifying: ["澄清中", "dusk"],
  awaiting_plan_approval: ["待批计划", "amber"],
  running: ["运行中", "signal"],
  paused: ["已暂停", "amber"],
  awaiting_user: ["等待用户", "amber"],
  synthesizing: ["汇总中", "dusk"],
  completed: ["已完成", "moss"],
  failed: ["已失败", "ember"],
  cancelled: ["已取消", "ash"],
};

const TASK: Record<string, [string, string]> = {
  pending: ["待解锁", "ash"],
  ready: ["就绪", "signal"],
  queued: ["已排队", "dusk"],
  running: ["执行中", "signal"],
  awaiting_approval: ["待审批", "amber"],
  reported: ["待复核", "amber"],
  needs_rework: ["需返工", "ember"],
  blocked: ["受阻", "ember"],
  awaiting_user: ["等待用户", "amber"],
  accepted: ["已采纳", "moss"],
  failed: ["已失败", "ember"],
  skipped: ["已跳过", "ash"],
  superseded: ["已替代", "ash"],
  cancelled: ["已取消", "ash"],
};

const ATTEMPT: Record<string, [string, string]> = {
  queued: ["排队", "dusk"],
  running: ["执行", "signal"],
  awaiting_approval: ["待审", "amber"],
  reported: ["已回报", "moss"],
  timed_out: ["超时", "ember"],
  crashed: ["崩溃", "ember"],
  cancelled: ["取消", "ash"],
};

const APPROVAL: Record<string, [string, string]> = {
  pending: ["待审批", "amber"],
  approved: ["已通过", "moss"],
  rejected: ["已拒绝", "ember"],
  expired: ["已过期", "ash"],
};

const TOOL: Record<string, [string, string]> = {
  untested: ["未测试", "ash"],
  ok: ["正常", "moss"],
  error: ["异常", "ember"],
  disabled: ["已停用", "ash"],
};

const TABLES = { run: RUN, task: TASK, attempt: ATTEMPT, approval: APPROVAL, tool: TOOL };

export function statusLabel(kind: keyof typeof TABLES, status: string): [string, string] {
  return TABLES[kind][status] ?? [status, "ash"];
}

export function StatusChip({
  kind,
  status,
  live = false,
}: {
  kind: keyof typeof TABLES;
  status: string;
  live?: boolean;
}) {
  const [label, tone] = statusLabel(kind, status);
  return (
    <span className={`chip chip-${tone}${live ? " chip-live" : ""}`}>
      {label}
    </span>
  );
}

export function OutcomeChip({ outcome }: { outcome: string }) {
  const map: Record<string, [string, string]> = {
    success: ["圆满达成", "moss"],
    partial: ["部分达成", "amber"],
    failed: ["未达成", "ember"],
    cancelled: ["已中止", "ash"],
  };
  const [label, tone] = map[outcome] ?? [outcome, "ash"];
  return <span className={`chip chip-${tone}`}>{label}</span>;
}
