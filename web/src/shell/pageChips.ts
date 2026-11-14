import { signal } from '@preact/signals';

// 旧页面（iframe）往顶栏标题右边放可点击标签的通道，例如「后台进行中的任务」。
//
// 旧页面 → 外壳：parent.postMessage({ type: 'page-chips', page: '<导航 id>', chips: [...] }, origin)
//   每次发完整列表（不是增量）；发空数组 = 清空。
// 外壳 → 旧页面：点击标签时向页面里所有 iframe 发 { type: 'page-chip-click', page, id }。
// 只收同源消息。外壳只显示当前页面的标签；离开再回来，iframe 重新加载会再发一次。

export type ChipTone = 'blue' | 'green' | 'red' | 'yellow' | 'accent' | 'grey';
export interface PageChip { id: string; label: string; tone: ChipTone; title?: string }

const TONES = new Set<ChipTone>(['blue', 'green', 'red', 'yellow', 'accent', 'grey']);
const MAX_CHIPS = 20;

export const pageChips = signal<Record<string, PageChip[]>>({});

/** 校验一条 postMessage，合法则返回 [page, chips]，否则 null。 */
export function parseChipMessage(data: unknown): [string, PageChip[]] | null {
  if (!data || typeof data !== 'object') return null;
  const d = data as { type?: unknown; page?: unknown; chips?: unknown };
  if (d.type !== 'page-chips' || typeof d.page !== 'string' || !Array.isArray(d.chips)) return null;
  const chips: PageChip[] = [];
  for (const c of d.chips.slice(0, MAX_CHIPS)) {
    if (!c || typeof c !== 'object') continue;
    const { id, label, tone, title } = c as Record<string, unknown>;
    if (typeof id !== 'string' || typeof label !== 'string') continue;
    chips.push({
      id, label: label.slice(0, 40),
      tone: TONES.has(tone as ChipTone) ? (tone as ChipTone) : 'grey',
      title: typeof title === 'string' ? title.slice(0, 200) : undefined,
    });
  }
  return [d.page, chips];
}

export function listenPageChips(): () => void {
  const on = (e: MessageEvent) => {
    if (e.origin !== location.origin) return;
    const r = parseChipMessage(e.data);
    if (r) pageChips.value = { ...pageChips.value, [r[0]]: r[1] };
  };
  window.addEventListener('message', on);
  return () => window.removeEventListener('message', on);
}

export function clickPageChip(page: string, id: string) {
  document.querySelectorAll('iframe').forEach((f) => {
    f.contentWindow?.postMessage({ type: 'page-chip-click', page, id }, location.origin);
  });
}
