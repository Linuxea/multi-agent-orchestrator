/** REST 客户端测试：密钥头、错误形态、204。 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, getAccessKey, setAccessKey } from "./client";

function mockFetch(resp: { status: number; body: unknown; ct?: string }) {
  const noBody = resp.status === 204;
  const body = noBody
    ? null
    : typeof resp.body === "string"
      ? resp.body
      : JSON.stringify(resp.body);
  return vi.fn().mockResolvedValue(
    new Response(body, {
      status: resp.status,
      headers: noBody ? {} : { "content-type": resp.ct ?? "application/json" },
    }),
  );
}

afterEach(() => {
  setAccessKey(null);
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("携带 Bearer 与 X-Platform-Key", async () => {
    setAccessKey("key123");
    const fetchMock = mockFetch({ status: 200, body: { ok: 1 } });
    vi.stubGlobal("fetch", fetchMock);

    await api("/anything");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer key123");
    expect(headers.get("X-Platform-Key")).toBe("key123");
    expect(String(fetchMock.mock.calls[0]![0])).toBe("/api/v1/anything");
  });

  it("POST 自动 JSON 头", async () => {
    const fetchMock = mockFetch({ status: 201, body: { id: "x" } });
    vi.stubGlobal("fetch", fetchMock);
    await api("/sessions", { method: "POST", body: JSON.stringify({ title: "t" }) });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
  });

  it("错误转 ApiError 并带 detail", async () => {
    vi.stubGlobal("fetch", mockFetch({ status: 409, body: { detail: "slug 已存在" } }));
    await expect(api("/agents")).rejects.toMatchObject({
      status: 409,
      detail: "slug 已存在",
    });
    await expect(api("/agents")).rejects.toBeInstanceOf(ApiError);
  });

  it("204 返回 undefined", async () => {
    vi.stubGlobal("fetch", mockFetch({ status: 204, body: "" }));
    await expect(api("/runs/x/pause", { method: "POST" })).resolves.toBeUndefined();
  });

  it("getAccessKey 持久化在 localStorage", () => {
    setAccessKey("abc");
    expect(getAccessKey()).toBe("abc");
    setAccessKey(null);
    expect(getAccessKey()).toBeNull();
  });
});
