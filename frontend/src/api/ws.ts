/** WebSocket 事件客户端（§12.3）。
 *
 * - 连接 /api/v1/ws（带访问密码查询参数）；
 * - subscribe 时带 after_seq：服务端先补发历史持久化事件再推实时事件；
 * - 收到 {"op":"ping"} 自动回 pong（60s 无 pong 会被断开）；
 * - 事件按 run 通道内 seq 去重（补发与实时可能重叠）；
 * - 断线指数退避重连；4401 = 访问密码错误，交上层回登录页。
 */

export type WsStatus = "connecting" | "open" | "closed" | "unauthorized";

export interface StreamEvent {
  id: string;
  seq: number;
  type: string;
  actor: string;
  task_key: string | null;
  attempt_id: string | null;
  payload: Record<string, unknown>;
  ts: string;
}

type EventListener = (event: StreamEvent, channel: string) => void;
type StatusListener = (status: WsStatus) => void;

const PING_PONG = "ping";

export class WsClient {
  private ws: WebSocket | null = null;
  private status: WsStatus = "closed";
  private eventListeners = new Set<EventListener>();
  private statusListeners = new Set<StatusListener>();
  private lastSeq = new Map<string, number>();
  private subscribed = new Set<string>();
  private reconnectAttempt = 0;
  private closedByUser = false;
  private connectTimer: number | null = null;

  constructor(
    private url: () => string,
    private backoffMs: (attempt: number) => number = (n) =>
      Math.min(15000, 800 * 2 ** n),
  ) {}

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  private open(): void {
    this.setStatus("connecting");
    const ws = new WebSocket(this.url());
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectAttempt = 0;
      this.setStatus("open");
      // 重连后恢复订阅（after_seq = 各通道已见最大 seq，实现无缝补发）
      for (const channel of this.subscribed) {
        this.sendSubscribe(channel, this.lastSeq.get(channel) ?? 0);
      }
    };

    ws.onmessage = (msg) => {
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(msg.data as string) as Record<string, unknown>;
      } catch {
        return;
      }
      if (data["op"] === PING_PONG) {
        ws.send(JSON.stringify({ op: "pong" }));
        return;
      }
      if (data["op"] === "subscribed") return;
      const channel = data["channel"];
      const event = data["event"];
      if (typeof channel !== "string" || typeof event !== "object" || event === null) return;
      const ev = event as StreamEvent & { seq?: unknown };
      if (typeof ev.seq !== "number") return;
      // 去重：同通道只接受更大 seq
      const seen = this.lastSeq.get(channel) ?? 0;
      if (ev.seq <= seen) return;
      this.lastSeq.set(channel, ev.seq);
      for (const fn of this.eventListeners) {
        fn(ev as StreamEvent, channel);
      }
    };

    ws.onclose = (ev) => {
      this.ws = null;
      if (ev.code === 4401) {
        this.setStatus("unauthorized");
        return; // 访问密码错误：不重连，等用户重新登录
      }
      this.setStatus("closed");
      if (!this.closedByUser) {
        const delay = this.backoffMs(this.reconnectAttempt++);
        this.connectTimer = window.setTimeout(() => this.open(), delay);
      }
    };

    ws.onerror = () => {
      /* onclose 会跟随触发，交给其处理 */
    };
  }

  private sendSubscribe(channel: string, afterSeq: number): void {
    this.ws?.send(JSON.stringify({ op: "subscribe", channel, after_seq: afterSeq }));
  }

  subscribe(channel: string): void {
    this.subscribed.add(channel);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.sendSubscribe(channel, this.lastSeq.get(channel) ?? 0);
    }
  }

  unsubscribe(channel: string): void {
    this.subscribed.delete(channel);
    this.lastSeq.delete(channel);
    // 协议无退订操作：客户端按通道过滤已退订内容
  }

  onEvent(fn: EventListener): () => void {
    this.eventListeners.add(fn);
    return () => this.eventListeners.delete(fn);
  }

  onStatus(fn: StatusListener): () => void {
    this.statusListeners.add(fn);
    fn(this.status);
    return () => this.statusListeners.delete(fn);
  }

  private setStatus(status: WsStatus): void {
    this.status = status;
    for (const fn of this.statusListeners) fn(status);
  }

  close(): void {
    this.closedByUser = true;
    if (this.connectTimer !== null) window.clearTimeout(this.connectTimer);
    this.ws?.close(1000);
    this.ws = null;
    this.setStatus("closed");
  }
}

export function defaultWsUrl(key: string | null): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const k = key ? `?key=${encodeURIComponent(key)}` : "";
  return `${proto}//${location.host}/api/v1/ws${k}`;
}
