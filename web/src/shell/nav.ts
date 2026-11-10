import { computed, signal } from '@preact/signals';
import { api } from '../lib/api';

export interface NavItem { id: string; title: string; icon: string; group: string; order: number; legacy_url: string | null }
interface ModulesResp { modules: { id: string; title?: string; error?: string; nav: NavItem[] }[] }

export const GROUPS: { id: string; title: string; fold?: boolean }[] = [
  { id: 'work', title: '工作 · 钱' },
  { id: 'life', title: '生活' },
  { id: 'collect', title: '收藏', fold: true },
  { id: 'ops', title: '运维', fold: true },
];

export const navItems = signal<NavItem[]>([]);
export const moduleErrors = signal<{ id: string; error: string }[]>([]);
export const navLoaded = signal(false);

export const navById = computed(() => new Map(navItems.value.map((n) => [n.id, n])));

/** 原生页面（不走 iframe）的 id 集合；导航里的同名条目会渲染成新页面。 */
export const NATIVE = new Set(['health', 'nintendo', 'qimen', 'claude_usage', 'lid']);

export async function loadNav() {
  const r = await api<ModulesResp>('/api/modules');
  const items = r.modules.flatMap((m) => m.nav).sort((a, b) => a.order - b.order);
  navItems.value = items;
  moduleErrors.value = r.modules.filter((m) => m.error).map((m) => ({ id: m.id, error: m.error! }));
  navLoaded.value = true;
}

/** 旧面板的 hash 书签（#mercor / #agent …）→ 新路由。 */
const LEGACY_HASH: Record<string, string> = { agent: 'claude_usage' };
export function legacyHashRoute(hash: string): string | null {
  const h = hash.replace(/^#/, '');
  if (!h) return null;
  return `/dashboard/${LEGACY_HASH[h] ?? h}`;
}
