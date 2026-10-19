/** 团队编辑器测试：结构化剧本阶段 + 校验 + 保存编排（第四期）。 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TeamsPage } from "./TeamsPage";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    teamApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), remove: vi.fn(), setPlaybook: vi.fn(), removePlaybook: vi.fn() },
    agentApi: { list: vi.fn() },
    factoryApi: { draftAgent: vi.fn(), draftTeam: vi.fn() },
  },
}));

vi.mock("../api/endpoints", () => apiMock);
vi.mock("../state/app", () => ({
  useApp: () => ({
    apiError: (e: unknown) => {
      lastError = e;
    },
    toast: () => undefined,
  }),
}));

let lastError: unknown = null;

const AGENTS = [
  { id: "agent_r", slug: "web-researcher", name: "调研员" },
  { id: "agent_w", slug: "writer", name: "撰稿人" },
];

/** 按占位符取第 n 个 key 输入框。 */
const keyInput = (n: number): HTMLElement => {
  const el = screen.getAllByPlaceholderText("S1")[n];
  if (!el) throw new Error(`key 输入框 ${n} 不存在`);
  return el;
};

/** 按标签正则取第 n 个输入框。 */
const labeledInput = (pattern: RegExp, n: number): HTMLElement => {
  const el = screen.getAllByLabelText(pattern)[n];
  if (!el) throw new Error(`输入框 ${pattern}/${n} 不存在`);
  return el;
};

describe("TeamsPage 剧本编辑", () => {
  beforeEach(() => {
    lastError = null;
    Object.values(apiMock.teamApi).forEach((fn) => fn.mockReset());
    apiMock.teamApi.list.mockResolvedValue([]);
    apiMock.teamApi.create.mockResolvedValue({ id: "team_1", slug: "squad", name: "小队" });
    apiMock.teamApi.setPlaybook.mockResolvedValue({ id: "pb_1", name: "剧本" });
    apiMock.agentApi.list.mockResolvedValue(AGENTS);
  });

  it("空态提示无团队", async () => {
    render(<TeamsPage />);
    expect(await screen.findByText("尚无团队")).toBeInTheDocument();
  });

  it("结构化编辑两个阶段并保存：阶段随剧本提交", async () => {
    const user = userEvent.setup();
    render(<TeamsPage />);
    await screen.findByText("+ 新建团队");
    await user.click(screen.getByText("+ 新建团队"));

    await user.type(screen.getByLabelText("slug"), "squad");
    await user.type(screen.getByLabelText("名称"), "简报小队");

    await user.click(screen.getByText("+ 阶段"));
    await user.type(keyInput(0), "S1");
    await user.type(labeledInput(/^阶段名$/, 0), "调研");

    await user.click(screen.getByText("+ 阶段"));
    await user.type(keyInput(1), "S2");
    await user.type(labeledInput(/^阶段名$/, 1), "成稿");
    await user.type(labeledInput(/依赖阶段/, 1), "S1");

    await user.click(screen.getByText("保存"));

    await waitFor(() => {
      if (lastError) throw new Error(`保存被拦截: ${(lastError as Error).message}`);
      expect(apiMock.teamApi.setPlaybook).toHaveBeenCalled();
    });
    expect(apiMock.teamApi.create).toHaveBeenCalledWith(
      expect.objectContaining({ slug: "squad", name: "简报小队" }),
    );
    const pbArgs = apiMock.teamApi.setPlaybook.mock.calls[0];
    if (!pbArgs) throw new Error("setPlaybook 未被调用");
    const pbCall = pbArgs[1] as {
      stages: { key: string; name: string; depends_on: string[] }[];
    };
    expect(pbCall.stages).toEqual([
      { key: "S1", name: "调研", goal: "", agent_hint: "", depends_on: [] },
      { key: "S2", name: "成稿", goal: "", agent_hint: "", depends_on: ["S1"] },
    ]);
  });

  it("阶段依赖不存在的 key 时保存被拦截", async () => {
    const user = userEvent.setup();
    render(<TeamsPage />);
    await screen.findByText("+ 新建团队");
    await user.click(screen.getByText("+ 新建团队"));

    await user.type(screen.getByLabelText("slug"), "squad");
    await user.type(screen.getByLabelText("名称"), "小队");
    await user.click(screen.getByText("+ 阶段"));
    await user.type(keyInput(0), "S1");
    await user.type(labeledInput(/依赖阶段/, 0), "S9");

    await user.click(screen.getByText("保存"));
    await waitFor(() => expect(lastError).toBeInstanceOf(Error));
    expect((lastError as Error).message).toContain("S9");
    expect(apiMock.teamApi.create).not.toHaveBeenCalled();
  });
});
