import { useEffect, useState } from 'preact/hooks';
import { api } from '../../lib/api';
import { toastError } from '../../lib/toast';
import { Skeleton } from '../../ui';
import { Bars } from '../../ui/chart';
import { byId, qLabel } from './data';
import type { Prediction } from './data';
import { ExamHistory } from './Exam';
import { LEVEL_NAMES } from './logic';

interface Stats {
  levels: number[]; days: { day: string; n: number; ok: number }[]; answered: number; accuracy: number | null;
  worst: { qid: string; wrong: number; correct: number }[]; prediction: Prediction; exam_date: string | null;
}

// 红色只留给「答错」：等级 1 不是错，用黄
const LEVEL_COLORS = ['grey', 'yellow', 'blue', 'purple', 'green'];

export function StatsView(p: { onExit: () => void; onPractice: (ids: string[], title: string) => void }) {
  const [s, setS] = useState<Stats | null>(null);
  useEffect(() => { api<Stats>('/api/lid/stats').then(setS).catch(toastError); }, []);
  if (!s) return <div class="lid-session"><Skeleton lines={8} h={18} /></div>;
  const total = s.levels.reduce((a, b) => a + b, 0) || 1;
  return (
    <div class="lid-session">
      <section class="card">
        <h3>掌握进度<span class="sub">{s.levels[4]}/{total} 已掌握</span></h3>
        <div class="lid-stack">
          {s.levels.map((n, k) => n > 0 && (
            <span key={k} style={{ width: `${(n / total) * 100}%`, background: `var(--${LEVEL_COLORS[k]})` }} data-tip={`${LEVEL_NAMES[k]}：${n} 题`} />
          ))}
        </div>
        <div class="lid-legend">
          {s.levels.map((n, k) => <span key={k}><i style={{ background: `var(--${LEVEL_COLORS[k]})` }} />{LEVEL_NAMES[k]} {n}</span>)}
        </div>
        <div class="muted small" style={{ marginTop: 8 }}>
          按当前节奏最早 {s.prediction.date} 全部掌握{s.exam_date ? `（考试 ${s.exam_date}）` : ''}
        </div>
      </section>
      <section class="card">
        <h3>近 14 天做题量<span class="sub">累计 {s.answered} 题 · 正确率 {s.accuracy === null ? '—' : `${Math.round(s.accuracy * 100)}%`}</span></h3>
        <Bars data={s.days.map((d) => ({ label: `${d.day.slice(5)}（对 ${d.ok}）`, value: d.n }))} height={120} fmt={(v) => `${v} 题`} />
        <div class="row muted small" style={{ justifyContent: 'space-between', marginTop: 4 }}>
          <span>{s.days[0]?.day.slice(5)}</span><span>今天</span>
        </div>
      </section>
      <section class="card">
        <h3>最常错的题<span class="sub">{s.worst.length ? <button class="btn sm" onClick={() => p.onPractice(s.worst.map((w) => w.qid), '最常错')}>练这些</button> : null}</span></h3>
        {s.worst.length === 0 ? <div class="muted small">还没有错过的题</div> : (
          <div class="col">
            {s.worst.map((w) => {
              const q = byId.value.get(w.qid);
              return q ? (
                <div key={w.qid} class="lid-line">
                  <span class="badge red">{qLabel(q)}</span>
                  <span lang="de" style={{ flex: 1 }}>{q.de.q}</span>
                  <span class="muted small" style={{ whiteSpace: 'nowrap' }}>错 {w.wrong} / 对 {w.correct}</span>
                </div>
              ) : null;
            })}
          </div>
        )}
      </section>
      <ExamHistory />
      <div class="lid-actions"><button class="btn primary lid-big" onClick={p.onExit}>返回</button></div>
    </div>
  );
}
