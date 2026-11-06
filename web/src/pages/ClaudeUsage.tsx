import { useState } from 'preact/hooks';
import { api } from '../lib/api';
import { fmtDE, fmtRel } from '../lib/format';
import { mutateKey, useQuery } from '../lib/query';
import { toastError } from '../lib/toast';
import { Async, Bar, Card, Empty, Tabs } from '../ui';
import { Bars } from '../ui/chart';

interface Limit { kind: string; percent: number; severity?: string; resets_at?: string; scope?: { model?: { display_name?: string } } | null }
interface Usage { ok: boolean; fetched_at?: number; last_error?: string | null; data?: { limits?: Limit[]; five_hour?: { utilization: number; resets_at: string }; seven_day?: { utilization: number; resets_at: string } } }
interface Hist { view: string; buckets?: { label: string; value: number }[]; grid?: number[][]; weekdays?: string[] }

function label(l: Limit) {
  if (l.kind === 'session') return '5 小时窗口';
  if (l.kind === 'weekly_all') return '7 天（全部）';
  if (l.kind === 'weekly_scoped') return `7 天（${l.scope?.model?.display_name ?? '按模型'}）`;
  return l.kind;
}
function tone(l: Limit): 'green' | 'yellow' | 'red' {
  const s = (l.severity ?? '').toLowerCase();
  if (/high|crit|exceed/.test(s)) return 'red';
  if (/warn|approach/.test(s)) return 'yellow';
  return l.percent >= 90 ? 'red' : l.percent >= 70 ? 'yellow' : 'green';
}

export function ClaudeUsagePage() {
  const usage = useQuery<Usage>('/api/claude-usage', { refreshMs: 60_000 });
  const [view, setView] = useState<'hour' | 'weekday' | 'date' | 'heatmap'>('hour');
  const [win, setWin] = useState('5h');
  const hist = useQuery<Hist>(`/api/claude-usage/history?view=${view}&window=${win}&days=30`, { staleMs: 120_000 });
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try { await api('/api/claude-usage/refresh', { method: 'POST' }); mutateKey('/api/claude-usage'); }
    catch (e) { toastError(e); } finally { setBusy(false); }
  };
  return (
    <div class="page">
      <Card title="📊 Claude 账户用量" sub={<span class="row">{usage.data?.fetched_at && <span>更新于 {fmtRel(usage.data.fetched_at)}</span>}<button class="btn sm" disabled={busy} onClick={refresh}>刷新</button></span>}>
        <Async q={usage}>{(u) => {
          let limits = u.data?.limits ?? [];
          if (!limits.length && u.data) {
            if (u.data.five_hour) limits = [...limits, { kind: 'session', percent: u.data.five_hour.utilization, resets_at: u.data.five_hour.resets_at }];
            if (u.data.seven_day) limits = [...limits, { kind: 'weekly_all', percent: u.data.seven_day.utilization, resets_at: u.data.seven_day.resets_at }];
          }
          if (!limits.length) return <Empty>暂无用量数据</Empty>;
          return (
            <div class="col" style={{ gap: '14px' }}>
              {u.last_error && <div class="error">{u.last_error}</div>}
              {limits.map((l) => (
                <div key={l.kind + label(l)}>
                  <div class="row small" style={{ marginBottom: 4 }}>
                    <span class="muted">{label(l)}</span>
                    <b style={{ marginLeft: 'auto', color: `var(--${tone(l)})` }}>{Math.round(l.percent)}%</b>
                  </div>
                  <Bar pct={l.percent} color={tone(l)} />
                  {l.resets_at && <div class="small muted" style={{ marginTop: 3 }}>{fmtDE(l.resets_at)} 重置（{fmtRel(l.resets_at)}）</div>}
                </div>
              ))}
            </div>
          );
        }}</Async>
      </Card>

      <Card title="📈 消耗分布" sub={
        <select class="input" value={win} onChange={(e) => setWin((e.target as HTMLSelectElement).value)}>
          <option value="5h">5 小时窗口</option><option value="7d">7 天窗口</option><option value="7d_sonnet">7 天 Sonnet</option>
        </select>}>
        <Tabs value={view} onChange={setView} options={[
          { value: 'hour', label: '24 小时' }, { value: 'weekday', label: '周几' }, { value: 'date', label: '30 天' }, { value: 'heatmap', label: '热力图' },
        ]} />
        <div style={{ marginTop: 14 }}>
          <Async q={hist}>{(h) => {
            if (h.view === 'heatmap' && h.grid) {
              const max = Math.max(1, ...h.grid.flat());
              return (
                <div class="col" style={{ gap: '3px' }}>
                  {h.grid.map((row, wi) => (
                    <div key={wi} class="row" style={{ gap: '3px', flexWrap: 'nowrap' }}>
                      <span class="small muted" style={{ width: 34 }}>{h.weekdays?.[wi]}</span>
                      {row.map((v, hr) => (
                        <span key={hr} data-tip={`${h.weekdays?.[wi]} ${String(hr).padStart(2, '0')}:00 · ${v}%`}
                          style={{ flex: 1, height: 16, borderRadius: 3, background: 'var(--accent)', opacity: v > 0 ? 0.12 + (v / max) * 0.88 : 0.05 }} />
                      ))}
                    </div>
                  ))}
                  <div class="small muted">颜色越深 = 该时段消耗越多（占窗口额度 %，近 30 天累计）</div>
                </div>
              );
            }
            const b = h.buckets ?? [];
            if (!b.some((x) => x.value > 0)) return <Empty>正在积累数据，过一会再来看</Empty>;
            return (
              <>
                <Bars data={b} fmt={(v) => `${v}%`} />
                <div class="row small muted" style={{ justifyContent: 'space-between', marginTop: 4 }}>
                  <span>{b[0]?.label}</span><span>{b[Math.floor(b.length / 2)]?.label}</span><span>{b[b.length - 1]?.label}</span>
                </div>
              </>
            );
          }}</Async>
        </div>
      </Card>
    </div>
  );
}
