/** 全局应用状态：认证、WS 连接、审批角标、提示条。 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, getAccessKey, setAccessKey } from "../api/client";
import { authApi } from "../api/endpoints";
import { WsClient, defaultWsUrl, type StreamEvent, type WsStatus } from "../api/ws";

export interface Toast {
  id: number;
  text: string;
  kind: "info" | "err";
}

interface AppState {
  authRequired: boolean;
  authChecked: boolean;
  login: (password: string) => Promise<boolean>;
  logout: () => void;
  wsStatus: WsStatus;
  subscribe: (channel: string) => void;
  unsubscribe: (channel: string) => void;
  onEvent: (fn: (event: StreamEvent, channel: string) => void) => () => void;
  toasts: Toast[];
  toast: (text: string, kind?: "info" | "err") => void;
  apiError: (e: unknown) => void;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp 必须在 AppProvider 内使用");
  return ctx;
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [authRequired, setAuthRequired] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [wsStatus, setWsStatus] = useState<WsStatus>("closed");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastSeq = useRef(0);

  const toast = useCallback((text: string, kind: "info" | "err" = "info") => {
    const id = ++toastSeq.current;
    setToasts((prev) => [...prev, { id, text, kind }]);
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4200);
  }, []);

  const apiError = useCallback(
    (e: unknown) => {
      const text =
        e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e);
      toast(text, "err");
    },
    [toast],
  );

  // 探测访问密码是否启用
  useEffect(() => {
    authApi
      .status()
      .then((s) => {
        setAuthRequired(s.required);
        setAuthChecked(true);
      })
      .catch(() => {
        // 后端不可达：显示离线提示，由 WS 状态呈现
        setAuthChecked(true);
      });
  }, []);

  const wsRef = useRef<WsClient | null>(null);
  if (wsRef.current === null) {
    wsRef.current = new WsClient(() => defaultWsUrl(getAccessKey()));
  }
  const ws = wsRef.current;

  useEffect(() => {
    if (!authChecked) return;
    ws.connect();
    const off = ws.onStatus(setWsStatus);
    return () => {
      off();
      ws.close();
    };
  }, [authChecked, ws]);

  // WS 报 4401：访问密码已启用而本地密钥失效
  useEffect(() => {
    if (wsStatus === "unauthorized") setAuthRequired(true);
  }, [wsStatus]);

  const login = useCallback(
    async (password: string) => {
      try {
        await authApi.verify(password);
      } catch {
        return false;
      }
      setAccessKey(password);
      setAuthRequired(false);
      ws.close();
      ws.connect();
      return true;
    },
    [ws],
  );

  const logout = useCallback(() => {
    setAccessKey(null);
    setAuthRequired(true);
    ws.close();
  }, [ws]);

  const value = useMemo<AppState>(
    () => ({
      authRequired,
      authChecked,
      login,
      logout,
      wsStatus,
      subscribe: (c: string) => ws.subscribe(c),
      unsubscribe: (c: string) => ws.unsubscribe(c),
      onEvent: (fn) => ws.onEvent(fn),
      toasts,
      toast,
      apiError,
    }),
    [authRequired, authChecked, login, logout, wsStatus, ws, toasts, toast, apiError],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
