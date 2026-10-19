import type { Budget, Usage } from "../api/types";

/** 预算用量条：费用 / 轮次 / 时长三行，超支变红。 */
export function UsageBar({
  usage,
  budget,
  currency = "CNY",
}: {
  usage: Partial<Usage> | null;
  budget: Budget | null;
  currency?: string;
}) {
  const rows: { label: string; value: number; max: number | null; unit: string }[] = [];
  const cost = usage?.cost ?? 0;
  rows.push({
    label: "费用",
    value: cost,
    max: budget?.max_cost ?? null,
    unit: currency === "CNY" ? "¥" : currency,
  });
  const turns = usage?.turns ?? 0;
  if (budget?.max_turns) rows.push({ label: "轮次", value: turns, max: budget.max_turns, unit: "轮" });
  const wall = usage?.wall_seconds ?? 0;
  if (budget?.max_wall_seconds)
    rows.push({ label: "时长", value: wall, max: budget.max_wall_seconds, unit: "s" });

  return (
    <div className="usage-bar">
      {rows.map((r) => {
        const pct = r.max ? Math.min(100, (r.value / r.max) * 100) : 0;
        const over = r.max !== null && r.value > r.max;
        return (
          <div className="usage-row" key={r.label}>
            <span className="usage-label">{r.label}</span>
            <div className="usage-track">
              <div
                className={`usage-fill${over ? " over" : ""}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="mono usage-num">
              {r.unit === "¥" ? `${r.unit}${r.value.toFixed(3)}` : `${Math.round(r.value)}${r.unit}`}
              {r.max !== null
                ? ` / ${r.unit === "¥" ? `${r.unit}${r.max}` : `${r.max}${r.unit}`}`
                : ""}
            </span>
          </div>
        );
      })}
    </div>
  );
}
