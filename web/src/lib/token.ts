import { signal } from '@preact/signals';

// 与旧页面共用同一个 localStorage 键：iframe 里的旧页面同源，读得到这里存的 token。
export const TOKEN_KEY = 'myspiders-token';
export const token = signal<string>(localStorage.getItem(TOKEN_KEY) ?? '');
/** 有接口回 401 时置真，外壳据此弹出 token 对话框。 */
export const tokenPrompt = signal(false);

export function setToken(v: string): void {
  const t = v.trim();
  token.value = t;
  if (t) localStorage.setItem(TOKEN_KEY, t);
  else localStorage.removeItem(TOKEN_KEY);
}
