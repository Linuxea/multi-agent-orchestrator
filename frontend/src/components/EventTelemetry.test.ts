/** 状态徽章与事件分类测试。 */

import { describe, expect, it } from "vitest";
import { eventCategory, eventLabel } from "./EventTelemetry";
import { statusLabel } from "./StatusChip";

describe("statusLabel", () => {
  it("运行状态映射中文", () => {
    expect(statusLabel("run", "running")).toEqual(["运行中", "signal"]);
    expect(statusLabel("run", "awaiting_plan_approval")).toEqual(["待批计划", "amber"]);
    expect(statusLabel("run", "completed")).toEqual(["已完成", "moss"]);
  });

  it("任务/尝试/审批状态映射", () => {
    expect(statusLabel("task", "awaiting_approval")).toEqual(["待审批", "amber"]);
    expect(statusLabel("task", "accepted")).toEqual(["已采纳", "moss"]);
    expect(statusLabel("attempt", "crashed")[1]).toBe("ember");
    expect(statusLabel("approval", "expired")).toEqual(["已过期", "ash"]);
  });

  it("未知状态回退原值", () => {
    expect(statusLabel("run", "whatever")).toEqual(["whatever", "ash"]);
  });
});

describe("eventCategory / eventLabel", () => {
  it("按前缀分类", () => {
    expect(eventCategory("task.status_changed")).toBe("task");
    expect(eventCategory("plan.proposed")).toBe("task");
    expect(eventCategory("attempt.output.delta")).toBe("attempt");
    expect(eventCategory("tool.call.blocked")).toBe("tool");
    expect(eventCategory("approval.requested")).toBe("approval");
    expect(eventCategory("budget.exceeded")).toBe("budget");
    expect(eventCategory("engine.invalid_transition")).toBe("engine");
    expect(eventCategory("orchestrator.message")).toBe("run");
  });

  it("常用事件有中文标签", () => {
    expect(eventLabel("plan.proposed")).toBe("提出计划");
    expect(eventLabel("user.question_asked")).toBe("向用户提问");
    expect(eventLabel("attempt.output.delta")).toBe("输出增量");
  });
});
