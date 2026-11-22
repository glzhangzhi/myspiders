import { useEffect, useRef, useState } from 'preact/hooks';
import { useLocation } from 'preact-iso';
import { api } from '../lib/api';
import { fmtDE } from '../lib/format';
import { useQuery } from '../lib/query';
import { toastError } from '../lib/toast';
import { Badge, Bar, Card, Empty, Skeleton } from '../ui';
import './flights/flights.css';
import { Calendar, Matrix, TripList, TripRow } from './flights/parts';
import { postStream } from './flights/stream';
import type { Config, DayDetail, DaySummary, DoneEv, Filters, Review, RoundtripRes, StartEv } from './flights/types';
import { addDays, dayDiff, formFromQuery, formToQuery, md, money, searchBody, weekday, type Form } from './flights/util';

/**
 * Trip.com 机票面板：选航线 + 日期区间 + 硬条件，后端逐天查（全局串行、限速），查完一天推一天。
 * 单程 → 日历；往返 → 矩阵（格 = 去程最低 + 回程最低，两张单程相加），点格再查一次真往返价对比。
 * 筛选全是后端硬规则（app/features/flights/filters.py）；DeepSeek 只在查完后写点评。约定见 docs/flights.md。
 */

interface Run {
  start: StartEv | null;
  out: Record<string, DaySummary>;
  ret: Record<string, DaySummary>;
  done: DoneEv | null;
  requests: number;
  error: string | null;
  running: boolean;
}
const EMPTY_RUN: Run = { start: null, out: {}, ret: {}, done: null, requests: 0, error: null, running: false };

export function FlightsPage() {
  const loc = useLocation();
  const cfg = useQuery<Config>('/api/flights/config', { staleMs: 60_000 });
  const [form, setForm] = useState<Form>(() => withDefaults(formFromQuery(loc.query)));
  const [run, setRun] = useState<Run>(EMPTY_RUN);
  const [sel, setSel] = useState<{ out: string; ret?: string } | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  const cur = run.start?.currency ?? cfg.data?.currency ?? 'EUR';

  async function submit() {
    const err = validateForm(form, cfg.data);
    if (err) { toastError(err); return; }
    loc.route(`/dashboard/flights?${formToQuery(form)}`, true);
    abort.current?.abort();
    const ac = new AbortController();
    abort.current = ac;
    setSel(null);
    setRun({ ...EMPTY_RUN, running: true });
    const snapshot = form;
    try {
      await postStream('/api/flights/search', searchBody(snapshot), {
        onEvent: (ev, data) => {
          if (ev === 'start') setRun((r) => ({ ...r, start: data as StartEv }));
          else if (ev === 'day') {
            const d = data as DaySummary & { requests_so_far: number };
            setRun((r) => ({ ...r, requests: d.requests_so_far, [d.leg]: { ...r[d.leg], [d.date]: d } }));
          } else if (ev === 'done') setRun((r) => ({ ...r, done: data as DoneEv, running: false }));
          else if (ev === 'busy' || ev === 'fail') setRun((r) => ({ ...r, error: (data as { error: string }).error, running: false }));
        },
      }, ac.signal);
      setRun((r) => (r.running ? { ...r, running: false, error: r.done ? null : '连接中断，查询没有完成' } : r));
    } catch (e) {
      if (ac.signal.aborted) return;
      setRun((r) => ({ ...r, running: false, error: e instanceof Error ? e.message : String(e) }));
    }
  }

  function stop() {
    abort.current?.abort();
    setRun((r) => ({ ...r, running: false, error: '已手动停止（已查完的天数保留）' }));
  }

  /** 明细接口重查成功后，把那一天的格子也更新掉（失败重试的路径） */
  function patchDay(leg: 'out' | 'ret', d: DayDetail) {
    setRun((r) => {
      const old = r[leg][d.date];
      if (!old) return r;
      const best = d.matched[0] ?? null;
      const next: DaySummary = {
        ...old, status: best ? 'ok' : 'empty', error: undefined, best, top: d.matched.slice(0, 5),
        matched: d.matched.length, rejected: d.rejected.length, total: d.matched.length + d.rejected.length,
        top_rejected: d.rejected.slice(0, 3), note: best ? undefined : (d.rejected.length ? `${d.rejected.length} 条全被条件排除` : 'Trip 这天没有航班'),
        fetched_at: d.fetched_at, cached: d.cached,
      };
      return { ...r, [leg]: { ...r[leg], [d.date]: next } };
    });
  }

  const st = run.start;
  const doneDays = Object.keys(run.out).length + Object.keys(run.ret).length;
  const currentDay = run.running && st ? nextPending(st, run) : null;

  return (
    <div class="page">
      <QueryBar form={form} setForm={setForm} cfg={cfg.data} running={run.running} onSubmit={submit} onStop={stop} />

      {(run.running || run.start || run.error) && (
        <Card>
          <div class="col">
            <div class="row">
              <strong>{st ? `${st.origin} → ${st.dest}` : '准备中…'}</strong>
              {st && <span class="muted">{st.mode === 'rt' ? '往返' : '单程'}</span>}
              <span class="muted">已查 {doneDays} / {st?.total_days ?? '…'} 天 · 已发 {run.requests} 次请求</span>
              {run.done && <span class="muted">· 用时 {run.done.stats.elapsed_s}s{run.done.stats.refills ? ` · 补查 ${run.done.stats.refills} 天` : ''}{run.done.stats.cached_days ? ` · 缓存 ${run.done.stats.cached_days} 天` : ''}</span>}
              {run.done && run.done.stats.errors > 0 && <Badge tone="red">{run.done.stats.errors} 天查询失败</Badge>}
            </div>
            {st && <Bar pct={(doneDays / Math.max(1, st.total_days)) * 100} />}
            {run.error && <div class="error">{run.error}</div>}
            {run.done && (
              <div class="small muted">
                查询时间 {fmtDE(run.done.newest_fetched_at)}
                {run.done.oldest_fetched_at && run.done.oldest_fetched_at !== run.done.newest_fetched_at && `（最早的缓存 ${fmtDE(run.done.oldest_fetched_at)}）`}
                {' · '}价格以 Trip 详情页为准，同一航班几分钟内也可能浮动几十{cur === 'EUR' ? '欧' : ` ${cur}`}
              </div>
            )}
          </div>
        </Card>
      )}

      {st && st.mode === 'ow' && (
        <Card title="出发日历" sub={<Legend />}>
          <Calendar days={st.out_days} data={run.out} current={currentDay} selected={sel?.out ?? null} cur={cur} onPick={(d) => setSel({ out: d })} />
        </Card>
      )}
      {st && st.mode === 'rt' && (
        <Card title="往返矩阵" sub={<span>格内 = 去程单程最低 + 回程单程最低（各自按条件筛选），点格子查真往返价</span>}>
          <Matrix outDays={st.out_days} retDays={st.ret_days} out={run.out} ret={run.ret} cur={cur}
            selected={sel?.ret ? [sel.out, sel.ret] : null} onPick={(o, r) => setSel({ out: o, ret: r })} />
          <div style="margin-top:8px"><Legend /></div>
        </Card>
      )}

      {st && sel && !sel.ret && (
        <DayPanel key={sel.out} origin={st.origin} dest={st.dest} date={sel.out} filters={form.f} cur={cur}
          onClose={() => setSel(null)} onLoaded={(d) => patchDay('out', d)} title={`${md(sel.out)} 周${weekday(sel.out)} · ${st.origin} → ${st.dest}`} />
      )}
      {st && sel?.ret && (
        <RoundtripPanel key={`${sel.out}_${sel.ret}`} st={st} out={sel.out} ret={sel.ret} filters={form.f} cur={cur}
          outSum={run.out[sel.out]} retSum={run.ret[sel.ret]} onClose={() => setSel(null)} onPatch={patchDay} />
      )}

      {run.done && st?.review_enabled && <ReviewCard key={run.done.query_id} queryId={run.done.query_id} />}

      {!run.start && !run.running && !run.error && (
        <Card><Empty>填好航线和日期，点「查询」。后端逐天串行查询（每次请求间隔 1–2 秒），11 天大约 1–2 分钟；同一天 30 分钟内重复查询走缓存。</Empty></Card>
      )}
    </div>
  );
}

function nextPending(st: StartEv, run: Run): string | null {
  for (const d of st.out_days) if (!run.out[d]) return d;
  for (const d of st.ret_days) if (!run.ret[d]) return `ret:${d}`;
  return null;
}

function withDefaults(f: Form): Form {
  // 没带参数打开时给一个合理的默认区间：两周后起 7 天
  if (f.d1) return f;
  const today = new Date().toISOString().slice(0, 10);
  const d1 = addDays(today, 14);
  return { ...f, d1, d2: addDays(d1, 6), r1: addDays(d1, 10), r2: addDays(d1, 16) };
}

function validateForm(f: Form, cfg?: Config): string | null {
  if (!/^[A-Z]{3}$/.test(f.origin) || !/^[A-Z]{3}$/.test(f.dest)) return '出发地和目的地要填 3 字母代码（可输入城市名从下拉里选）';
  if (f.origin === f.dest) return '出发地和目的地相同';
  if (!f.d1 || !f.d2) return '出发日期没填全';
  const n = dayDiff(f.d1, f.d2) + 1;
  if (n < 1) return '出发区间结束早于开始';
  if (f.mode === 'ow' && n > (cfg?.max_days_oneway ?? 31)) return `单程最多查 ${cfg?.max_days_oneway ?? 31} 天，现在是 ${n} 天`;
  if (f.mode === 'rt') {
    const cap = cfg?.max_days_roundtrip ?? 15;
    if (n > cap) return `往返的去程区间最多 ${cap} 天，现在是 ${n} 天`;
    if (!f.r1 || !f.r2) return '回程日期没填全';
    const m = dayDiff(f.r1, f.r2) + 1;
    if (m < 1) return '回程区间结束早于开始';
    if (m > cap) return `往返的回程区间最多 ${cap} 天，现在是 ${m} 天`;
  }
  return null;
}

function Legend() {
  return (
    <span class="fl-legend">
      <span><i class="h4" />便宜</span><span><i class="h1" />贵</span>
      <span><i style="background:var(--bg)" />无航班/全被排除</span>
      <span><i style="background:var(--red-bg);border-color:var(--red)" />查询失败</span>
      <span><i style="border-style:dashed" />还在查</span>
    </span>
  );
}

// ── 查询栏 ──

function QueryBar(p: { form: Form; setForm: (f: Form) => void; cfg?: Config; running: boolean; onSubmit: () => void; onStop: () => void }) {
  const { form: f, setForm } = p;
  const set = (patch: Partial<Form>) => setForm({ ...f, ...patch });
  const setF = (patch: Partial<Filters>) => setForm({ ...f, f: { ...f.f, ...patch } });
  const nOut = f.d1 && f.d2 ? dayDiff(f.d1, f.d2) + 1 : 0;
  const nRet = f.r1 && f.r2 ? dayDiff(f.r1, f.r2) + 1 : 0;
  const reqs = f.mode === 'rt' ? nOut + nRet : nOut;
  return (
    <Card>
      <form class="col" style="gap:12px" onSubmit={(e) => { e.preventDefault(); p.onSubmit(); }}>
        <div class="fl-bar">
          <span class="fl-seg">
            <button type="button" class={f.mode === 'ow' ? 'on' : ''} onClick={() => set({ mode: 'ow' })}>单程</button>
            <button type="button" class={f.mode === 'rt' ? 'on' : ''} onClick={() => set({ mode: 'rt' })}>往返</button>
          </span>
          <PlaceInput label="出发" value={f.origin} onChange={(v) => set({ origin: v })} />
          <button type="button" class="btn ghost sm" title="交换出发地和目的地" onClick={() => set({ origin: f.dest, dest: f.origin })}>⇄</button>
          <PlaceInput label="到达" value={f.dest} onChange={(v) => set({ dest: v })} />
          <label>{f.mode === 'rt' ? '去程从' : '出发从'}<input class="input date" type="date" value={f.d1} onInput={(e) => set({ d1: e.currentTarget.value })} /></label>
          <label>到<input class="input date" type="date" value={f.d2} min={f.d1} onInput={(e) => set({ d2: e.currentTarget.value })} /></label>
          {f.mode === 'rt' && <>
            <label>回程从<input class="input date" type="date" value={f.r1} min={f.d1} onInput={(e) => set({ r1: e.currentTarget.value })} /></label>
            <label>到<input class="input date" type="date" value={f.r2} min={f.r1} onInput={(e) => set({ r2: e.currentTarget.value })} /></label>
          </>}
          {p.running
            ? <button type="button" class="btn" onClick={p.onStop}>停止</button>
            : <button type="submit" class="btn primary" disabled={p.cfg?.busy}>查询</button>}
          <span class="small muted">{reqs > 0 ? `将查 ${reqs} 天（≥ ${reqs} 次请求）` : ''}</span>
        </div>
        <div class="fl-filters">
          <label class="opt"><input type="checkbox" checked={f.f.direct_only} onChange={(e) => setF({ direct_only: e.currentTarget.checked })} />只看直飞</label>
          <label class="opt">最多中转
            <select class="input num" disabled={f.f.direct_only} value={f.f.max_transfers === null ? 'any' : String(f.f.max_transfers)}
              onChange={(e) => setF({ max_transfers: e.currentTarget.value === 'any' ? null : Number(e.currentTarget.value) })}>
              <option value="0">0</option><option value="1">1</option><option value="2">2</option><option value="any">不限</option>
            </select>次
          </label>
          <label class="opt"><input type="checkbox" checked={f.f.allow_long_layover} onChange={(e) => setF({ allow_long_layover: e.currentTarget.checked })} />
            接受长时间中转<span class="muted">（否则排除 &gt;</span>
            <input class="input num" type="number" min="1" max="48" step="0.5" value={f.f.long_layover_hours} disabled={f.f.allow_long_layover}
              onInput={(e) => setF({ long_layover_hours: Number(e.currentTarget.value) || 6 })} /><span class="muted">h）</span>
          </label>
          <label class="opt"><input type="checkbox" checked={f.f.allow_recheck} onChange={(e) => setF({ allow_recheck: e.currentTarget.checked })} />
            接受需重新托运<span class="muted">（中转 ≥</span>
            <input class="input num" type="number" min="0" max="48" step="0.5" value={f.f.recheck_min_layover_hours} disabled={!f.f.allow_recheck}
              onInput={(e) => setF({ recheck_min_layover_hours: Number(e.currentTarget.value) || 0 })} /><span class="muted">h）</span>
          </label>
          <label class="opt">总时长 ≤
            <input class="input num" type="number" min="1" max="96" placeholder="不限" value={f.f.max_duration_hours ?? ''}
              onInput={(e) => setF({ max_duration_hours: e.currentTarget.value ? Number(e.currentTarget.value) : null })} />h
          </label>
        </div>
        <div class="small muted">1 位成人 · 经济舱 · {p.cfg?.currency ?? 'EUR'}。行李「推断直挂」= 同一张票但 Trip 没给直挂标记，下单前在详情页确认。</div>
      </form>
    </Card>
  );
}

interface Place { code: string; name: string; country: string; kind: 'city' | 'airport' }
function PlaceInput(p: { label: string; value: string; onChange: (v: string) => void }) {
  const [text, setText] = useState(p.value);
  const [opts, setOpts] = useState<Place[]>([]);
  const timer = useRef<number>();
  const id = `fl-pl-${p.label}`;
  useEffect(() => setText(p.value), [p.value]);
  function onInput(v: string) {
    setText(v);
    const code = v.trim().toUpperCase();
    // 选中下拉项时 value 就是代码；手输 3 个字母也直接当代码
    if (/^[A-Z]{3}$/.test(code)) p.onChange(code);
    clearTimeout(timer.current);
    if (v.trim().length < 2 || opts.some((o) => o.code === code)) return;
    timer.current = window.setTimeout(async () => {
      try { setOpts((await api<{ items: Place[] }>(`/api/flights/places?q=${encodeURIComponent(v.trim())}`)).items); } catch { /* 补全失败不打扰 */ }
    }, 300);
  }
  return (
    <label>{p.label}
      <input class="input code" list={id} value={text} placeholder="TPE / 台北" onInput={(e) => onInput(e.currentTarget.value)}
        onBlur={() => setText(p.value)} />
      <datalist id={id}>
        {opts.map((o) => <option key={`${o.kind}${o.code}`} value={o.code}>{`${o.name}${o.kind === 'city' ? '（全部机场）' : ''} · ${o.country}`}</option>)}
      </datalist>
    </label>
  );
}

// ── 单日明细 ──

function useDetail(origin: string, dest: string, date: string, filters: Filters, onLoaded?: (d: DayDetail) => void) {
  const [state, setState] = useState<{ data?: DayDetail; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    let alive = true;
    api<DayDetail>('/api/flights/day', { body: { origin, dest, date, filters } })
      .then((d) => { if (alive) { setState({ data: d, loading: false }); onLoaded?.(d); } })
      .catch((e) => alive && setState({ error: e instanceof Error ? e.message : String(e), loading: false }));
    return () => { alive = false; };
  }, [origin, dest, date]);
  return state;
}

function DayPanel(p: { origin: string; dest: string; date: string; filters: Filters; cur: string; title: string; onClose: () => void; onLoaded: (d: DayDetail) => void }) {
  const s = useDetail(p.origin, p.dest, p.date, p.filters, p.onLoaded);
  return (
    <Card title={p.title} sub={<span class="row">
      {s.data && <span>{s.data.cached ? '缓存' : '刚查'} · {fmtDE(s.data.fetched_at)}</span>}
      {s.data && <a class="btn sm" href={s.data.link} target="_blank" rel="noopener noreferrer">在 Trip 打开 ↗</a>}
      <button class="btn sm ghost" onClick={p.onClose}>✕</button></span>}>
      <DetailBody s={s} cur={p.cur} />
    </Card>
  );
}

function DetailBody(p: { s: { data?: DayDetail; error?: string; loading: boolean }; cur: string }) {
  if (p.s.loading) return <Skeleton lines={4} h={40} />;
  if (p.s.error) return <div class="error">{p.s.error}</div>;
  const d = p.s.data!;
  return (
    <div class="col">
      {d.warnings.map((w) => <div class="error" key={w}>{w}</div>)}
      <TripList matched={d.matched} rejected={d.rejected} cur={p.cur} link={d.link} />
    </div>
  );
}

// ── 往返格明细 ──

function RoundtripPanel(p: {
  st: StartEv; out: string; ret: string; filters: Filters; cur: string; outSum?: DaySummary; retSum?: DaySummary;
  onClose: () => void; onPatch: (leg: 'out' | 'ret', d: DayDetail) => void;
}) {
  const { st } = p;
  const outD = useDetail(st.origin, st.dest, p.out, p.filters, (d) => p.onPatch('out', d));
  const retD = useDetail(st.dest, st.origin, p.ret, p.filters, (d) => p.onPatch('ret', d));
  const [rt, setRt] = useState<{ data?: RoundtripRes; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    let alive = true;
    api<RoundtripRes>('/api/flights/roundtrip', { body: { origin: st.origin, dest: st.dest, out: p.out, ret: p.ret, filters: p.filters } })
      .then((d) => alive && setRt({ data: d, loading: false }))
      .catch((e) => alive && setRt({ error: e instanceof Error ? e.message : String(e), loading: false }));
    return () => { alive = false; };
  }, [p.out, p.ret]);

  const ob = outD.data?.matched[0] ?? p.outSum?.best ?? null;
  const rb = retD.data?.matched[0] ?? p.retSum?.best ?? null;
  const twoOw = ob && rb ? ob.price + rb.price : null;
  const real = rt.data?.best?.price ?? null;
  const win = twoOw !== null && real !== null ? (real < twoOw ? 'rt' : 'ow') : null;
  const nights = dayDiff(p.out, p.ret);
  return (
    <Card title={`${md(p.out)} 周${weekday(p.out)} 去 · ${md(p.ret)} 周${weekday(p.ret)} 回（${nights} 天）`}
      sub={<button class="btn sm ghost" onClick={p.onClose}>✕</button>}>
      <div class="col" style="gap:14px">
        <div class="fl-compare">
          <div class={`box ${win === 'ow' ? 'win' : ''}`}>
            <span class="muted small">两张单程 {win === 'ow' && <Badge tone="accent">更便宜</Badge>}</span>
            <span class="v">{twoOw !== null ? money(twoOw, p.cur) : '—'}</span>
            <span class="small muted">{ob && rb ? `${ob.flights.join('+')} ${money(ob.price, p.cur)} + ${rb.flights.join('+')} ${money(rb.price, p.cur)}` : '有一边没有符合条件的航班'}</span>
            <span class="small muted">两段都按条件筛过；分开买，误机不受保护</span>
          </div>
          <div class={`box ${win === 'rt' ? 'win' : ''}`}>
            <span class="muted small">真往返（Trip 往返票） {win === 'rt' && <Badge tone="accent">更便宜</Badge>}</span>
            <span class="v">{rt.loading ? '查询中…' : real !== null ? money(real, p.cur) : '—'}</span>
            {rt.error && <span class="small" style="color:var(--red)">{rt.error}</span>}
            {rt.data && !rt.data.best && <span class="small muted">去程段没有符合条件的往返组合（共 {rt.data.total} 条）</span>}
            {rt.data?.best && <span class="small muted">去程 {rt.data.best.flights.join('+')}；{rt.data.note}</span>}
            {rt.data && <a class="btn sm" style="align-self:flex-start" href={rt.data.link} target="_blank" rel="noopener noreferrer">在 Trip 打开往返 ↗</a>}
          </div>
        </div>
        {rt.data && rt.data.matched.length > 1 && (
          <details class="fl-fold">
            <summary>真往返的其他去程选项（{rt.data.matched_count} 条符合，价格为往返总价）</summary>
            <div class="fl-list">{rt.data.matched.map((t) => <TripRow key={t.key} t={t} cur={p.cur} link={rt.data!.link} />)}</div>
          </details>
        )}
        <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(340px,1fr))">
          <div class="col">
            <strong>去程 {st.origin} → {st.dest} · {md(p.out)}</strong>
            <DetailBody s={outD} cur={p.cur} />
          </div>
          <div class="col">
            <strong>回程 {st.dest} → {st.origin} · {md(p.ret)}</strong>
            <DetailBody s={retD} cur={p.cur} />
          </div>
        </div>
      </div>
    </Card>
  );
}

// ── DeepSeek 点评 ──

function ReviewCard(p: { queryId: string }) {
  const [s, setS] = useState<{ data?: Review; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    let alive = true;
    api<Review>('/api/flights/review', { body: { query_id: p.queryId } })
      .then((d) => alive && setS({ data: d, loading: false }))
      .catch((e) => alive && setS({ error: e instanceof Error ? e.message : String(e), loading: false }));
    return () => { alive = false; };
  }, [p.queryId]);
  return (
    <Card title="DeepSeek 点评" sub="只基于上面筛好的结果，不参与筛选">
      {s.loading && <Skeleton lines={3} />}
      {s.error && <div class="error">{s.error}</div>}
      {s.data && (
        <div class="fl-review">
          <p>{s.data.summary}</p>
          {s.data.picks.length > 0 && <div class="col" style="gap:4px">
            {s.data.picks.map((x) => <div key={x.label}><Badge tone="accent">{x.label}</Badge> <span class="small">{x.why}</span></div>)}
          </div>}
        </div>
      )}
    </Card>
  );
}
