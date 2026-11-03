import { useState } from 'preact/hooks';
import { api } from '../lib/api';
import { toast, toastError } from '../lib/toast';
import { tokenPrompt, token } from '../lib/token';
import { Card, Drawer } from '../ui';

export function Toolbox(p: { onClose: () => void }) {
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [topic, setTopic] = useState('spider');
  const [restart, setRestart] = useState('');
  const [dsh, setDsh] = useState<string | null>(null);

  const send = async (e: Event) => {
    e.preventDefault();
    try { await api('/push/send', { body: { title, body, topic, priority: 'low' } }); toast('推送成功', 'ok'); setTitle(''); setBody(''); }
    catch (x) { toastError(x); }
  };
  const doRestart = async () => {
    if (!confirm('确定重启服务？页面会断开几秒。')) return;
    try {
      await api('/api/system/restart-service', { method: 'POST' });
      setRestart('等待服务恢复…');
      await new Promise((r) => setTimeout(r, 2500));
      for (let i = 0; i < 15; i++) {
        try { if ((await fetch('/api/health', { signal: AbortSignal.timeout(3000) })).ok) { setRestart('✅ 已恢复'); toast('服务重启成功', 'ok'); return; } } catch { /* 还没起来 */ }
        await new Promise((r) => setTimeout(r, 2000));
      }
      setRestart('⚠️ 30 秒内未恢复，请手动检查');
    } catch (x) { toastError(x); }
  };
  const loadDsh = async () => {
    try { const r = await api<{ url: string | null; error?: string }>('/api/system/dsh-url'); setDsh(r.url ?? r.error ?? '未取到'); }
    catch (x) { toastError(x); }
  };
  return (
    <Drawer title="🧰 工具箱" onClose={p.onClose}>
      <Card title="🔑 管理令牌">
        <div class="row"><span class="muted small">{token.value ? '已设置' : '未设置 —— 写操作会失败'}</span>
          <button class="btn sm" style={{ marginLeft: 'auto' }} onClick={() => (tokenPrompt.value = true)}>{token.value ? '更换 / 退出' : '设置'}</button></div>
      </Card>
      <Card title="📤 ntfy 推送">
        <form class="col" onSubmit={send}>
          <input class="input" placeholder="标题" value={title} onInput={(e) => setTitle((e.target as HTMLInputElement).value)} />
          <textarea class="input" rows={2} placeholder="内容" value={body} onInput={(e) => setBody((e.target as HTMLTextAreaElement).value)} />
          <div class="row">
            <select class="input" value={topic} onChange={(e) => setTopic((e.target as HTMLSelectElement).value)}>
              <option value="spider">spider（通用）</option><option value="nintendo">nintendo</option><option value="monitor">monitor</option><option value="claude">claude</option>
            </select>
            <button class="btn primary" style={{ marginLeft: 'auto' }} disabled={!title.trim()}>发送</button>
          </div>
        </form>
      </Card>
      <Card title="🖥️ DSH 登录链接">
        <div class="row"><button class="btn sm" onClick={loadDsh}>获取</button>
          {dsh && <button class="btn sm" onClick={() => navigator.clipboard.writeText(dsh).then(() => toast('已复制', 'ok'))}>复制</button>}</div>
        {dsh && <div class="mono small" style={{ wordBreak: 'break-all', marginTop: 8 }}>{dsh}</div>}
      </Card>
      <Card title="🔄 服务">
        <div class="row"><button class="btn danger" onClick={doRestart}>重启 myspider</button><span class="small muted">{restart}</span></div>
      </Card>
    </Drawer>
  );
}
