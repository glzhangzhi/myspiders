import { useState } from 'preact/hooks';
import { api } from '../lib/api';
import { fmtDE, fmtRel } from '../lib/format';
import { mutateKey, useQuery } from '../lib/query';
import { toast, toastError } from '../lib/toast';
import { Async, Badge, Card, Stat } from '../ui';
import { LineChart } from '../ui/chart';

interface Status {
  last_price?: number; threshold?: number; last_ok?: boolean; last_detail?: string; has_cookie?: boolean;
  cookie_fingerprint?: string; last_price_sources?: string[]; last_list_price?: number; last_in_stock?: boolean;
  last_checked_at?: string; last_title?: string; url?: string; interval_minutes?: number;
}
interface Hist { price: number; checked_at: string }

export function Joybuy() {
  const st = useQuery<Status>('/api/joybuy/status', { refreshMs: 120_000 });
  const hist = useQuery<Hist[]>('/api/joybuy/history?limit=200', { staleMs: 120_000 });
  const [cookie, setCookie] = useState('');
  const [th, setTh] = useState('');
  const [busy, setBusy] = useState('');
  const act = async (name: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(name);
    try { await fn(); toast(ok, 'ok'); mutateKey('/api/joybuy/status'); mutateKey('/api/joybuy/history?limit=200'); }
    catch (e) { toastError(e); } finally { setBusy(''); }
  };
  return (
    <Card title="🛒 Joybuy 降价监控" sub={st.data?.last_checked_at ? `检查于 ${fmtRel(st.data.last_checked_at)}` : ''}>
      <Async q={st}>{(s) => {
        const srcs = s.last_price_sources ?? [];
        const hasCookie = s.has_cookie ?? !!s.cookie_fingerprint;
        const cookieState = !hasCookie ? ['未设置', 'red'] : s.last_ok === false ? ['已失效', 'red'] : ['正常', 'green'];
        return (
          <div class="col" style={{ gap: '14px' }}>
            {s.last_title && <a class="small" href={s.url} target="_blank" rel="noreferrer">{s.last_title}</a>}
            <div class="stats">
              <Stat label="当前价" value={s.last_price != null ? `€${s.last_price.toFixed(2)}` : '—'}
                color={s.last_price != null && s.threshold != null && s.last_price < s.threshold ? 'green' : undefined}
                delta={s.last_list_price ? `原价 €${s.last_list_price.toFixed(2)}` : undefined} />
              <Stat label="推送阈值" value={s.threshold != null ? `€${Number(s.threshold).toFixed(2)}` : '—'} />
              <Stat label="Cookie" value={cookieState[0]} color={cookieState[1]} />
              {/* 价格源掉到 1 个 = 页面改版前兆，这是唯一的预警信号 */}
              <Stat label="价格源" value={`${srcs.length}/3`} color={srcs.length >= 2 ? undefined : srcs.length === 1 ? 'yellow' : 'red'} />
              {s.last_in_stock === false && <Stat label="库存" value="缺货" color="yellow" />}
            </div>
            {s.last_ok === false && s.last_detail && <div class="error">上次失败：{s.last_detail}</div>}
            {hist.data && hist.data.length > 1 && (() => {
              const pts = [...hist.data].reverse();
              return <LineChart height={180} xs={pts.map((h) => new Date(h.checked_at).getTime() / 1000)}
                series={[{ label: '价格', color: 'accent', values: pts.map((h) => h.price) }]} yFmt={(v) => `€${v}`} />;
            })()}
            <div class="row">
              <button class="btn sm" disabled={!!busy} onClick={() => act('check', () => api('/api/joybuy/check', { method: 'POST' }), '已查询')}>立刻查一次</button>
              <input class="input" style={{ width: '110px' }} type="number" step="0.01" placeholder="新阈值 €" value={th} onInput={(e) => setTh((e.target as HTMLInputElement).value)} />
              <button class="btn sm" disabled={!!busy || !(parseFloat(th) > 0)} onClick={() => act('th', () => api('/api/joybuy/threshold', { body: { threshold: parseFloat(th) } }), '阈值已更新')}>改阈值</button>
              <button class="btn sm ghost" disabled={!!busy} onClick={() => act('reset', () => api('/api/joybuy/reset-notify', { method: 'POST' }), '已重置推送状态')}>重置推送</button>
            </div>
            <form class="row" onSubmit={(e) => { e.preventDefault(); act('cookie', async () => {
              const r = await api<{ verify?: { ok?: boolean; detail?: string } }>('/api/joybuy/cookie', { body: { cookie } });
              setCookie('');
              if (r.verify && r.verify.ok === false) throw new Error(`cookie 已保存，但验证没通过：${r.verify.detail ?? '未知原因'}`);
            }, 'cookie 已更新并验证通过'); }}>
              <input class="input" style={{ flex: 1 }} type="password" autocomplete="off" placeholder="粘贴新 cookie（含 mfs_session=…）" value={cookie} onInput={(e) => setCookie((e.target as HTMLInputElement).value)} />
              <button class="btn sm" disabled={!!busy || cookie.length < 10}>更新 cookie</button>
            </form>
            <div class="small muted">每 {s.interval_minutes ?? '?'} 分钟检查 · 上次 {fmtDE(s.last_checked_at)} · 只在价格变动时留痕</div>
          </div>
        );
      }}</Async>
      {hist.data && hist.data.length > 0 && <div class="row small muted" style={{ marginTop: 8 }}><Badge>{hist.data.length} 次变价</Badge></div>}
    </Card>
  );
}
