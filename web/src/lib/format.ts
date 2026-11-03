const DE = 'Europe/Berlin';

/** ISO 时间 → 德国时间「MM-DD HH:mm」（全站展示口径）。 */
export function fmtDE(iso?: string | number | null, withYear = false): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const d = typeof iso === 'number' ? new Date(iso < 1e12 ? iso * 1000 : iso) : new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('zh-CN', {
    timeZone: DE, hour12: false,
    ...(withYear ? { year: 'numeric' } : {}), month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

/** 相对时间：刚刚 / 5 分钟前 / 3 小时后。 */
export function fmtRel(iso?: string | number | null, now = Date.now()): string {
  if (iso === null || iso === undefined || iso === '') return '—';
  const t = typeof iso === 'number' ? (iso < 1e12 ? iso * 1000 : iso) : new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const s = Math.round((t - now) / 1000);
  const a = Math.abs(s);
  const unit = a < 45 ? null : a < 3600 ? [Math.round(a / 60), '分钟'] : a < 86400 ? [Math.round(a / 3600), '小时'] : [Math.round(a / 86400), '天'];
  if (!unit) return '刚刚';
  return s < 0 ? `${unit[0]} ${unit[1]}前` : `${unit[0]} ${unit[1]}后`;
}

export function fmtBytes(n?: number | null): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
}

export function fmtDur(sec?: number | null): string {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 1 : 0)} 秒`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`;
  return `${Math.floor(sec / 3600)} 小时 ${Math.round((sec % 3600) / 60)} 分`;
}

export function fmtNum(n?: number | null, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

/** 浏览器本地时间 → 'YYYY-MM-DD HH:mm'（奇门排盘用本地时间而非服务器时区）。 */
export function localStamp(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
