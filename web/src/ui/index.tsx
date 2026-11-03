import type { ComponentChildren, JSX } from 'preact';
import { useEffect } from 'preact/hooks';

export function Card(p: { title?: ComponentChildren; sub?: ComponentChildren; children: ComponentChildren; class?: string; style?: JSX.CSSProperties }) {
  return (
    <section class={`card ${p.class ?? ''}`} style={p.style}>
      {p.title && <h3>{p.title}{p.sub && <span class="sub">{p.sub}</span>}</h3>}
      {p.children}
    </section>
  );
}

export function Stat(p: { label: string; value: ComponentChildren; delta?: ComponentChildren; color?: string }) {
  return (
    <div class="stat">
      <span class="label">{p.label}</span>
      <span class="value" style={p.color ? { color: `var(--${p.color})` } : undefined}>{p.value}</span>
      {p.delta !== undefined && <span class="delta">{p.delta}</span>}
    </div>
  );
}

export function Bar(p: { pct: number; color?: string }) {
  const pct = Math.max(0, Math.min(100, p.pct));
  return <div class="bar"><span style={{ width: `${pct}%`, background: p.color ? `var(--${p.color})` : undefined }} /></div>;
}

export function Badge(p: { tone?: 'green' | 'red' | 'yellow' | 'blue' | 'accent'; children: ComponentChildren; title?: string }) {
  return <span class={`badge ${p.tone ?? ''}`} title={p.title}>{p.children}</span>;
}

export function Skeleton(p: { h?: number; w?: string; lines?: number }) {
  const n = p.lines ?? 1;
  return (
    <div class="col">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} class="skeleton" style={{ height: `${p.h ?? 14}px`, width: i === n - 1 && n > 1 ? '60%' : p.w ?? '100%' }} />
      ))}
    </div>
  );
}

export function Empty(p: { children: ComponentChildren }) { return <div class="empty">{p.children}</div>; }
export function ErrorBox(p: { error: unknown }) {
  return <div class="error">{p.error instanceof Error ? p.error.message : String(p.error)}</div>;
}

/** 数据加载三态：加载中 → 骨架屏；出错 → 错误框；有数据 → render。 */
export function Async<T>(p: { q: { data?: T; error?: Error; loading: boolean }; lines?: number; children: (d: T) => ComponentChildren }) {
  if (p.q.data !== undefined) return <>{p.children(p.q.data)}</>;
  if (p.q.error) return <ErrorBox error={p.q.error} />;
  return <Skeleton lines={p.lines ?? 3} />;
}

export function Overlay(p: { onClose: () => void; children: ComponentChildren }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape') p.onClose(); };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [p.onClose]);
  return <div class="overlay" onClick={(e) => { if (e.target === e.currentTarget) p.onClose(); }}>{p.children}</div>;
}

export function Drawer(p: { title: ComponentChildren; onClose: () => void; actions?: ComponentChildren; children: ComponentChildren }) {
  return (
    <Overlay onClose={p.onClose}>
      <aside class="drawer" role="dialog" aria-label={typeof p.title === 'string' ? p.title : undefined}>
        <header><h2>{p.title}</h2>{p.actions}<button class="btn ghost sm" onClick={p.onClose} aria-label="关闭">✕</button></header>
        <div class="body">{p.children}</div>
      </aside>
    </Overlay>
  );
}

export function Switch(p: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return <button type="button" role="switch" aria-checked={p.on} aria-label={p.label} class={`switch ${p.on ? 'on' : ''}`} disabled={p.disabled} onClick={() => p.onChange(!p.on)} />;
}

export function Tabs<T extends string>(p: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div class="tabs" role="tablist">
      {p.options.map((o) => (
        <button key={o.value} role="tab" aria-selected={o.value === p.value} class={o.value === p.value ? 'on' : ''} onClick={() => p.onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export interface Column<R> { key: string; label: string; num?: boolean; render?: (r: R) => ComponentChildren }
/** 表格；行数多时只渲染前 limit 行并提供「显示全部」。 */
export function Table<R extends Record<string, unknown>>(p: { rows: R[]; cols: Column<R>[]; limit?: number; rowKey?: (r: R, i: number) => string | number; showAll?: boolean; onShowAll?: () => void }) {
  const limit = p.limit ?? 200;
  const rows = p.showAll ? p.rows : p.rows.slice(0, limit);
  return (
    <div style={{ overflowX: 'auto' }}>
      <table class="table">
        <thead><tr>{p.cols.map((c) => <th key={c.key} class={c.num ? 'num' : ''}>{c.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={p.rowKey ? p.rowKey(r, i) : i}>
              {p.cols.map((c) => <td key={c.key} class={c.num ? 'num' : ''}>{c.render ? c.render(r) : String(r[c.key] ?? '—')}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {!p.showAll && p.rows.length > limit && (
        <div class="row" style={{ justifyContent: 'center', padding: '8px' }}>
          <button class="btn sm" onClick={p.onShowAll}>显示全部 {p.rows.length} 行</button>
        </div>
      )}
    </div>
  );
}
