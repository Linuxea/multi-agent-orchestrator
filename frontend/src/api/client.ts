/** REST 客户端：基址 /api/v1，自动附带访问密码，统一错误形态。 */

const KEY_STORAGE = "platform.access-key";

export function getAccessKey(): string | null {
  return localStorage.getItem(KEY_STORAGE);
}

export function setAccessKey(key: string | null): void {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

async function parseError(resp: Response): Promise<string> {
  try {
    const body = (await resp.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (body.detail !== undefined) return JSON.stringify(body.detail);
  } catch {
    /* 非 JSON 响应体 */
  }
  return `请求失败（HTTP ${resp.status}）`;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const key = getAccessKey();
  if (key) {
    headers.set("Authorization", `Bearer ${key}`);
    headers.set("X-Platform-Key", key);
  }
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(`/api/v1${path}`, { ...init, headers });
  if (!resp.ok) throw new ApiError(resp.status, await parseError(resp));
  if (resp.status === 204) return undefined as T;
  const ct = resp.headers.get("content-type") ?? "";
  return ct.includes("json") ? ((await resp.json()) as T) : (undefined as T);
}

export const jsonBody = (data: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(data),
});

/** 带访问密码下载文件（<a href> 无法附带头部，改经 blob）。 */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const headers = new Headers();
  const key = getAccessKey();
  if (key) {
    headers.set("Authorization", `Bearer ${key}`);
    headers.set("X-Platform-Key", key);
  }
  const resp = await fetch(`/api/v1${path}`, { headers });
  if (!resp.ok) throw new ApiError(resp.status, "下载失败");
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
