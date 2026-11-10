import { signal } from '@preact/signals';

/**
 * 全局音频播放器（挂在外壳层，切页面不中断）。通用组件，不认识任何具体功能：
 * 功能页把一条「轨道」交给 `play()`，轨道自带取地址、存进度的回调。
 *
 * 约定（机主定的）：
 *  - 必须用 <audio> 元素（小米 HyperOS + Chrome 实测锁屏、切后台都能继续播），不做 PWA；
 *  - 接 Media Session：锁屏/通知栏显示标题作者，播放/暂停/快进快退；
 *  - 听完不自动播下一条，不做睡眠定时器；
 *  - 进度每隔几秒和暂停时交给 `track.savePos` 存到服务器。
 */
export interface Track {
  id: string;
  title: string;
  artist?: string;
  album?: string;
  /** 播放地址（已准备好的文件）。 */
  url: string;
  /** 续播位置（秒）。 */
  startAt?: number;
  duration?: number;
  savePos?: (pos: number) => void | Promise<unknown>;
}

export interface PlayerState {
  track: Track | null;
  playing: boolean;
  pos: number;
  dur: number;
  loading: boolean;
  error: string | null;
}

export const player = signal<PlayerState>({ track: null, playing: false, pos: 0, dur: 0, loading: false, error: null });

const SAVE_EVERY_MS = 5000;
const SKIP = { back: 15, fwd: 30 };

let audio: HTMLAudioElement | null = null;
let lastSaved = 0;

function set(p: Partial<PlayerState>) { player.value = { ...player.value, ...p }; }

function save(force = false) {
  const t = player.value.track;
  if (!t?.savePos || !audio) return;
  const now = Date.now();
  if (!force && now - lastSaved < SAVE_EVERY_MS) return;
  lastSaved = now;
  const pos = audio.currentTime;
  // 听到结尾的存 0：下次从头播
  const ended = audio.duration > 0 && audio.duration - pos < 3;
  Promise.resolve(t.savePos(ended ? 0 : pos)).catch(() => {});
}

function el(): HTMLAudioElement {
  if (audio) return audio;
  const a = new Audio();
  a.preload = 'auto';
  a.addEventListener('timeupdate', () => { set({ pos: a.currentTime }); save(); updatePositionState(); });
  a.addEventListener('durationchange', () => set({ dur: Number.isFinite(a.duration) ? a.duration : 0 }));
  a.addEventListener('play', () => { set({ playing: true }); setMs('playing'); });
  a.addEventListener('pause', () => { set({ playing: false }); setMs('paused'); save(true); });
  a.addEventListener('waiting', () => set({ loading: true }));
  a.addEventListener('canplay', () => set({ loading: false }));
  a.addEventListener('ended', () => { set({ playing: false }); save(true); });
  a.addEventListener('error', () => set({ loading: false, playing: false, error: '播放失败（文件可能已被清理，重新点一次会重新下载）' }));
  audio = a;
  installMediaSession();
  // 关页面 / 切走时尽量存一次
  window.addEventListener('pagehide', () => save(true));
  document.addEventListener('visibilitychange', () => { if (document.hidden) save(true); });
  return a;
}

function setMs(state: MediaSessionPlaybackState) {
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = state;
}

function updatePositionState() {
  if (!audio || !('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
  const d = audio.duration;
  if (!Number.isFinite(d) || d <= 0) return;
  try { navigator.mediaSession.setPositionState({ duration: d, position: Math.min(audio.currentTime, d), playbackRate: audio.playbackRate }); } catch { /* 部分浏览器在元数据未就绪时抛 */ }
}

function installMediaSession() {
  if (!('mediaSession' in navigator)) return;
  const ms = navigator.mediaSession;
  const h: [MediaSessionAction, MediaSessionActionHandler][] = [
    ['play', () => { void audio?.play(); }],
    ['pause', () => audio?.pause()],
    ['seekbackward', (d) => seekBy(-(d.seekOffset || SKIP.back))],
    ['seekforward', (d) => seekBy(d.seekOffset || SKIP.fwd)],
    ['seekto', (d) => { if (d.seekTime !== undefined) seek(d.seekTime); }],
    ['stop', () => stop()],
  ];
  for (const [a, fn] of h) { try { ms.setActionHandler(a, fn); } catch { /* 不支持的 action */ } }
  // 不注册 previoustrack / nexttrack：没有「下一条」，系统会用快进快退按钮代替
}

export function play(t: Track) {
  const a = el();
  const cur = player.value.track;
  if (cur && cur.id === t.id && a.src) { void a.play(); return; }
  if (cur) save(true);
  set({ track: t, playing: false, pos: t.startAt ?? 0, dur: t.duration ?? 0, loading: true, error: null });
  a.src = t.url;
  const start = t.startAt ?? 0;
  if (start > 0) {
    const onMeta = () => { a.currentTime = Math.min(start, Math.max(0, (a.duration || start) - 1)); a.removeEventListener('loadedmetadata', onMeta); };
    a.addEventListener('loadedmetadata', onMeta);
  }
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title: t.title, artist: t.artist ?? '', album: t.album ?? '' });
  }
  a.play().catch((e: Error) => set({ loading: false, error: e.name === 'NotAllowedError' ? '浏览器拦住了自动播放，点一下 ▶' : e.message }));
}

export function toggle() {
  const a = el();
  if (!player.value.track) return;
  if (a.paused) void a.play(); else a.pause();
}

export function seek(sec: number) {
  if (!audio) return;
  const d = audio.duration;
  audio.currentTime = Math.max(0, Number.isFinite(d) ? Math.min(sec, d - 0.5) : sec);
  set({ pos: audio.currentTime });
  save(true);
}

export function seekBy(delta: number) { if (audio) seek(audio.currentTime + delta); }

export function stop() {
  if (!audio) return;
  save(true);
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  set({ track: null, playing: false, pos: 0, dur: 0, loading: false, error: null });
  if ('mediaSession' in navigator) { navigator.mediaSession.metadata = null; navigator.mediaSession.playbackState = 'none'; }
}

/** 正在播的这条标题/作者改了（比如刚编辑过）：同步到播放条与锁屏。 */
export function retitle(id: string, title: string, artist?: string) {
  const t = player.value.track;
  if (!t || t.id !== id) return;
  set({ track: { ...t, title, artist: artist ?? t.artist } });
  if ('mediaSession' in navigator && navigator.mediaSession.metadata) {
    navigator.mediaSession.metadata = new MediaMetadata({ title, artist: artist ?? t.artist ?? '', album: t.album ?? '' });
  }
}

export function fmtClock(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const s = Math.floor(sec % 60), m = Math.floor((sec / 60) % 60), h = Math.floor(sec / 3600);
  const p = (n: number) => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}
