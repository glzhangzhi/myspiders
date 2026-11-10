import type { ComponentChildren } from 'preact';
import { imgUrl, qLabel } from './data';
import type { Question } from './logic';

/**
 * 题目卡片（练习与模拟考共用）。
 *   order：显示位置 → 官方下标（练习打乱，模拟考恒等）
 *   chosen：已选的官方下标；reveal：是否标出对错
 *   zh：是否显示中文（null = 本题不提供「中」按钮，模拟考作答时）
 */
export function QuestionCard(p: {
  q: Question; order: number[]; chosen: number | null; reveal: boolean;
  zh: boolean | null; onToggleZh?: () => void;
  starred?: boolean; onStar?: () => void;
  onChoose: (official: number) => void;
  expOpen?: boolean; onToggleExp?: () => void;
  head?: ComponentChildren; flagged?: boolean;
}) {
  const { q, order, chosen, reveal } = p;
  const zh = !!p.zh;
  return (
    <section class="card lid-card">
      <div class="lid-card-top">
        <span class="badge">{qLabel(q)}</span>
        {q.part === 'state' && <span class="badge accent">Hessen</span>}
        {p.flagged && <span class="badge yellow">待检查</span>}
        {p.head}
        <span class="spacer" />
        {p.zh !== null && (
          <button type="button" class={`lid-icon ${zh ? 'on' : ''}`} onClick={p.onToggleZh} aria-pressed={zh} title="显示/隐藏中文（z）">中</button>
        )}
        {p.onStar && (
          <button type="button" class={`lid-icon star ${p.starred ? 'on' : ''}`} onClick={p.onStar} aria-pressed={!!p.starred} title="收藏">{p.starred ? '★' : '☆'}</button>
        )}
      </div>
      <div class="lid-q" lang="de">{q.de.q}</div>
      {zh && <div class="lid-zh">{q.zh.q}</div>}
      {q.images.map((name) => (
        <figure key={name} class="lid-fig">
          <img src={imgUrl(name)} alt={`Abbildung zu Frage ${q.id}`} loading="lazy" />
          {q.credit && <figcaption>{q.credit}</figcaption>}
        </figure>
      ))}
      <div class="lid-opts" role="radiogroup">
        {order.map((oi, pos) => {
          const isChosen = chosen === oi;
          const cls = reveal ? (oi === q.answer ? 'ok' : isChosen ? 'bad' : 'dim') : isChosen ? 'sel' : '';
          return (
            <button key={oi} type="button" role="radio" aria-checked={isChosen} class={`lid-opt ${cls}`}
              disabled={reveal} onClick={() => p.onChoose(oi)}>
              <span class="lid-key">{reveal && oi === q.answer ? '✓' : reveal && isChosen ? '✗' : pos + 1}</span>
              <span class="lid-opt-text">
                <span lang="de">{q.de.options[oi]}</span>
                {zh && <span class="lid-zh">{q.zh.options[oi]}</span>}
              </span>
            </button>
          );
        })}
      </div>
      {reveal && (
        <div class="lid-exp">
          <button type="button" class="lid-exp-head" onClick={p.onToggleExp} aria-expanded={!!p.expOpen}>
            <span class={chosen === q.answer ? 'lid-good' : 'lid-bad'}>{chosen === null ? '未作答' : chosen === q.answer ? '答对了' : '答错了'}</span>
            <span class="muted small">解析 {p.expOpen ? '▾' : '▸'}</span>
          </button>
          {p.expOpen && (
            <div class="lid-exp-body">
              <div lang="de">{q.exp.de}</div>
              {zh && <div class="lid-zh">{q.exp.zh}</div>}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
