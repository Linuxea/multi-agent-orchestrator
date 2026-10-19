/** 时间线组装测试：消息 + 事件卡片合并、事件载荷渲染。 */

import { describe, expect, it } from "vitest";
import { buildTimeline } from "./RunStream";
import type { MessageInfo, PlanVersion } from "../api/types";

function msg(partial: Partial<MessageInfo>): MessageInfo {
  return {
    id: "m1",
    session_id: "s1",
    role: "user",
    content: "",
    run_id: null,
    created_at: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

const RUNS = [
  { id: "run_abc123", status: "completed", outcome: "success" },
];

const PLANS: PlanVersion[] = [
  {
    id: "p1",
    version: 1,
    tasks_snapshot: [
      { key: "t1", title: "调研", agent_slug: "researcher", depends_on: [] },
    ],
    change_reason: "",
    proposed_by: "orchestrator",
    approved: true,
  },
];

describe("buildTimeline", () => {
  it("按时间交错消息与事件卡片", () => {
    const messages = [
      msg({ id: "m1", role: "user", content: "需求 A", created_at: "2026-01-01T00:00:01Z", run_id: "run_abc123" }),
      msg({ id: "m2", role: "orchestrator", content: "最终答复", created_at: "2026-01-01T00:00:10Z", run_id: "run_abc123" }),
    ];
    const events = [
      {
        id: "e1",
        seq: 1,
        type: "orchestrator.message",
        payload: { text: "我提出了计划" },
        ts: "2026-01-01T00:00:02Z",
      },
      {
        id: "e2",
        seq: 2,
        type: "plan.proposed",
        payload: { version: 1, estimated_cost: 0.4, rationale: "先调研后撰写", task_count: 1 },
        ts: "2026-01-01T00:00:03Z",
      },
    ];

    const items = buildTimeline(messages, events, RUNS, "run_abc123", PLANS);
    // 里程碑 + 2 条消息 + 1 条协调者消息事件 + 1 张计划卡
    expect(items.length).toBe(5);
    // 顺序：runmark → 用户消息 → 协调者消息 → 计划卡 → 最终答复
    expect(items[0]!.key).toBe("runmark-run_abc123");
    expect(items[1]!.key).toBe("msg-m1");
    expect(items[2]!.key).toBe("ev-1");
    expect(items[3]!.key).toBe("ev-2");
    expect(items[4]!.key).toBe("msg-m2");
  });

  it("已回答的问题不再出现", () => {
    const events = [
      {
        id: "q1",
        seq: 1,
        type: "user.question_asked",
        payload: { question_id: "q1", question: "要深入哪个方向？", options: ["市场", "技术"] },
        ts: "2026-01-01T00:00:02Z",
      },
      {
        id: "a1",
        seq: 2,
        type: "user.answered",
        payload: { question_id: "q1" },
        ts: "2026-01-01T00:00:03Z",
      },
    ];
    const items = buildTimeline([], events, RUNS, "run_abc123", []);
    expect(items.filter((i) => i.key.startsWith("ev-"))).toHaveLength(0);
  });
});
