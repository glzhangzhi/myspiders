import { describe, expect, it } from 'vitest';
import { countdown, optionOrder, requeue, showZh, shuffled, todayQueue, type Progress, type Question } from '../../pages/lid/logic';

const q = (id: string): Question => ({
  id, n: 0, part: 'general', de: { q: '', options: ['a', 'b', 'c', 'd'] }, zh: { q: '', options: ['', '', '', ''] },
  answer: 0, exp: { de: '', zh: '' }, images: [], credit: null,
});
const p = (qid: string, level: number, due: string | null, streak = 1): Progress => ({
  qid, level, due, streak, seen: 1, correct: 1, wrong: 0, last_at: null, wrongbook: false, starred: false, intro_on: null,
});

describe('lid logic', () => {
  it('todayQueue：到期复习（逾期久的在前）+ 新题按题号限额', () => {
    const qs = ['1', '2', '3', '4', '5', '6'].map(q);
    const prog = {
      '2': p('2', 2, '2026-11-10'), '3': p('3', 1, '2026-11-08'), '4': p('4', 3, '2026-11-20'), '5': p('5', 4, null),
    };
    expect(todayQueue(qs, prog, '2026-11-10', 1)).toEqual(['3', '2', '1']);
    expect(todayQueue(qs, prog, '2026-11-10', 5)).toEqual(['3', '2', '1', '6']);
    expect(todayQueue(qs, prog, '2026-11-10', 0)).toEqual(['3', '2']);
  });
  it('requeue 插到 3–5 题之后，队尾不够就追加', () => {
    const qq = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'];
    expect(requeue(qq, 0, 'a', () => 0)).toEqual(['a', 'b', 'c', 'd', 'a', 'e', 'f', 'g', 'h']);
    expect(requeue(qq, 0, 'a', () => 0.99)).toEqual(['a', 'b', 'c', 'd', 'e', 'f', 'a', 'g', 'h']);
    expect(requeue(qq, 6, 'g', () => 0)).toEqual([...qq, 'g']);
  });
  it('shuffled 是排列', () => {
    const s = shuffled(4);
    expect([...s].sort()).toEqual([0, 1, 2, 3]);
  });
  it('图片编号题不打乱', () => {
    const img = { ...q('21'), de: { q: '', options: ['Bild 1', 'Bild 2', 'Bild 3', 'Bild 4'] } };
    expect(optionOrder(img, () => 0)).toEqual([0, 1, 2, 3]);
    expect(optionOrder({ ...q('130'), de: { q: '', options: ['1', '2', '3', '4'] } }, () => 0)).toEqual([0, 1, 2, 3]);
    expect(optionOrder(q('x'), () => 0)).not.toEqual([0, 1, 2, 3]);
  });
  it('showZh 淡出规则', () => {
    expect(showZh('auto', 0, false)).toBe(true);
    expect(showZh('auto', 1, false)).toBe(true);
    expect(showZh('auto', 2, false)).toBe(false);
    expect(showZh('auto', 5, true)).toBe(true);
    expect(showZh('always', 9, false)).toBe(true);
    expect(showZh('never', 0, true)).toBe(false);
  });
  it('countdown', () => {
    expect(countdown(24)).toBe('24 天');
    expect(countdown(0)).toBe('今天');
    expect(countdown(null)).toBe('—');
  });
});
