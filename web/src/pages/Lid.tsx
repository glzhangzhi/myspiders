import { useEffect, useState } from 'preact/hooks';
import { toastError } from '../lib/toast';
import { ErrorBox, Skeleton } from '../ui';
import { bank, byId, lid, loadLid, qLabel, removeWrong, setZhMode, zhMode } from './lid/data';
import { Exam } from './lid/Exam';
import { countdown, MASTERED, shuffleIds, todayQueue, type ZhMode } from './lid/logic';
import { Session } from './lid/Session';
import { StatsView } from './lid/Stats';
import './lid/lid.css';

type View =
  | { kind: 'home' }
  | { kind: 'session'; title: string; ids: string[]; mode: string; key: number }
  | { kind: 'exam' }
  | { kind: 'stats' }
  | { kind: 'list'; which: 'wrongbook' | 'starred' };

/** 「Leben in Deutschland」入籍考试刷题（手机优先）。约定见 docs/lid.md。 */
export function LidPage() {
  const [view, setView] = useState<View>({ kind: 'home' });
  const [err, setErr] = useState<unknown>(null);
  useEffect(() => { loadLid().catch(setErr); }, []);
  const home = () => { setView({ kind: 'home' }); loadLid().catch(toastError); window.scrollTo({ top: 0 }); };
  const run = (title: string, ids: string[], mode: string) => setView({ kind: 'session', title, ids, mode, key: Date.now() });

  if (err) return <div class="page"><ErrorBox error={err} /></div>;
  if (!bank.value || !lid.value) return <div class="page lid-page"><Skeleton lines={8} h={20} /></div>;

  return (
    <div class="page lid-page">
      {view.kind === 'home' && <Home run={run} go={setView} />}
      {view.kind === 'session' && <Session key={view.key} title={view.title} ids={view.ids} mode={view.mode} onExit={home} />}
      {view.kind === 'exam' && <Exam onExit={home} />}
      {view.kind === 'stats' && <StatsView onExit={home} onPractice={(ids, t) => run(t, ids, 'practice')} />}
      {view.kind === 'list' && <QList which={view.which} onExit={home} run={run} />}
    </div>
  );
}

function Home(p: { run: (title: string, ids: string[], mode: string) => void; go: (v: View) => void }) {
  const s = lid.value!;
  const qs = bank.value!.questions;
  const queue = todayQueue(qs, s.progress, s.today, s.new_left);
  const newToday = Math.min(s.new_left, qs.filter((q) => (s.progress[q.id]?.level ?? 0) === 0).length);
  const pred = s.prediction;
  const late = pred.on_time === false;
  const allIds = qs.map((q) => q.id);
  const unseen = qs.filter((q) => !s.progress[q.id]?.seen).map((q) => q.id);
  const wrongIds = qs.filter((q) => s.progress[q.id]?.wrongbook).map((q) => q.id);
  const starIds = qs.filter((q) => s.progress[q.id]?.starred).map((q) => q.id);

  return (
    <>
      <section class="card lid-hero">
        <div class="lid-hero-top">
          <div>
            <div class="muted small">考试倒计时</div>
            <div class="lid-days">{s.days_left === null ? '未设日期' : countdown(s.days_left)}</div>
            {s.exam_date && <div class="muted small">{s.exam_date} · 33 题 / 60 分钟 / 17 题及格</div>}
          </div>
          <div class="lid-ring" style={{ '--p': `${(s.mastered / s.total) * 100}` } as never} data-tip={`已掌握 ${s.mastered}/${s.total}`}>
            <span>{s.mastered}<small>/{s.total}</small></span>
          </div>
        </div>
        <div class="stats lid-stats">
          <div class="stat"><span class="label">到期复习</span><span class="value">{s.due}</span></div>
          <div class="stat"><span class="label">今日新题余量</span><span class="value">{newToday}</span></div>
          <div class="stat"><span class="label">今天已做</span><span class="value">{s.done_today}</span></div>
        </div>
        <div class={`lid-pred ${late ? 'late' : pred.on_time ? 'ok' : ''}`}>
          {pred.new_remaining === 0 && s.mastered === s.total ? '全部掌握 🎉'
            : <>按每天 {s.daily_new} 道新题的节奏，最早 <b>{pred.date}</b> 全部掌握
              {pred.on_time === null ? '' : late ? `，赶不上考试（晚 ${daysBetween(s.exam_date!, pred.date)} 天）——每天多做些到期题，别断。` : '，考前来得及。'}</>}
        </div>
      </section>

      <button class="btn primary lid-start" disabled={queue.length === 0}
        onClick={() => p.run('今日学习', queue, 'today')}>
        {queue.length ? <>开始今日学习 <small>（{queue.length} 题）</small></> : '今天的都做完了 ✓'}
      </button>

      <div class="lid-menu">
        <MenuBtn icon="❌" label="错题本" n={wrongIds.length} onClick={() => p.go({ kind: 'list', which: 'wrongbook' })} />
        <MenuBtn icon="★" label="收藏" n={starIds.length} onClick={() => p.go({ kind: 'list', which: 'starred' })} />
        <MenuBtn icon="📋" label="模拟考" onClick={() => p.go({ kind: 'exam' })} />
        <MenuBtn icon="📊" label="统计" onClick={() => p.go({ kind: 'stats' })} />
      </div>

      <section class="card">
        <h3>自由练习<span class="sub">没到期的题答对只计统计、不升级</span></h3>
        <div class="lid-free">
          <button class="btn lid-tap" onClick={() => p.run('顺序练习', allIds, 'practice')}>顺序 1→310</button>
          <button class="btn lid-tap" onClick={() => p.run('随机练习', shuffleIds(allIds), 'practice')}>随机</button>
          <button class="btn lid-tap" disabled={!unseen.length} onClick={() => p.run('没做过的', unseen, 'practice')}>没做过的（{unseen.length}）</button>
          <button class="btn lid-tap" onClick={() => p.run('黑森州题', allIds.filter((id) => id.startsWith('HE-')), 'practice')}>黑森州 10 题</button>
        </div>
      </section>

      <section class="card">
        <h3>中文辅助</h3>
        <div class="lid-seg" role="radiogroup">
          {([['auto', '自动淡出'], ['always', '总是显示'], ['never', '从不显示']] as [ZhMode, string][]).map(([m, t]) => (
            <button key={m} role="radio" aria-checked={zhMode.value === m} class={zhMode.value === m ? 'on' : ''} onClick={() => setZhMode(m)}>{t}</button>
          ))}
        </div>
        <div class="muted small" style={{ marginTop: 8 }}>自动：一道题连续答对 2 次后默认收起中文，答错就回来；卡片角上的「中」可以临时展开。</div>
      </section>
    </>
  );
}

function daysBetween(a: string, b: string) { return Math.round((Date.parse(b) - Date.parse(a)) / 86400000); }

function MenuBtn(p: { icon: string; label: string; n?: number; onClick: () => void }) {
  return (
    <button class="lid-menu-btn" onClick={p.onClick}>
      <span class="lid-menu-icon">{p.icon}</span>
      <span>{p.label}</span>
      {p.n !== undefined && <span class="badge">{p.n}</span>}
    </button>
  );
}

function QList(p: { which: 'wrongbook' | 'starred'; onExit: () => void; run: (title: string, ids: string[], mode: string) => void }) {
  const s = lid.value!;
  const title = p.which === 'wrongbook' ? '错题本' : '收藏';
  const ids = bank.value!.questions.filter((q) => (p.which === 'wrongbook' ? s.progress[q.id]?.wrongbook : s.progress[q.id]?.starred)).map((q) => q.id);
  return (
    <div class="lid-session">
      <section class="card">
        <h3>{title}<span class="sub">{ids.length} 题{p.which === 'wrongbook' ? ' · 连续答对 2 次自动移出' : ''}</span></h3>
        {ids.length === 0 ? <div class="muted small">空的</div> : (
          <div class="col">
            {ids.map((id) => {
              const q = byId.value.get(id)!;
              const pr = s.progress[id];
              return (
                <div key={id} class="lid-line">
                  <span class={`badge ${p.which === 'wrongbook' ? 'red' : 'yellow'}`}>{qLabel(q)}</span>
                  <span lang="de" style={{ flex: 1 }}>{q.de.q}</span>
                  <span class="muted small" style={{ whiteSpace: 'nowrap' }}>{pr && pr.level >= MASTERED ? '已掌握' : `错 ${pr?.wrong ?? 0}`}</span>
                  {p.which === 'wrongbook' && <button class="btn ghost sm lid-tap" title="移出错题本" onClick={() => removeWrong(id).catch(toastError)}>移出</button>}
                </div>
              );
            })}
          </div>
        )}
      </section>
      <div class="lid-actions">
        <button class="btn ghost lid-tap" onClick={p.onExit}>返回</button>
        <button class="btn primary lid-big" disabled={!ids.length} onClick={() => p.run(title, ids, p.which)}>练这 {ids.length} 题</button>
      </div>
    </div>
  );
}
