import { useEffect, useRef } from 'preact/hooks';
import type uPlot from 'uplot';

export interface Series { label: string; color: string; values: (number | null)[] }

function cssVar(name: string) { return getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim(); }

/** 时间序列折线图（uPlot 按需加载，首屏不带它）。xs 为 epoch 秒。 */
export function LineChart(p: { xs: number[]; series: Series[]; height?: number; yFmt?: (v: number) => string }) {
  const el = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let plot: uPlot | undefined; let ro: ResizeObserver | undefined; let dead = false;
    (async () => {
      const [{ default: UPlot }] = await Promise.all([import('uplot'), import('uplot/dist/uPlot.min.css')]);
      if (dead || !el.current) return;
      const grid = { stroke: cssVar('surface2'), width: 1 };
      const axis = { stroke: cssVar('text-dim'), grid, ticks: { stroke: cssVar('border'), width: 1 } };
      plot = new UPlot({
        width: el.current.clientWidth, height: p.height ?? 220,
        legend: { show: p.series.length > 1 },
        cursor: { drag: { x: true, y: false } },
        scales: { x: { time: true } },
        axes: [axis, { ...axis, values: p.yFmt ? (_u: uPlot, vs: number[]) => vs.map((v) => p.yFmt!(v)) : undefined }],
        series: [{}, ...p.series.map((s) => ({ label: s.label, stroke: cssVar(s.color) || s.color, width: 1.6, points: { show: false } }))],
      }, [p.xs, ...p.series.map((s) => s.values)] as uPlot.AlignedData, el.current);
      ro = new ResizeObserver(() => { if (el.current && plot) plot.setSize({ width: el.current.clientWidth, height: p.height ?? 220 }); });
      ro.observe(el.current);
    })();
    return () => { dead = true; ro?.disconnect(); plot?.destroy(); };
  }, [p.xs, p.series, p.height]);
  return <div ref={el} style={{ width: '100%', minHeight: `${p.height ?? 220}px` }} />;
}

/** 纯 CSS 柱状图（几十根以内足够，不必加载图表库）。悬浮提示用 data-tip（即时），不用 title（约 1 秒延迟）。 */
export function Bars(p: { data: { label: string; value: number }[]; height?: number; color?: string; fmt?: (v: number) => string }) {
  const max = Math.max(1, ...p.data.map((d) => d.value));
  const h = p.height ?? 140;
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: '2px', height: `${h}px` }}>
      {p.data.map((d) => (
        <div key={d.label} data-tip={`${d.label}：${p.fmt ? p.fmt(d.value) : d.value}`}
          style={{ flex: 1, minWidth: '3px', height: `${(d.value / max) * 100}%`, background: `var(--${p.color ?? 'accent'})`, borderRadius: '3px 3px 0 0', opacity: d.value ? 1 : 0.25 }} />
      ))}
    </div>
  );
}
