import { useState } from 'preact/hooks';
import { api } from '../lib/api';
import { fmtBytes, fmtDE, fmtDur, fmtNum, fmtRel } from '../lib/format';
import { useQuery } from '../lib/query';
import { toast, toastError } from '../lib/toast';
import { Async, Badge, Bar, Card, Empty, Stat } from '../ui';
import { Joybuy } from './Joybuy';
import { spiders, statusOf } from '../shell/spiders';

interface Health {
  disk: Record<string, { bytes?: number; count?: number; total?: number; used?: number; pct?: number }>;
  mongo: { dbs: Record<string, { bytes: number; collections: number }>; total_bytes: number; error?: string };
  backup: { finished_at?: string; ok?: boolean; duration_seconds?: number; errors?: string[] };
  drive: { total: number; used: number; free: number } | null;
  cached_at?: number;
}

export function SystemPage() {
  const q = useQuery<Health>('/api/system/health', { staleMs: 60_000 });
  const failed = spiders.value.filter((s) => s.enabled && s.last_status === 'failed');
  const [busy, setBusy] = useState(false);
  const backup = async () => {
    if (!confirm('现在执行一次完整备份？（mongodump + rclone，可能要几分钟）')) return;
    setBusy(true);
    try { await api('/api/spiders/backup/trigger', { method: 'POST' }); toast('备份已开始，结果见爬虫管理', 'ok'); }
    catch (e) { toastError(e); } finally { setBusy(false); }
  };
  return (
    <div class="page">
      <div class="stats">
        <Card><Stat label="爬虫" value={spiders.value.length} delta={`${spiders.value.filter((s) => s.enabled).length} 个启用`} /></Card>
        <Card><Stat label="失败" value={failed.length} color={failed.length ? 'red' : undefined} delta={failed.map((s) => s.name).join('、') || '一切正常'} /></Card>
        <Async q={q} lines={1}>{(d) => <Card><Stat label="系统盘" value={`${d.disk.system?.pct ?? 0}%`} delta={`${fmtBytes(d.disk.system?.used)} / ${fmtBytes(d.disk.system?.total)}`} /></Card>}</Async>
        <Async q={q} lines={1}>{(d) => <Card><Stat label="MongoDB" value={fmtBytes(d.mongo.total_bytes)} delta={Object.values(d.mongo.dbs).reduce((a, b) => a + b.collections, 0) + ' 个集合'} /></Card>}</Async>
      </div>

      <div class="grid">
        <Card title="💾 备份" sub={<button class="btn sm" disabled={busy} onClick={backup}>立即备份</button>}>
          <Async q={q}>{(d) => (
            <div class="col">
              <div class="row">
                {d.backup.finished_at
                  ? <Badge tone={d.backup.ok ? 'green' : 'red'}>{d.backup.ok ? '上次成功' : '上次失败'}</Badge>
                  : <Badge>尚未备份</Badge>}
                <span class="muted small">{fmtDE(d.backup.finished_at)} · {fmtRel(d.backup.finished_at)} · 耗时 {fmtDur(d.backup.duration_seconds)}</span>
              </div>
              {d.backup.errors?.map((e) => <div class="error" key={e}>{e}</div>)}
              {d.drive && (
                <>
                  <div class="row small"><span class="muted">Google Drive</span><span style={{ marginLeft: 'auto' }}>{fmtBytes(d.drive.used)} / {fmtBytes(d.drive.total)}</span></div>
                  <Bar pct={(d.drive.used / d.drive.total) * 100} color={d.drive.used / d.drive.total > 0.85 ? 'red' : undefined} />
                </>
              )}
            </div>
          )}</Async>
        </Card>

        <Card title="🗄️ 存储" sub={<span title="磁盘统计有缓存，后台定时刷新">{q.data?.cached_at ? `${fmtRel(q.data.cached_at)}统计` : ''}</span>}>
          <Async q={q}>{(d) => (
            <table class="table">
              <tbody>
                {Object.entries(d.disk).filter(([k]) => k !== 'system').map(([k, v]) => (
                  <tr key={k}><td>📁 {k}</td><td class="num">{fmtBytes(v.bytes)}</td><td class="num muted">{fmtNum(v.count ?? 0)} 文件</td></tr>
                ))}
                {Object.entries(d.mongo.dbs).map(([k, v]) => (
                  <tr key={k}><td>🍃 {k}</td><td class="num">{fmtBytes(v.bytes)}</td><td class="num muted">{v.collections} 集合</td></tr>
                ))}
              </tbody>
            </table>
          )}</Async>
        </Card>

        <Card title="⏱️ 最近运行" sub="本次启动以来">
          {!spiders.value.some((s) => s.last_run_at) && <Empty>服务刚启动，还没有任务跑过</Empty>}
          <table class="table">
            <tbody>
              {[...spiders.value].filter((s) => s.last_run_at).sort((a, b) => (b.last_run_at ?? '').localeCompare(a.last_run_at ?? '')).slice(0, 8).map((s) => {
                const st = statusOf(s);
                return <tr key={s.spider_id}><td>{s.name}</td><td><Badge tone={st.tone}>{st.text}</Badge></td><td class="num muted">{fmtRel(s.last_run_at)}</td></tr>;
              })}
            </tbody>
          </table>
        </Card>
      </div>

      <Joybuy />
    </div>
  );
}
