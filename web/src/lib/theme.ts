import { effect, signal } from '@preact/signals';

export type ThemeMode = 'dark' | 'light' | 'auto';
const KEY = 'myspiders-theme';
export const themeMode = signal<ThemeMode>((localStorage.getItem(KEY) as ThemeMode) || 'dark');

/** 把主题写到本页与所有同源 iframe（旧页面）的 <html data-theme>。 */
export function applyTheme(doc: Document = document, mode: ThemeMode = themeMode.value) {
  doc.documentElement.dataset.theme = mode;
  doc.querySelectorAll('iframe').forEach((f) => {
    try { if (f.contentDocument) f.contentDocument.documentElement.dataset.theme = mode; } catch { /* 跨源忽略 */ }
  });
}

effect(() => {
  localStorage.setItem(KEY, themeMode.value);
  applyTheme();
});

export function cycleTheme() {
  themeMode.value = themeMode.value === 'dark' ? 'light' : themeMode.value === 'light' ? 'auto' : 'dark';
}
