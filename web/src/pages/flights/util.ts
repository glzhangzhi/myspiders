import type { Baggage, Filters, Trip } from './types';

export const DEFAULT_FILTERS: Filters = {
  direct_only: false, max_transfers: 1, allow_long_layover: false, long_layover_hours: 6,
  allow_recheck: false, recheck_min_layover_hours: 3, max_duration_hours: null,
};

export const fmtMin = (m: number) => { const h = Math.floor(m / 60), mm = m % 60; return mm ? `${h}h${String(mm).padStart(2, '0')}` : `${h}h`; };
export const hhmm = (s: string) => s.slice(11, 16);
export const md = (iso: string) => `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;
const WD = ['日', '一', '二', '三', '四', '五', '六'];
export const weekday = (iso: string) => WD[new Date(`${iso}T00:00:00Z`).getUTCDay()];
export const isWeekend = (iso: string) => { const d = new Date(`${iso}T00:00:00Z`).getUTCDay(); return d === 0 || d === 6; };

export function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
export const dayDiff = (a: string, b: string) => Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / 86400000);

/** 跨天提示：到达比出发晚几天就标 +N */
export function plusDays(t: Trip): string {
  const n = dayDiff(t.dep_time.slice(0, 10), t.arr_time.slice(0, 10));
  return n > 0 ? `+${n}` : '';
}

export const BAG_LABEL: Record<Baggage, string> = { confirmed: '行李直挂', inferred: '行李直挂（推断）', recheck: '需重新托运' };
export const BAG_TONE: Record<Baggage, 'green' | 'yellow' | 'red'> = { confirmed: 'green', inferred: 'yellow', recheck: 'red' };

export function layoverText(t: Trip): string {
  if (!t.layovers.length) return '直飞';
  return t.layovers.map((l) => `${l.airport} ${fmtMin(l.minutes)}${l.change_airport ? '（换机场）' : ''}`).join(' · ');
}

/** 悬浮提示的多行文本（data-tip，\n 换行） */
export function tripTip(t: Trip, cur: string): string {
  return [
    `${t.flights.join(' + ')}  ${money(t.price, cur)}`,
    `${hhmm(t.dep_time)} ${t.from} → ${hhmm(t.arr_time)}${plusDays(t)} ${t.to}，共 ${fmtMin(t.duration)}`,
    t.layovers.length ? `中转：${layoverText(t)}` : '直飞',
    BAG_LABEL[t.baggage] + (t.checked_bag ? `，${t.checked_bag}` : '，不含免费托运'),
  ].join('\n');
}

export function money(v: number, cur: string): string {
  const sym = cur === 'EUR' ? '€' : cur === 'USD' ? '$' : cur === 'CNY' ? '¥' : `${cur} `;
  return `${sym}${Math.round(v)}`;
}

/** 热力等级 0–4：按本次已出结果的价格区间线性分档，越便宜越深 */
export function heatLevel(price: number, min: number, max: number): number {
  if (max <= min) return 4;
  return 4 - Math.min(4, Math.floor(((price - min) / (max - min)) * 5));
}

// ── URL 参数 ↔ 查询状态 ──

export interface Form {
  mode: 'ow' | 'rt'; origin: string; dest: string; d1: string; d2: string; r1: string; r2: string; f: Filters;
}

export function formFromQuery(q: Record<string, string>): Form {
  const num = (v: string | undefined, d: number) => (v !== undefined && v !== '' && !Number.isNaN(Number(v)) ? Number(v) : d);
  return {
    mode: q.mode === 'rt' ? 'rt' : 'ow',
    origin: (q.from ?? '').toUpperCase(), dest: (q.to ?? '').toUpperCase(),
    d1: q.d1 ?? '', d2: q.d2 ?? '', r1: q.r1 ?? '', r2: q.r2 ?? '',
    f: {
      direct_only: q.direct === '1',
      max_transfers: q.mt === 'any' ? null : num(q.mt, DEFAULT_FILTERS.max_transfers ?? 1),
      allow_long_layover: q.longok === '1',
      long_layover_hours: num(q.lh, DEFAULT_FILTERS.long_layover_hours),
      allow_recheck: q.rck === '1',
      recheck_min_layover_hours: num(q.rh, DEFAULT_FILTERS.recheck_min_layover_hours),
      max_duration_hours: q.maxd ? num(q.maxd, 0) || null : null,
    },
  };
}

export function formToQuery(s: Form): string {
  const p = new URLSearchParams();
  p.set('mode', s.mode);
  if (s.origin) p.set('from', s.origin);
  if (s.dest) p.set('to', s.dest);
  if (s.d1) p.set('d1', s.d1);
  if (s.d2) p.set('d2', s.d2);
  if (s.mode === 'rt') { if (s.r1) p.set('r1', s.r1); if (s.r2) p.set('r2', s.r2); }
  const f = s.f, d = DEFAULT_FILTERS;
  if (f.direct_only) p.set('direct', '1');
  if (f.max_transfers !== d.max_transfers) p.set('mt', f.max_transfers === null ? 'any' : String(f.max_transfers));
  if (f.allow_long_layover) p.set('longok', '1');
  if (f.long_layover_hours !== d.long_layover_hours) p.set('lh', String(f.long_layover_hours));
  if (f.allow_recheck) p.set('rck', '1');
  if (f.recheck_min_layover_hours !== d.recheck_min_layover_hours) p.set('rh', String(f.recheck_min_layover_hours));
  if (f.max_duration_hours) p.set('maxd', String(f.max_duration_hours));
  return p.toString();
}

export function searchBody(s: Form) {
  return {
    mode: s.mode, origin: s.origin, dest: s.dest, out_from: s.d1, out_to: s.d2,
    ...(s.mode === 'rt' ? { ret_from: s.r1, ret_to: s.r2 } : {}), filters: s.f,
  };
}
