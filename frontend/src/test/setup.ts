import "@testing-library/jest-dom/vitest";

// jsdom 缺 WebSocket/localStorage 补齐（localStorage jsdom 自带）
class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = 0;
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(code = 1000): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  // 测试辅助：模拟服务端行为
  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  serverMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

Object.defineProperty(window, "WebSocket", {
  writable: true,
  value: FakeWebSocket,
});

export { FakeWebSocket };
