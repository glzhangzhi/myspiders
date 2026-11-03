import { signal } from '@preact/signals';
import { useLocation } from 'preact-iso';
import { GROUPS, navItems } from './nav';
import { cycleTheme, themeMode } from '../lib/theme';

const FOLD_KEY = 'myspiders-nav-folds';
const COLLAPSE_KEY = 'myspiders-sidebar';
const folds = signal<Record<string, boolean>>((() => { try { return JSON.parse(localStorage.getItem(FOLD_KEY) || '{}'); } catch { return {}; } })());
export const collapsed = signal(localStorage.getItem(COLLAPSE_KEY) === 'collapsed');
export const mobileOpen = signal(false);

function isFolded(g: { id: string; fold?: boolean }) { return g.id in folds.value ? folds.value[g.id] : !!g.fold; }
function toggleFold(id: string, cur: boolean) { folds.value = { ...folds.value, [id]: !cur }; localStorage.setItem(FOLD_KEY, JSON.stringify(folds.value)); }
export function toggleCollapsed() { collapsed.value = !collapsed.value; localStorage.setItem(COLLAPSE_KEY, collapsed.value ? 'collapsed' : 'open'); }

const THEME_LABEL = { dark: '🌙 深色', light: '☀️ 浅色', auto: '🖥️ 跟随系统' } as const;

export function Sidebar(p: { onSpiders: () => void; onTools: () => void; onPalette: () => void }) {
  const loc = useLocation();
  const active = loc.path.replace(/^\/dashboard\/?/, '').split('/')[0] || 'health';
  const go = (id: string) => { loc.route(`/dashboard/${id}`); mobileOpen.value = false; };
  return (
    <>
      {mobileOpen.value && <div class="scrim show-mobile" onClick={() => (mobileOpen.value = false)} />}
      <aside class={`sidebar ${collapsed.value ? 'collapsed' : ''} ${mobileOpen.value ? 'open' : ''}`}>
        <div class="brand"><span class="logo">🕷️</span><span class="name">MySpiders</span></div>
        <nav aria-label="功能导航">
          {GROUPS.map((g) => {
            const items = navItems.value.filter((n) => n.group === g.id);
            if (!items.length) return null;
            const folded = isFolded(g) && !items.some((i) => i.id === active);
            return (
              <div key={g.id} class={`nav-group ${folded ? 'folded' : ''}`}>
                <button class="nav-ghead" onClick={() => toggleFold(g.id, folded)} aria-expanded={!folded}>
                  <span class="chev">▼</span><span class="gtext">{g.title}</span>
                </button>
                {!folded && items.map((it) => (
                  <a key={it.id} href={`/dashboard/${it.id}`} class={`nav-item ${it.id === active ? 'active' : ''}`} title={it.title}
                    onClick={(e) => { e.preventDefault(); go(it.id); }}>
                    <span class="icon">{it.icon}</span><span class="label">{it.title}</span>
                  </a>
                ))}
              </div>
            );
          })}
        </nav>
        <div class="foot">
          <button class="nav-item" onClick={p.onPalette} title="命令面板"><span class="icon">⌘</span><span class="label">命令面板 <kbd>Ctrl K</kbd></span></button>
          <button class="nav-item" onClick={p.onSpiders} title="爬虫管理"><span class="icon">⏱️</span><span class="label">爬虫管理</span></button>
          <button class="nav-item" onClick={p.onTools} title="工具箱"><span class="icon">🧰</span><span class="label">工具箱</span></button>
          <button class="nav-item" onClick={cycleTheme} title="切换主题"><span class="icon">🎨</span><span class="label">{THEME_LABEL[themeMode.value]}</span></button>
          <button class="nav-item hide-mobile" onClick={toggleCollapsed} title="收起/展开侧边栏"><span class="icon">{collapsed.value ? '»' : '«'}</span><span class="label">收起</span></button>
        </div>
      </aside>
    </>
  );
}
