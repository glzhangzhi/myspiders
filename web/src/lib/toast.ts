import { signal } from '@preact/signals';

export interface Toast { id: number; text: string; kind: 'ok' | 'err' | 'info' }
export const toasts = signal<Toast[]>([]);
let seq = 0;

export function toast(text: string, kind: Toast['kind'] = 'info', ms = 3200) {
  const id = ++seq;
  toasts.value = [...toasts.value, { id, text, kind }];
  setTimeout(() => { toasts.value = toasts.value.filter((t) => t.id !== id); }, ms);
}

export function toastError(e: unknown) { toast(e instanceof Error ? e.message : String(e), 'err', 5000); }
