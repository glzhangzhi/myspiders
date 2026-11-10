// 入籍考试刷题的纯逻辑（无 DOM、无请求），单测见 web/src/lib/__tests__/lid.test.ts。

export interface Question {
  id: string; n: number; part: 'general' | 'state';
  de: { q: string; options: string[] }; zh: { q: string; options: string[] };
  answer: number; exp: { de: string; zh: string }; images: string[]; credit: string | null;
}
export interface Bank { meta: Record<string, unknown>; questions: Question[] }
export interface Progress {
  qid: string; level: number; due: string | null; streak: number; seen: number; correct: number; wrong: number;
  last_at: string | null; wrongbook: boolean; starred: boolean; intro_on: string | null; relearn?: boolean;
}
export type ZhMode = 'auto' | 'always' | 'never';

export const MASTERED = 4;
export const LEVEL_NAMES = ['新题', '1 级', '2 级', '3 级', '已掌握'];

/** 今日队列：先到期复习（逾期最久在前，同日按题号），再新题（按官方题号，不超过今日余量）。 */
export function todayQueue(qs: Question[], prog: Record<string, Progress>, today: string, newLeft: number): string[] {
  const due: { id: string; due: string; i: number }[] = [];
  const fresh: string[] = [];
  qs.forEach((q, i) => {
    const p = prog[q.id];
    const lv = p?.level ?? 0;
    if (lv === 0) fresh.push(q.id);
    else if (lv < MASTERED && p?.due && p.due <= today) due.push({ id: q.id, due: p.due, i });
  });
  due.sort((a, b) => (a.due < b.due ? -1 : a.due > b.due ? 1 : a.i - b.i));
  return [...due.map((d) => d.id), ...fresh.slice(0, Math.max(0, newLeft))];
}

/** 答错的题在本轮里「过 3–5 题」再出一次：插到当前位置之后第 gap+1 个位置（不够长就放队尾）。 */
export function requeue(queue: string[], pos: number, qid: string, rand: () => number = Math.random): string[] {
  const gap = 3 + Math.floor(rand() * 3);   // 3..5
  const at = Math.min(queue.length, pos + 1 + gap);
  return [...queue.slice(0, at), qid, ...queue.slice(at)];
}

/** 显示顺序（打乱）→ 每个位置对应的官方下标。 */
export function shuffled(n: number, rand: () => number = Math.random): number[] {
  const a = Array.from({ length: n }, (_, i) => i);
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/**
 * 中文是否默认展开（按单题）：
 *   always/never 照办；auto 时 streak < 2 显示。streak 取「作答前」的值 ——
 *   否则第二次答对的瞬间中文会在眼前消失；但本题刚答错时立刻回来。
 */
export function showZh(mode: ZhMode, streakBefore: number, answeredWrong: boolean): boolean {
  if (mode === 'always') return true;
  if (mode === 'never') return false;
  return streakBefore < 2 || answeredWrong;
}

/** 选项是「Bild 1..4」或「1..4」（编号指向图里的标号）：打乱会让屏幕上的序号和图对不上，保持官方顺序。 */
export function isNumberedOptions(q: Question): boolean {
  return q.de.options.every((o) => /^(Bild )?[1-4]$/.test(o.trim()));
}

/** 练习时的选项显示顺序。 */
export function optionOrder(q: Question, rand: () => number = Math.random): number[] {
  return isNumberedOptions(q) ? q.de.options.map((_, i) => i) : shuffled(q.de.options.length, rand);
}

export function shuffleIds(ids: string[], rand: () => number = Math.random): string[] {
  return shuffled(ids.length, rand).map((i) => ids[i]);
}

/** 剩余天数文案。 */
export function countdown(days: number | null): string {
  if (days === null) return '—';
  if (days > 0) return `${days} 天`;
  if (days === 0) return '今天';
  return '已过';
}
