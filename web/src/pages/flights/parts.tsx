import { useState } from 'preact/hooks';
import { Badge } from '../../ui';
import type { DaySummary, Trip } from './types';
import { BAG_LABEL, BAG_TONE, fmtMin, heatLevel, hhmm, isWeekend, layoverText, md, money, plusDays, tripTip, weekday } from './util';

// ── 单程日历 ──

export function Calendar(p: {
  days: string[]; data: Record<string, DaySummary>; current: string | null; selected: string | null; cur: string;
  onPick: (d: string) => void;
}) {
  const prices = p.days.map((d) => p.data[d]).filter((s) => s?.status === 'ok' && s.best).map((s) => s!.best!.price);
  const min = Math.min(...prices), max = Math.max(...prices);
  // 周一开头；第一天前面补空格
  const lead = (new Date(`${p.days[0]}T00:00:00Z`).getUTCDay() + 6) % 7;
  return (
    <div class="fl-cal">
      {['一', '二', '三', '四', '五', '六', '日'].map((w) => <div class="wd" key={w}>周{w}</div>)}
      {Array.from({ length: lead }, (_, i) => <div class="fl-cell blank" key={`b${i}`} />)}
      {p.days.map((d) => <DayCell key={d} d={d} s={p.data[d]} cur={p.cur} min={min} max={max}
        running={p.current === d} selected={p.selected === d} onPick={p.onPick} />)}
    </div>
  );
}

function DayCell(p: { d: string; s?: DaySummary; cur: string; min: number; max: number; running: boolean; selected: boolean; onPick: (d: string) => void }) {
  const { d, s } = p;
  const head = <span class={`d ${isWeekend(d) ? 'we' : ''}`}><span>{md(d)}</span><span>{weekday(d)}</span></span>;
  if (!s) {
    return <button class={`fl-cell pending ${p.running ? 'cur' : ''}`} disabled>{head}<span class="p">{p.running ? '查询中…' : '排队'}</span></button>;
  }
  const sel = p.selected ? 'sel' : '';
  if (s.status === 'error') {
    return <button class={`fl-cell error ${sel}`} data-tip={`查询失败：${s.error}\n点开可重试`} onClick={() => p.onPick(d)}>{head}<span class="p">查询失败</span><span class="s">点开重试</span></button>;
  }
  if (s.status === 'empty' || !s.best) {
    const tip = [s.note ?? '没有符合条件的航班', ...(s.top_rejected ?? []).map((t) => `${t.flights.join('+')} ${money(t.price, p.cur)}：${t.reasons?.join('；')}`)].join('\n');
    return <button class={`fl-cell empty ${sel}`} data-tip={tip} onClick={() => p.onPick(d)}>{head}<span class="p">{s.total ? '无符合' : '无航班'}</span><span class="s">{s.total ? `${s.total} 条被排除` : ''}</span></button>;
  }
  const b = s.best;
  const lv = heatLevel(b.price, p.min, p.max);
  const tip = (s.top ?? [b]).slice(0, 3).map((t) => tripTip(t, p.cur)).join('\n──\n')
    + `\n\n符合 ${s.matched} / 共 ${s.total} 条${s.cached ? '（缓存）' : ''}`;
  return (
    <button class={`fl-cell h${lv} ${b.price === p.min ? 'low' : ''} ${sel}`} data-tip={tip} onClick={() => p.onPick(d)}>
      {head}
      <span class="p">{money(b.price, p.cur)}</span>
      <span class="s">{fmtMin(b.duration)} · {b.stops ? `${b.layovers.map((l) => l.airport).join('/')}转` : '直飞'}</span>
    </button>
  );
}

// ── 往返矩阵 ──

export interface Cell { out: string; ret: string; status: 'ok' | 'empty' | 'error' | 'pending' | 'invalid'; price?: number }

export function buildCells(outDays: string[], retDays: string[], out: Record<string, DaySummary>, ret: Record<string, DaySummary>): Cell[][] {
  // 与 service.build_matrix 同一套规则（失败盖过无航班），前端边查边画
  return outDays.map((o) => retDays.map((r): Cell => {
    if (r < o) return { out: o, ret: r, status: 'invalid' };
    const a = out[o], b = ret[r];
    const sts: string[] = [a?.status ?? 'pending', b?.status ?? 'pending'];
    if (sts.includes('error')) return { out: o, ret: r, status: 'error' };
    if (sts.includes('empty')) return { out: o, ret: r, status: 'empty' };
    if (sts.includes('pending')) return { out: o, ret: r, status: 'pending' };
    return { out: o, ret: r, status: 'ok', price: a!.best!.price + b!.best!.price };
  }));
}

export function Matrix(p: {
  outDays: string[]; retDays: string[]; out: Record<string, DaySummary>; ret: Record<string, DaySummary>;
  cur: string; selected: [string, string] | null; onPick: (o: string, r: string) => void;
}) {
  const cells = buildCells(p.outDays, p.retDays, p.out, p.ret);
  const prices = cells.flat().filter((c) => c.status === 'ok').map((c) => c.price!);
  const min = Math.min(...prices), max = Math.max(...prices);
  const legTip = (s?: DaySummary) => !s ? '还没查' : s.status === 'error' ? `查询失败：${s.error}` : s.best ? tripTip(s.best, p.cur) : (s.note ?? '无符合条件');
  const legText = (s?: DaySummary) => !s ? '…' : s.status === 'error' ? '失败' : s.best ? money(s.best.price, p.cur) : '无';
  return (
    <div class="fl-mx-wrap">
      <table class="fl-mx">
        <thead>
          <tr>
            <th class="corner">去程 ↓ / 回程 →</th>
            <th />
            {p.retDays.map((r) => <th key={r} class={isWeekend(r) ? 'we' : ''}>{md(r)} {weekday(r)}</th>)}
          </tr>
          <tr class="legrow">
            <td /><td>单程</td>
            {p.retDays.map((r) => <td key={r} class={p.ret[r]?.status === 'error' ? 'err' : ''} data-tip={`回程 ${r}\n${legTip(p.ret[r])}`}>{legText(p.ret[r])}</td>)}
          </tr>
        </thead>
        <tbody>
          {cells.map((row, i) => {
            const o = p.outDays[i];
            return (
              <tr key={o}>
                <th class={isWeekend(o) ? 'we' : ''}>{md(o)} {weekday(o)}</th>
                <td class="legrow" style="font-size:11px;text-align:center" data-tip={`去程 ${o}\n${legTip(p.out[o])}`}>{legText(p.out[o])}</td>
                {row.map((c) => {
                  const sel = p.selected?.[0] === c.out && p.selected?.[1] === c.ret ? 'sel' : '';
                  if (c.status === 'invalid') return <td key={c.ret}><span class="fl-cell invalid" /></td>;
                  if (c.status === 'pending') return <td key={c.ret}><button class="fl-cell pending" disabled><span class="p">…</span></button></td>;
                  if (c.status === 'error') return <td key={c.ret}><button class={`fl-cell error ${sel}`} data-tip="有一边查询失败，点开重试" onClick={() => p.onPick(c.out, c.ret)}><span class="p">失败</span></button></td>;
                  if (c.status === 'empty') return <td key={c.ret}><button class={`fl-cell empty ${sel}`} data-tip="有一边没有符合条件的航班" onClick={() => p.onPick(c.out, c.ret)}><span class="p">无</span></button></td>;
                  const a = p.out[c.out].best!, b = p.ret[c.ret].best!;
                  const tip = `${c.out} → ${c.ret}（${Math.round((Date.parse(c.ret) - Date.parse(c.out)) / 86400000)} 天）\n两张单程合计 ${money(c.price!, p.cur)}\n\n去：${tripTip(a, p.cur)}\n\n回：${tripTip(b, p.cur)}`;
                  return (
                    <td key={c.ret}>
                      <button class={`fl-cell h${heatLevel(c.price!, min, max)} ${c.price === min ? 'low' : ''} ${sel}`} data-tip={tip} onClick={() => p.onPick(c.out, c.ret)}>
                        <span class="p">{money(c.price!, p.cur)}</span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── 行程明细 ──

export function TripRow(p: { t: Trip; cur: string; link?: string; bad?: boolean }) {
  const t = p.t;
  return (
    <div class={`fl-trip ${p.bad ? 'bad' : ''}`}>
      <div class="price">{money(t.price, p.cur)}</div>
      <div class="main">
        <div class="times">
          {hhmm(t.dep_time)} {t.from} → {hhmm(t.arr_time)}<sup>{plusDays(t)}</sup> {t.to}
          <span class="muted small" style="font-weight:400"> · {fmtMin(t.duration)} · {t.flights.join(' + ')}</span>
        </div>
        <div class="meta">
          <span>{t.stops ? `中转 ${layoverText(t)}` : '直飞'}</span>
          <Badge tone={BAG_TONE[t.baggage]}>{BAG_LABEL[t.baggage]}</Badge>
          <span>{t.checked_bag ?? '不含免费托运'}</span>
          {t.codeshare && <span>含代码共享</span>}
        </div>
        {t.reasons && <div class="why">{t.reasons.join('；')}</div>}
      </div>
      {p.link && <div class="act"><a class="btn sm" href={p.link} target="_blank" rel="noopener noreferrer">Trip ↗</a></div>}
    </div>
  );
}

type SortBy = 'price' | 'duration';
export function TripList(p: { matched: Trip[]; rejected: Trip[]; cur: string; link: string; limit?: number }) {
  const [sort, setSort] = useState<SortBy>('price');
  const [all, setAll] = useState(false);
  const cmp = (a: Trip, b: Trip) => sort === 'price' ? a.price - b.price || a.duration - b.duration : a.duration - b.duration || a.price - b.price;
  const ok = [...p.matched].sort(cmp);
  const bad = [...p.rejected].sort(cmp);
  const lim = p.limit ?? 12;
  return (
    <div class="col">
      <div class="row small muted">
        <span>符合 {ok.length} 条</span>
        <span class="fl-seg">
          <button class={sort === 'price' ? 'on' : ''} onClick={() => setSort('price')}>按价格</button>
          <button class={sort === 'duration' ? 'on' : ''} onClick={() => setSort('duration')}>按时长</button>
        </span>
      </div>
      <div class="fl-list">
        {ok.length ? (all ? ok : ok.slice(0, lim)).map((t) => <TripRow key={t.key} t={t} cur={p.cur} link={p.link} />) : <div class="muted small">没有符合条件的行程</div>}
      </div>
      {!all && ok.length > lim && <button class="btn sm ghost" onClick={() => setAll(true)}>再显示 {ok.length - lim} 条</button>}
      {bad.length > 0 && (
        <details class="fl-fold">
          <summary>被条件排除的 {bad.length} 条（含原因）</summary>
          <div class="fl-list">{bad.map((t) => <TripRow key={t.key} t={t} cur={p.cur} link={p.link} bad />)}</div>
        </details>
      )}
    </div>
  );
}
