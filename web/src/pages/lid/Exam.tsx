import { useEffect, useMemo, useState } from 'preact/hooks';
import { api } from '../../lib/api';
import { toastError } from '../../lib/toast';
import { LineChart } from '../../ui/chart';
import { QuestionCard } from './Card';
import { byId, loadLid, progressOf, zhMode } from './data';
import { showZh } from './logic';

interface ExamStart { id: string; qids: string[]; started_at: string; deadline: string; duration_min: number; pass_mark: number }
interface ExamItem { qid: string; chosen: number | null; answer: number; correct: boolean }
interface ExamResult { id: string; score: number; total: number; passed: boolean; items: ExamItem[] }
export interface ExamRow { id: string; started_at: string; score: number; total: number; passed: boolean }

const IDENTITY = [0, 1, 2, 3];
const KEY = 'lid-exam-draft';
interface Draft { exam: ExamStart; answers: (number | null)[]; flags: boolean[] }

function mmss(ms: number) {
  const s = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** 模拟考：只显示德语、官方选项顺序、交卷前不显示对错。草稿存 localStorage，刷新不丢。 */
export function Exam(p: { onExit: () => void }) {
  const [draft, setDraft] = useState<Draft | null>(() => {
    try { const d = JSON.parse(localStorage.getItem(KEY) || 'null') as Draft | null; return d && Date.parse(d.exam.deadline) > Date.now() - 3600_000 ? d : null; } catch { return null; }
  });
  const [result, setResult] = useState<ExamResult | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { if (draft) localStorage.setItem(KEY, JSON.stringify(draft)); else localStorage.removeItem(KEY); }, [draft]);

  const start = async () => {
    setBusy(true);
    try {
      const e = await api<ExamStart>('/api/lid/exam/start', { body: {} });
      setDraft({ exam: e, answers: e.qids.map(() => null), flags: e.qids.map(() => false) });
    } catch (x) { toastError(x); } finally { setBusy(false); }
  };

  const submit = async (d: Draft) => {
    if (busy) return;
    setBusy(true);
    try {
      const r = await api<ExamResult>(`/api/lid/exam/${d.exam.id}/finish`, { body: { answers: d.answers } });
      setResult(r); setDraft(null);
      loadLid().catch(() => {});
    } catch (x) { toastError(x); } finally { setBusy(false); }
  };

  if (result) return <ExamReview r={result} onExit={p.onExit} />;
  if (draft) return <ExamRun d={draft} setD={setDraft} submit={submit} busy={busy} onAbort={() => { setDraft(null); p.onExit(); }} />;
  return (
    <div class="lid-session">
      <section class="card">
        <h3>📋 模拟考</h3>
        <p class="muted" style={{ margin: 0 }}>33 题（30 道通用 + 3 道黑森州），60 分钟，答对 17 题及格。只显示德语、选项按官方顺序，交卷前不显示对错。时间到自动交卷。</p>
      </section>
      <ExamHistory />
      <div class="lid-actions">
        <button class="btn ghost lid-tap" onClick={p.onExit}>返回</button>
        <button class="btn primary lid-big" disabled={busy} onClick={start}>开始模拟考</button>
      </div>
    </div>
  );
}

function ExamRun(p: { d: Draft; setD: (d: Draft) => void; submit: (d: Draft) => void; busy: boolean; onAbort: () => void }) {
  const { d } = p;
  const [i, setI] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [gridOpen, setGridOpen] = useState(() => !window.matchMedia('(max-width: 760px)').matches);
  const deadline = Date.parse(d.exam.deadline);
  const left = deadline - now;
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  useEffect(() => { if (left <= 0 && !p.busy) p.submit(d); }, [left <= 0]);

  const q = byId.value.get(d.exam.qids[i]);
  const answered = d.answers.filter((a) => a !== null).length;
  const set = (patch: Partial<Draft>) => p.setD({ ...d, ...patch });
  const choose = (oi: number) => { const a = [...d.answers]; a[i] = oi; set({ answers: a }); };
  const flag = () => { const f = [...d.flags]; f[i] = !f[i]; set({ flags: f }); };
  const go = (k: number) => { setI(Math.max(0, Math.min(d.exam.qids.length - 1, k))); window.scrollTo({ top: 0 }); };
  const trySubmit = () => {
    const open = d.exam.qids.length - answered;
    if (confirm(open ? `还有 ${open} 题没答，确定交卷？` : '确定交卷？')) p.submit(d);
  };

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (/^[1-4]$/.test(e.key)) choose(+e.key - 1);
      else if (e.key === 'ArrowRight' || e.key === 'Enter') go(i + 1);
      else if (e.key === 'ArrowLeft') go(i - 1);
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  });

  if (!q) return null;
  return (
    <div class="lid-session">
      <div class="lid-bar">
        <button class="btn ghost lid-tap" onClick={() => { if (confirm('放弃这场模拟考？（不计成绩）')) p.onAbort(); }}>✕</button>
        <span class="lid-bar-title">模拟考 {i + 1}/33</span>
        <span class={`lid-timer ${left < 5 * 60_000 ? 'warn' : ''}`}>⏱ {mmss(left)}</span>
      </div>
      <button type="button" class="btn lid-grid-toggle" onClick={() => setGridOpen(!gridOpen)} aria-expanded={gridOpen}>
        <span>题号 · 已答 {answered}/33{d.flags.some(Boolean) ? ` · 待检查 ${d.flags.filter(Boolean).length}` : ''}</span><span>{gridOpen ? '▴' : '▾'}</span>
      </button>
      {gridOpen && <div class="lid-grid">
        {d.exam.qids.map((_, k) => (
          <button key={k} type="button" onClick={() => go(k)}
            class={`lid-cell ${k === i ? 'cur' : ''} ${d.answers[k] !== null ? 'done' : ''} ${d.flags[k] ? 'flag' : ''}`}>{k + 1}</button>
        ))}
      </div>}
      <QuestionCard q={q} order={IDENTITY} chosen={d.answers[i]} reveal={false} zh={null} onChoose={choose} flagged={d.flags[i]} />
      <div class="lid-actions">
        <button class="btn lid-tap" disabled={i === 0} onClick={() => go(i - 1)}>‹</button>
        <button class={`btn lid-tap ${d.flags[i] ? 'lid-flag-on' : ''}`} onClick={flag}>⚑ 待检查</button>
        {i < d.exam.qids.length - 1
          ? <button class="btn primary lid-big" onClick={() => go(i + 1)}>下一题 ›</button>
          : <button class="btn primary lid-big" disabled={p.busy} onClick={trySubmit}>交卷（{answered}/33）</button>}
      </div>
      {i < d.exam.qids.length - 1 && (
        <div class="row" style={{ justifyContent: 'center' }}>
          <button class="btn ghost sm" disabled={p.busy} onClick={trySubmit}>提前交卷（已答 {answered}/33）</button>
        </div>
      )}
    </div>
  );
}

function ExamReview(p: { r: ExamResult; onExit: () => void }) {
  const wrong = p.r.items.filter((it) => !it.correct);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  return (
    <div class="lid-session">
      <section class="card lid-result">
        <div class={`lid-score ${p.r.passed ? 'pass' : 'fail'}`}>{p.r.score}<span>/{p.r.total}</span></div>
        <div class={p.r.passed ? 'lid-good' : 'lid-bad'} style={{ fontSize: 18, fontWeight: 650 }}>{p.r.passed ? '及格 ✓' : '不及格'}</div>
        <div class="muted small">及格线 17 题 · 答错 {wrong.filter((w) => w.chosen !== null).length} 题（已进错题本，等级退回 1）{wrong.some((w) => w.chosen === null) ? ` · 未作答 ${wrong.filter((w) => w.chosen === null).length} 题（算错，不进错题本）` : ''}</div>
      </section>
      {wrong.map((it) => {
        const q = byId.value.get(it.qid);
        if (!q) return null;
        const zhDefault = showZh(zhMode.value, progressOf(it.qid)?.streak ?? 0, false);
        const zh = open[`zh${it.qid}`] ?? zhDefault;
        return (
          <QuestionCard key={it.qid} q={q} order={IDENTITY} chosen={it.chosen} reveal zh={zh}
            onToggleZh={() => setOpen({ ...open, [`zh${it.qid}`]: !zh })} onChoose={() => {}}
            expOpen={open[`e${it.qid}`] ?? true} onToggleExp={() => setOpen({ ...open, [`e${it.qid}`]: !(open[`e${it.qid}`] ?? true) })}
            head={it.chosen === null ? <span class="badge red">未作答</span> : null} />
        );
      })}
      <div class="lid-actions"><button class="btn primary lid-big" onClick={p.onExit}>返回</button></div>
    </div>
  );
}

export function ExamHistory() {
  const [rows, setRows] = useState<ExamRow[] | null>(null);
  useEffect(() => { api<{ items: ExamRow[] }>('/api/lid/exams').then((r) => setRows(r.items)).catch(toastError); }, []);
  const chart = useMemo(() => {
    if (!rows || rows.length < 1) return null;
    const asc = [...rows].reverse();
    const xs = asc.map((r) => Math.floor(Date.parse(r.started_at) / 1000));
    // 同一秒内的多场会让 uPlot 的 x 轴重叠：顺延 1 秒
    for (let k = 1; k < xs.length; k++) if (xs[k] <= xs[k - 1]) xs[k] = xs[k - 1] + 1;
    return { xs, series: [
      { label: '得分', color: 'accent', values: asc.map((r) => r.score) },
      { label: '及格线', color: 'red', values: asc.map(() => 17) },
    ] };
  }, [rows]);
  if (!rows) return null;
  return (
    <section class="card">
      <h3>历史成绩<span class="sub">{rows.length} 场 · 及格 {rows.filter((r) => r.passed).length}</span></h3>
      {rows.length === 0 ? <div class="muted small">还没考过</div> : (
        <>
          {chart && rows.length >= 2 && <LineChart xs={chart.xs} series={chart.series} height={180} />}
          <div class="lid-chips">
            {rows.slice(0, 12).map((r) => (
              <span key={r.id} class={`badge ${r.passed ? 'green' : 'red'}`} data-tip={new Date(r.started_at).toLocaleString('zh-CN', { timeZone: 'Europe/Berlin' })}>{r.score}/{r.total}</span>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

