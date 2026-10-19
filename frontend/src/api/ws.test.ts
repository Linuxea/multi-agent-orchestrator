/** WS 客户端协议测试（§12.3）：订阅/补发/心跳/去重/重连/401。 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WsClient } from "./ws";
import { FakeWebSocket } from "../test/setup";

describe("WsClient", () => {
  let created: FakeWebSocket[];

  beforeEach(() => {
    vi.useFakeTimers();
    created = [];
    vi.stubGlobal(
      "WebSocket",
      class extends FakeWebSocket {
        constructor(url: string) {
          super(url);
          created.push(this);
        }
      },
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("连接后订阅通道并自动回 pong", () => {
    const client = new WsClient(() => "ws://test/ws");
    client.connect();
    const ws = created[0]!;
    ws.serverOpen();

    client.subscribe("run:run_123");
    expect(ws.sent).toEqual([
      JSON.stringify({ op: "subscribe", channel: "run:run_123", after_seq: 0 }),
    ]);

    ws.serverMessage({ op: "ping" });
    expect(ws.sent.at(-1)).toBe(JSON.stringify({ op: "pong" }));
  });

  it("收到事件回调并按 seq 去重", () => {
    const client = new WsClient(() => "ws://test/ws");
    const got: [number, string][] = [];
    client.onEvent((ev, channel) => got.push([ev.seq, channel]));
    client.connect();
    const ws = created[0]!;
    ws.serverOpen();
    client.subscribe("run:r1");

    ws.serverMessage({
      channel: "run:r1",
      event: { id: "a", seq: 1, type: "run.created", ts: "2026-01-01T00:00:00Z" },
    });
    ws.serverMessage({
      channel: "run:r1",
      event: { id: "a", seq: 1, type: "run.created", ts: "2026-01-01T00:00:00Z" },
    });
    ws.serverMessage({
      channel: "run:r1",
      event: { id: "b", seq: 2, type: "task.status_changed", ts: "2026-01-01T00:00:01Z" },
    });

    expect(got).toEqual([
      [1, "run:r1"],
      [2, "run:r1"],
    ]);
  });

  it("重连后以最后 seq 续订（无缝补发）", () => {
    const client = new WsClient(() => "ws://test/ws", () => 0);
    client.connect();
    const ws1 = created[0]!;
    ws1.serverOpen();
    client.subscribe("run:r1");
    ws1.serverMessage({
      channel: "run:r1",
      event: { id: "a", seq: 5, type: "run.created", ts: "t" },
    });

    ws1.close(1006); // 异常断开
    vi.advanceTimersByTime(0);
    expect(created.length).toBe(2);
    const ws2 = created[1]!;
    ws2.serverOpen();
    expect(ws2.sent).toEqual([
      JSON.stringify({ op: "subscribe", channel: "run:r1", after_seq: 5 }),
    ]);
  });

  it("4401 关闭码进入 unauthorized 且不重连", () => {
    const client = new WsClient(() => "ws://test/ws");
    const statuses: string[] = [];
    client.onStatus((s) => statuses.push(s));
    client.connect();
    const ws = created[0]!;
    ws.serverOpen();
    ws.close(4401);
    expect(statuses).toContain("unauthorized");
    expect(created.length).toBe(1); // 未新建连接
  });

  it("主动 close 不触发重连", () => {
    const client = new WsClient(() => "ws://test/ws");
    client.connect();
    const ws = created[0]!;
    ws.serverOpen();
    client.close();
    vi.advanceTimersByTime(30_000);
    expect(created.length).toBe(1);
  });
});
