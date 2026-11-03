import { useState } from 'preact/hooks';
import { Overlay } from '../ui';
import { api } from '../lib/api';
import { setToken, token, tokenPrompt } from '../lib/token';
import { toast } from '../lib/toast';

export function TokenDialog() {
  const [v, setV] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  if (!tokenPrompt.value) return null;
  const close = () => { tokenPrompt.value = false; setErr(''); };
  const save = async (e: Event) => {
    e.preventDefault();
    setBusy(true); setErr('');
    const prev = token.value;
    setToken(v);
    try {
      await api('/api/verify-token');
      toast('管理令牌已保存', 'ok');
      setV(''); close();
    } catch {
      setToken(prev); setErr('令牌无效');
    } finally { setBusy(false); }
  };
  return (
    <Overlay onClose={close}>
      <form class="dialog" onSubmit={save}>
        <h2>🔑 管理令牌</h2>
        <p class="muted small" style={{ marginTop: 0 }}>写操作与私密接口需要令牌（请求头 x-token），保存在本浏览器。</p>
        <input class="input" type="password" autoFocus autocomplete="current-password" placeholder="admin token" value={v}
          onInput={(e) => setV((e.target as HTMLInputElement).value)} style={{ width: '100%' }} />
        {err && <div class="error" style={{ marginTop: 8 }}>{err}</div>}
        <div class="row" style={{ justifyContent: 'flex-end', marginTop: 14 }}>
          {token.value && <button type="button" class="btn danger" onClick={() => { setToken(''); toast('已退出'); close(); }}>退出登录</button>}
          <button type="button" class="btn" onClick={close}>取消</button>
          <button class="btn primary" disabled={busy || !v.trim()}>保存</button>
        </div>
      </form>
    </Overlay>
  );
}
