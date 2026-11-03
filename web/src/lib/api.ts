import { token, tokenPrompt } from './token';

export class ApiError extends Error {
  constructor(public status: number, message: string, public body?: unknown) {
    super(message);
  }
}

export interface RequestOpts {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  signal?: AbortSignal;
}

/** 所有请求都带 x-token（读接口不需要也无妨）；401 时提示输入 token。 */
export async function api<T = unknown>(path: string, opts: RequestOpts = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token.value) headers['x-token'] = token.value;
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    if (opts.body instanceof FormData) body = opts.body;
    else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
  }
  const res = await fetch(path, { method: opts.method ?? (body ? 'POST' : 'GET'), headers, body, signal: opts.signal });
  const text = await res.text();
  let data: unknown = text;
  try { data = text ? JSON.parse(text) : null; } catch { /* 非 JSON 原样返回 */ }
  if (!res.ok) {
    if (res.status === 401) tokenPrompt.value = true;
    const detail = (data && typeof data === 'object' && 'detail' in data) ? String((data as { detail: unknown }).detail) : res.statusText;
    throw new ApiError(res.status, detail || `HTTP ${res.status}`, data);
  }
  return data as T;
}
