import { useMemo, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso';
import { Overlay } from '../ui';
import { navItems } from './nav';
import { spiders, toggleSpider, triggerSpider } from './spiders';
import { cycleTheme } from '../lib/theme';
import { tokenPrompt } from '../lib/token';

interface Cmd { id: string; icon: string; label: string; hint?: string; run: () => void }

/** 模糊匹配：每个查询字符按顺序出现即可（拼音首字母不做，中文按子串）。 */
export function fuzzy(q: string, s: string): boolean {
  q = q.toLowerCase().replace(/\s+/g, ''); s = s.toLowerCase();
  if (!q) return true;
  if (s.includes(q)) return true;
  let i = 0;
  for (const ch of s) if (ch === q[i]) i++;
  return i === q.length;
}

export function Palette(p: { onClose: () => void; onSpiders: () => void; onTools: () => void }) {
  const loc = useLocation();
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const cmds = useMemo<Cmd[]>(() => [
    ...navItems.value.map((n) => ({ id: `go:${n.id}`, icon: n.icon, label: n.title, hint: '打开', run: () => loc.route(`/dashboard/${n.id}`) })),
    { id: 'spiders', icon: '⏱️', label: '爬虫管理', hint: '面板', run: p.onSpiders },
    { id: 'tools', icon: '🧰', label: '工具箱', hint: '面板', run: p.onTools },
    { id: 'theme', icon: '🎨', label: '切换主题', run: cycleTheme },
    { id: 'token', icon: '🔑', label: '设置管理令牌', run: () => (tokenPrompt.value = true) },
    ...spiders.value.flatMap((s) => [
      { id: `run:${s.spider_id}`, icon: '▶', label: `立即运行：${s.name}`, hint: s.spider_id, run: () => triggerSpider(s.spider_id) },
      { id: `tog:${s.spider_id}`, icon: s.enabled ? '⏸' : '⏵', label: `${s.enabled ? '停用' : '启用'}：${s.name}`, hint: s.spider_id, run: () => toggleSpider(s.spider_id, !s.enabled) },
    ]),
  ], [navItems.value, spiders.value]);
  const list = cmds.filter((c) => fuzzy(q, `${c.label} ${c.hint ?? ''}`)).slice(0, 40);
  const run = (c?: Cmd) => { if (!c) return; p.onClose(); c.run(); };
  return (
    <Overlay onClose={p.onClose}>
      <div class="palette" role="dialog" aria-label="命令面板">
        <input autoFocus placeholder="跳转页面、运行爬虫、切换主题…" value={q}
          onInput={(e) => { setQ((e.target as HTMLInputElement).value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, list.length - 1)); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
            else if (e.key === 'Enter') { e.preventDefault(); run(list[sel]); }
          }} />
        <ul role="listbox">
          {list.map((c, i) => (
            <li key={c.id} role="option" aria-selected={i === sel} class={i === sel ? 'sel' : ''} onMouseEnter={() => setSel(i)} onClick={() => run(c)}>
              <span>{c.icon}</span><span>{c.label}</span>{c.hint && <span class="hint">{c.hint}</span>}
            </li>
          ))}
          {!list.length && <li class="muted">没有匹配的命令</li>}
        </ul>
      </div>
    </Overlay>
  );
}
