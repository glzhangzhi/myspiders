import { signal } from '@preact/signals';
import { useMemo, useState } from 'preact/hooks';
import { api } from '../lib/api';
import { fmtDE, fmtDur, fmtRel } from '../lib/format';
import { useEventSource } from '../lib/sse';
import { toast, toastError } from '../lib/toast';
import { Badge, Drawer, Empty, Switch } from '../ui';

export interface Spider {
  spider_id: string; name: string; description: string; category: string; enabled: boolean; schedule: string;
  last_run_at: string | null; next_run_at: string | null; last_status: string | null; last_error: string | null;
  last_duration_seconds: number | null;
}

export const spiders = signal<Spider[]>([]);

export async function loadSpiders() {
  const r = await api<{ spiders: Spider[] }>('/api/spiders');
  spiders.value = r.spiders;
}

function upsert(s: Spider) {
  const cur = spiders.value;
  const i = cur.findIndex((x) => x.spider_id === s.spider_id);
  spiders.value = i < 0 ? [...cur, s] : cur.map((x, j) => (j === i ? s : x));
}

export async function toggleSpider(id: string, enabled: boolean) {
  try { upsert(await api<Spider>(`/api/spiders/${id}/toggle`, { body: { enabled } })); toast(enabled ? '已启用' : '已停用', 'ok'); }
  catch (e) { toastError(e); }
}

export async function triggerSpider(id: string) {
  try { const r = await api<{ spider: Spider }>(`/api/spiders/${id}/trigger`, { method: 'POST' }); upsert(r.spider); toast('已触发', 'ok'); }
  catch (e) { toastError(e); }
}

/** 全局订阅爬虫状态（外壳挂一次即可）。 */
export function useSpiderStream() {
  return useEventSource('/api/spiders/events', 'status', (d) => upsert(d as Spider));
}

export function statusOf(s: Spider): { tone: 'green' | 'red' | 'yellow' | undefined; text: string } {
  if (!s.enabled) return { tone: undefined, text: '已停用' };
  switch (s.last_status) {
    case 'running': return { tone: 'yellow', text: '运行中' };
    case 'success': return { tone: 'green', text: '成功' };
    case 'failed': return { tone: 'red', text: '失败' };
    default: return { tone: undefined, text: '等待中' };
  }
}

const CATS: Record<string, string> = { monitor: '监控', system: '系统', claude: 'Claude', nintendo: 'Nintendo', software: '软件', av: '影片', timelog: '计时', media: '媒体', general: '通用' };

export function SpidersDrawer(p: { onClose: () => void; conn: string }) {
  const [cat, setCat] = useState('all');
  const [open, setOpen] = useState<string | null>(null);
  const list = spiders.value;
  const cats = useMemo(() => ['all', ...new Set(list.map((s) => s.category))], [list]);
  const shown = list.filter((s) => cat === 'all' || s.category === cat);
  const failed = list.filter((s) => s.enabled && s.last_status === 'failed').length;
  return (
    <Drawer title="⏱️ 爬虫管理" onClose={p.onClose}
      actions={<span class="row small muted"><span class={`dot ${p.conn === 'open' ? 'green' : 'yellow pulse'}`} />{p.conn === 'open' ? '实时' : '重连中'}</span>}>
      <div class="stats">
        <div class="stat"><span class="label">总数</span><span class="value">{list.length}</span></div>
        <div class="stat"><span class="label">已启用</span><span class="value">{list.filter((s) => s.enabled).length}</span></div>
        <div class="stat"><span class="label">失败</span><span class="value" style={failed ? { color: 'var(--red)' } : undefined}>{failed}</span></div>
      </div>
      <div class="row">
        {cats.map((c) => <button key={c} class={`btn sm ${c === cat ? 'primary' : ''}`} onClick={() => setCat(c)}>{c === 'all' ? '全部' : CATS[c] ?? c}</button>)}
      </div>
      <div>
        {!shown.length && <Empty>没有爬虫</Empty>}
        {shown.map((s) => {
          const st = statusOf(s);
          return (
            <div class="spider" key={s.spider_id}>
              <Switch on={s.enabled} onChange={(v) => toggleSpider(s.spider_id, v)} label={`启用 ${s.name}`} />
              <div class="meta">
                <div class="row" style={{ gap: '6px' }}>
                  <span class="name">{s.name}</span>
                  <Badge tone={st.tone}>{st.text}</Badge>
                  <span class="muted small">{s.schedule}</span>
                </div>
                <div class="small muted">
                  上次 {fmtRel(s.last_run_at)}{s.last_duration_seconds !== null && ` · ${fmtDur(s.last_duration_seconds)}`}
                  {s.enabled && s.next_run_at && ` · 下次 ${fmtDE(s.next_run_at)}`}
                </div>
                {s.last_error && <div class="small" style={{ color: 'var(--red)', marginTop: 2 }}>{s.last_error}</div>}
                {open === s.spider_id && s.description && <div class="desc">{s.description}</div>}
              </div>
              <div class="col" style={{ gap: '4px' }}>
                <button class="btn sm" disabled={s.last_status === 'running'} onClick={() => triggerSpider(s.spider_id)}>▶ 运行</button>
                {s.description && <button class="btn ghost sm" onClick={() => setOpen(open === s.spider_id ? null : s.spider_id)}>{open === s.spider_id ? '收起' : '说明'}</button>}
              </div>
            </div>
          );
        })}
      </div>
    </Drawer>
  );
}
