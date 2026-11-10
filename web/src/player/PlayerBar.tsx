import { useRef, useState } from 'preact/hooks';
import { fmtClock, player, seek, seekBy, stop, toggle } from './player';
import './player.css';

/** 外壳层的全局播放条：没有轨道时不渲染。拖动时只改显示，松手才 seek（手机上拖动更稳）。 */
export function PlayerBar() {
  const s = player.value;
  const [drag, setDrag] = useState<number | null>(null);
  const bar = useRef<HTMLInputElement>(null);
  if (!s.track) return null;
  const dur = s.dur || s.track.duration || 0;
  const pos = drag ?? s.pos;
  return (
    <div class="player" role="region" aria-label="播放器">
      <div class="player-row">
        <div class="player-meta">
          <div class="player-title" title={s.track.title}>{s.track.title}</div>
          <div class="player-artist">{s.error ? <span class="player-err">{s.error}</span> : (s.track.artist || ' ')}</div>
        </div>
        <button class="player-btn" onClick={() => seekBy(-15)} aria-label="后退 15 秒">↺<small>15</small></button>
        <button class="player-btn main" onClick={toggle} aria-label={s.playing ? '暂停' : '播放'}>
          {s.loading && s.playing ? '…' : s.playing ? '❚❚' : '▶'}
        </button>
        <button class="player-btn" onClick={() => seekBy(30)} aria-label="快进 30 秒">↻<small>30</small></button>
        <button class="player-btn close" onClick={stop} aria-label="关闭播放器">✕</button>
      </div>
      <div class="player-seek">
        <span>{fmtClock(pos)}</span>
        <input ref={bar} type="range" min={0} max={dur || 1} step={1} value={Math.min(pos, dur || 1)} aria-label="播放进度"
          style={{ '--p': `${dur ? (pos / dur) * 100 : 0}%` } as never}
          onInput={(e) => setDrag(Number((e.target as HTMLInputElement).value))}
          onChange={(e) => { seek(Number((e.target as HTMLInputElement).value)); setDrag(null); }} />
        <span>{fmtClock(dur)}</span>
      </div>
    </div>
  );
}
