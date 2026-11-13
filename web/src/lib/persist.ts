import { useState } from 'preact/hooks';

/**
 * 界面选择状态的本地记忆（标签页、筛选、排序……刷新/下次打开还在）。
 *
 * - key 统一加 `ui.` 前缀，按「页面.东西」命名（`ui.voice.tab`）；
 * - 只记「选了哪个」，不记搜索框、正在编辑的表单、翻页、多选勾选；
 * - 存的值可能已经失效（选项被删、类型改了），用 `valid` 校验，不合法就回默认值；
 * - localStorage 不可用（隐私模式、配额满）时静默退化成普通 useState。
 */
const PREFIX = 'ui.';

export function loadPersisted<T>(key: string, fallback: T, valid?: (v: unknown) => boolean): T {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (raw === null) return fallback;
    const v = JSON.parse(raw) as unknown;
    if (valid ? !valid(v) : typeof v !== typeof fallback) return fallback;
    return v as T;
  } catch { return fallback; }
}

export function savePersisted(key: string, v: unknown) {
  try { localStorage.setItem(PREFIX + key, JSON.stringify(v)); } catch { /* 存不了就算了 */ }
}

export function usePersisted<T>(key: string, fallback: T, valid?: (v: unknown) => boolean): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => loadPersisted(key, fallback, valid));
  const set = (nv: T) => { setV(nv); savePersisted(key, nv); };
  return [v, set];
}

/** 校验：值在给定的可选集合里。 */
export const oneOf = (...opts: unknown[]) => (v: unknown) => opts.includes(v);
