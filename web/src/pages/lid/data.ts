import { computed, signal } from '@preact/signals';
import { api } from '../../lib/api';
import type { Bank, Progress, Question, ZhMode } from './logic';

export interface Prediction { date: string; days: number; new_remaining: number; intro_done: string | null; on_time: boolean | null }
export interface LidState {
  today: string; progress: Record<string, Progress>; done_today: number; new_left: number; daily_new: number;
  exam_date: string | null; days_left: number | null; total: number; mastered: number; due: number;
  wrongbook: number; starred: number; prediction: Prediction; bank_etag: string;
}
export interface AnswerResp { correct: boolean; answer: number; state: Progress }

export const bank = signal<Bank | null>(null);
export const lid = signal<LidState | null>(null);
export const byId = computed(() => new Map((bank.value?.questions ?? []).map((q) => [q.id, q])));

let bankEtag = '';

/** 先取进度（小），题库按 etag 走长缓存（大，只在内容变时重新下载）。 */
export async function loadLid(): Promise<void> {
  const s = await api<LidState>('/api/lid/state');
  if (!bank.value || bankEtag !== s.bank_etag) {
    bank.value = await api<Bank>(`/api/lid/bank?v=${encodeURIComponent(s.bank_etag)}`);
    bankEtag = s.bank_etag;
  }
  lid.value = s;
}

export function progressOf(qid: string): Progress | undefined { return lid.value?.progress[qid]; }

function patchProgress(p: Progress) {
  const s = lid.value;
  if (s) lid.value = { ...s, progress: { ...s.progress, [p.qid]: p } };
}

export async function postAnswer(qid: string, chosen: number, mode: string): Promise<AnswerResp> {
  const r = await api<AnswerResp>('/api/lid/answer', { body: { qid, chosen, mode } });
  patchProgress(r.state);
  return r;
}

export async function setStar(qid: string, starred: boolean): Promise<void> {
  patchProgress(await api<Progress>('/api/lid/star', { body: { qid, starred } }));
}

export async function removeWrong(qid: string): Promise<void> {
  patchProgress(await api<Progress>('/api/lid/wrongbook/remove', { body: { qid } }));
}

// ── 中文显示全局开关（自动 / 总是 / 从不），存 localStorage ──
const ZH_KEY = 'lid-zh-mode';
const initial = localStorage.getItem(ZH_KEY);
export const zhMode = signal<ZhMode>(initial === 'always' || initial === 'never' ? initial : 'auto');
export function setZhMode(m: ZhMode) { zhMode.value = m; localStorage.setItem(ZH_KEY, m); }

export function imgUrl(name: string) { return `/api/lid/img/${encodeURIComponent(name)}`; }
export function qLabel(q: Question) { return q.part === 'state' ? q.id : `#${q.id}`; }
