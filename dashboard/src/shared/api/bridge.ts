/**
 * AstrBot 插件 Page Bridge 通信底层。
 * 适配本插件的后端约定：顶层 {ok, ...}，错误时 {ok:false, error, message, status}。
 */

export interface AstrBotContext {
  isDark?: boolean;
  pluginName?: string;
  [key: string]: unknown;
}

export interface AstrBotPluginPageBridge {
  ready?: () => Promise<AstrBotContext>;
  onContext?: (callback: (ctx: AstrBotContext) => void) => () => void;
  apiGet?: <T = unknown>(path: string, params?: Record<string, unknown>) => Promise<T | null>;
  apiPost?: <T = unknown>(path: string, body?: unknown) => Promise<T | null>;
  [key: string]: unknown;
}

declare global {
  interface Window {
    AstrBotPluginPage?: AstrBotPluginPageBridge;
  }
}

function getBridge(): AstrBotPluginPageBridge | null {
  return window.AstrBotPluginPage || null;
}

export async function fetchContext(): Promise<AstrBotContext> {
  const bridge = getBridge();
  if (bridge && typeof bridge.ready === "function") {
    return await bridge.ready();
  }
  return { isDark: false, pluginName: "astrbot_plugin_shoubanhua" };
}

export function onHostContext(callback: (ctx: AstrBotContext) => void): () => void {
  const bridge = getBridge();
  if (bridge && typeof bridge.onContext === "function") {
    return bridge.onContext(callback);
  }
  return () => undefined;
}

export async function bridgeGet<T = unknown>(
  path: string,
  params?: Record<string, unknown>
): Promise<T | null> {
  const bridge = getBridge();
  if (bridge && typeof bridge.apiGet === "function") {
    return await bridge.apiGet<T>(path, params);
  }
  return null;
}

export async function bridgePost<T = unknown>(
  path: string,
  body?: unknown
): Promise<T | null> {
  const bridge = getBridge();
  if (bridge && typeof bridge.apiPost === "function") {
    return await bridge.apiPost<T>(path, body);
  }
  return null;
}

/** 从 AstrBot 各种包装层级中解包出真正的负载 */
function unwrap(res: unknown): Record<string, unknown> {
  let current = res;
  for (let depth = 0; depth < 3; depth += 1) {
    if (current && typeof current === "object" && "data" in (current as Record<string, unknown>)) {
      const next = (current as Record<string, unknown>).data;
      if (next && typeof next === "object") {
        current = next;
        continue;
      }
    }
    break;
  }
  return (current && typeof current === "object" ? current : {}) as Record<string, unknown>;
}

/** 请求包装：统一抛错（优先 message，其次 error），成功返回解包后的负载 */
export async function request<T = Record<string, unknown>>(
  method: "GET" | "POST",
  path: string,
  paramsOrBody?: Record<string, unknown>
): Promise<T> {
  if (!getBridge()) {
    throw new Error("AstrBot 插件页面通信桥接不可用，请在 AstrBot 管理面板内打开。");
  }
  let res: unknown;
  try {
    res = method === "GET" ? await bridgeGet(path, paramsOrBody) : await bridgePost(path, paramsOrBody);
  } catch (err) {
    throw new Error((err as Error)?.message || "请求执行异常");
  }
  if (res === null || res === undefined) {
    throw new Error("请求失败：桥接未返回数据");
  }
  const payload = unwrap(res);
  const ok = payload.ok;
  if (ok === false || (ok === undefined && payload.error)) {
    const message = String(payload.message || payload.error || "请求失败");
    const error = new Error(message) as Error & { status?: number };
    error.status = Number(payload.status || 0);
    throw error;
  }
  return payload as T;
}

export async function apiGet<T = Record<string, unknown>>(
  path: string,
  params?: Record<string, unknown>
): Promise<T> {
  return request<T>("GET", path, params);
}

export async function apiPost<T = Record<string, unknown>>(
  path: string,
  body?: Record<string, unknown>
): Promise<T> {
  return request<T>("POST", path, body);
}
