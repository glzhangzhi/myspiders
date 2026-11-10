import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import { toastError } from '../../lib/toast';
import { Empty } from '../../ui';
import { QuestionCard } from './Card';
import { byId, postAnswer, progressOf, qLabel, setStar, zhMode } from './data';
import { optionOrder, requeue, showZh } from './logic';

interface Done { qid: string; correct: boolean }

/** 一轮练习：今日学习 / 自由练习 / 错题本 / 收藏 共用。 */
export function Session(p: { title: string; ids: string[]; mode: string; onExit: () => void }) {
  const [queue, setQueue] = useState(p.ids);
  const [pos, setPos] = useState(0);
  const [chosen, setChosen] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [zhOverride, setZhOverride] = useState<boolean | null>(null);
  const [expOpen, setExpOpen] = useState(false);
  const [done, setDone] = useState<Done[]>([]);
  const [finished, setFinished] = useState(p.ids.length === 0);
  const top = useRef<HTMLDivElement>(null);

  const qid = queue[pos];
  const q = qid ? byId.value.get(qid) : undefined;
  // 每次出题时定：选项顺序、作答前的 streak（中文淡出看的是它）
  const order = useMemo(() => (q ? optionOrder(q) : []), [qid, pos]);
  const streak0 = useMemo(() => progressOf(qid)?.streak ?? 0, [qid, pos]);
  const reveal = chosen !== null;
  const wrong = reveal && q !== undefined && chosen !== q.answer;
  const zh = zhOverride ?? showZh(zhMode.value, streak0, wrong);
  const starred = !!progressOf(qid)?.starred;

  const choose = async (oi: number) => {
    if (!q || reveal || busy) return;
    setBusy(true);
    try {
      const r = await postAnswer(q.id, oi, p.mode);
      setChosen(oi);
      setExpOpen(!r.correct);
      setDone((d) => [...d, { qid: q.id, correct: r.correct }]);
      if (!r.correct) setQueue((qq) => requeue(qq, pos, q.id));
    } catch (e) { toastError(e); } finally { setBusy(false); }
  };

  const next = () => {
    if (!reveal) return;
    setChosen(null); setZhOverride(null); setExpOpen(false);
    if (pos + 1 >= queue.length) setFinished(true);
    else setPos(pos + 1);
    top.current?.scrollIntoView({ block: 'start' });
  };

  const toggleZh = () => setZhOverride(!zh);
  const star = () => setStar(qid, !starred).catch(toastError);

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (finished || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement;
      if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
      if (/^[1-4]$/.test(e.key) && !reveal) { const oi = order[+e.key - 1]; if (oi !== undefined) choose(oi); }
      else if ((e.key === 'Enter' || e.key === ' ') && reveal) { e.preventDefault(); next(); }
      else if (e.key === 'z' || e.key === 'Z') toggleZh();
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  });

  if (finished) return <Summary title={p.title} done={done} onExit={p.onExit} />;
  if (!q) return <Empty>题库里没有 {qid}</Empty>;
  const ok = done.filter((d) => d.correct).length;
  return (
    <div class="lid-session" ref={top}>
      <div class="lid-bar">
        <button class="btn ghost lid-tap" onClick={() => setFinished(true)}>✕ 结束</button>
        <span class="lid-bar-title">{p.title}</span>
        <span class="muted small">{pos + 1}/{queue.length} · 对 {ok}/{done.length}</span>
      </div>
      <div class="bar"><span style={{ width: `${(pos / Math.max(1, queue.length)) * 100}%` }} /></div>
      <QuestionCard q={q} order={order} chosen={chosen} reveal={reveal} zh={zh} onToggleZh={toggleZh}
        starred={starred} onStar={star} onChoose={choose} expOpen={expOpen} onToggleExp={() => setExpOpen(!expOpen)} />
      <div class="lid-actions">
        {reveal
          ? <button class="btn primary lid-big" onClick={next}>{pos + 1 >= queue.length ? '完成' : '下一题'} <kbd class="hide-mobile">Enter</kbd></button>
          : <span class="muted small lid-hint">点选项作答<span class="hide-mobile"> · 1–4 选 · z 切换中文</span></span>}
      </div>
    </div>
  );
}

function Summary(p: { title: string; done: Done[]; onExit: () => void }) {
  const ok = p.done.filter((d) => d.correct).length;
  const wrongIds = [...new Set(p.done.filter((d) => !d.correct).map((d) => d.qid))];
  return (
    <div class="lid-session">
      <section class="card">
        <h3>{p.title} · 本轮小结</h3>
        <div class="stats">
          <div class="stat"><span class="label">做了</span><span class="value">{p.done.length}</span></div>
          <div class="stat"><span class="label">答对</span><span class="value" style={{ color: 'var(--green)' }}>{ok}</span></div>
          <div class="stat"><span class="label">答错</span><span class="value" style={{ color: wrongIds.length ? 'var(--red)' : undefined }}>{p.done.length - ok}</span></div>
        </div>
        {wrongIds.length > 0 && (
          <div class="col" style={{ marginTop: 12 }}>
            <div class="muted small">错过的题（已进错题本）</div>
            {wrongIds.map((id) => {
              const q = byId.value.get(id);
              return q ? <div key={id} class="lid-line"><span class="badge red">{qLabel(q)}</span><span lang="de">{q.de.q}</span></div> : null;
            })}
          </div>
        )}
      </section>
      <div class="lid-actions"><button class="btn primary lid-big" onClick={p.onExit}>返回</button></div>
    </div>
  );
}
