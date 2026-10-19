/** 审批卡交互测试：决定调用与 remember 选择。 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApprovalsPage } from "./ApprovalsPage";

const { approvalApiMock } = vi.hoisted(() => ({
  approvalApiMock: { list: vi.fn(), decide: vi.fn() },
}));

vi.mock("../api/endpoints", () => ({
  approvalApi: approvalApiMock,
}));

vi.mock("../state/app", () => ({
  useApp: () => ({
    apiError: (e: unknown) => {
      throw e;
    },
    toast: () => undefined,
    onEvent: () => () => undefined,
  }),
}));

function pendingRow(overrides: Record<string, unknown> = {}) {
  return {
    id: "appr_1",
    run_id: "run_abc123xyz",
    attempt_id: "att_1",
    requested_by: "agent:researcher",
    tool_slug: "bash",
    action: "bash: ls -la",
    arguments: { command: "ls -la" },
    reason: "绑定权限 ask",
    risk_note: "该工具配置为需审批（ask）",
    status: "pending",
    ...overrides,
  };
}

describe("ApprovalsPage", () => {
  beforeEach(() => {
    approvalApiMock.list.mockReset();
    approvalApiMock.decide.mockReset();
    approvalApiMock.list.mockResolvedValue([]);
    approvalApiMock.decide.mockResolvedValue(undefined);
  });

  it("空态显示无待办", async () => {
    approvalApiMock.list.mockResolvedValue([]);
    render(<ApprovalsPage />);
    expect(await screen.findByText("没有待审批的调用")).toBeInTheDocument();
  });

  it("展示待审批卡片并提交决定（默认 once）", async () => {
    approvalApiMock.list.mockResolvedValue([pendingRow()]);
    render(<ApprovalsPage />);
    expect(await screen.findByText("bash: ls -la")).toBeInTheDocument();
    expect(screen.getByText(/需审批/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() =>
      expect(approvalApiMock.decide).toHaveBeenCalledWith("appr_1", true, "", "once"),
    );
  });

  it("拒绝走 remember=always_for_agent", async () => {
    approvalApiMock.list.mockResolvedValue([pendingRow({ id: "appr_2" })]);
    render(<ApprovalsPage />);
    await screen.findByText("bash: ls -la");
    await userEvent.click(screen.getByLabelText("始终允许该 agent"));
    await userEvent.click(screen.getByRole("button", { name: "拒绝" }));
    await waitFor(() =>
      expect(approvalApiMock.decide).toHaveBeenCalledWith("appr_2", false, "", "always_for_agent"),
    );
  });

  it("已处理的审批不显示决定按钮", async () => {
    approvalApiMock.list.mockResolvedValue([pendingRow({ status: "approved" })]);
    render(<ApprovalsPage />);
    await screen.findByText("bash: ls -la");
    expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
  });
});
