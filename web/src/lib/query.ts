import { useCallback, useEffect, useRef, useState } from 'preact/hooks';
import { api } from './api';

/**
 * 极简 SWR：同一个 key 的请求进程内共享缓存与在途 Promise。
 * 切回一个页面时先秒出上次的数据，同时后台刷新 —— 这是「前端更快」的主要来源。
 */
interface Entry { data?: unknown; error?: unknown; at: number; inflight?: Promise<unknown> }
const cache = new Map<string, Entry>();
const listeners = new Map<string, Set<() => void>>();

function notify(key: string) { listeners.get(key)?.forEach((fn) => fn()); }

export function fetchKey<T>(key: string, fetcher: () => Promise<T> = () => api<T>(key)): Promise<T> {
  const e = cache.get(key) ?? { at: 0 };
  if (e.inflight) return e.inflight as Promise<T>;
  const p = fetcher().then(
    (data) => { cache.set(key, { data, at: Date.now() }); notify(key); return data; },
    (error) => { cache.set(key, { ...e, error, inflight: undefined }); notify(key); throw error; },
  );
  cache.set(key, { ...e, inflight: p });
  return p;
}

export function mutateKey(key: string, data?: unknown) {
  if (data === undefined) { cache.delete(key); fetchKey(key).catch(() => {}); }
  else { cache.set(key, { data, at: Date.now() }); notify(key); }
}

export interface QueryResult<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  refresh: () => Promise<void>;
}

export function useQuery<T>(key: string | null, opts: { refreshMs?: number; staleMs?: number } = {}): QueryResult<T> {
  const [, force] = useState(0);
  const staleMs = opts.staleMs ?? 15_000;
  const keyRef = useRef(key);
  keyRef.current = key;

  useEffect(() => {
    if (!key) return;
    const fn = () => force((n) => n + 1);
    const set = listeners.get(key) ?? new Set();
    set.add(fn); listeners.set(key, set);
    const e = cache.get(key);
    if (!e || Date.now() - e.at > staleMs) fetchKey(key).catch(() => {});
    let t: number | undefined;
    if (opts.refreshMs) {
      t = window.setInterval(() => { if (!document.hidden) fetchKey(key).catch(() => {}); }, opts.refreshMs);
    }
    return () => { set.delete(fn); if (t) clearInterval(t); };
  }, [key, opts.refreshMs, staleMs]);

  const refresh = useCallback(async () => {
    if (!keyRef.current) return;
    cache.delete(keyRef.current);
    await fetchKey(keyRef.current).catch(() => {});
  }, []);

  const e = key ? cache.get(key) : undefined;
  return {
    data: e?.data as T | undefined,
    error: e?.error as Error | undefined,
    loading: !!key && !e?.data && !e?.error,
    refresh,
  };
}

/** 测试用：清空缓存。 */
export function _resetQueryCache() { cache.clear(); listeners.clear(); }
