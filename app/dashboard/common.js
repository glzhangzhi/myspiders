// MySpiders dashboard 共享前端工具
// 由各页面通过 <script src="/dashboard/common.js"></script> 在主脚本之前引入。

// 顶部短暂提示条。依赖页面存在 <div id="toast"> 及 .toast / .toast.show 样式。
function showToast(msg, type = '') {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'toast show ' + type;
  setTimeout(() => { t.className = 'toast'; }, 3000);
}

// 把结果（字符串或对象）写入指定的 result-box 元素。
function R(id, text, cls = 'ok') {
  const el = document.getElementById(id);
  if (!el) return;
  if (typeof text === 'object') text = JSON.stringify(text, null, 2);
  el.textContent = text;
  el.className = 'result-box visible ' + cls;
}

// 读取本域保存的 admin token（localStorage 按 origin 隔离）。
function getToken() {
  return localStorage.getItem('myspiders-token') || '';
}

// ── 界面选择状态的本地记忆（标签、时间跨度、筛选、排序…）──
// key 命名 `<页面>.<东西>`，如 media.tab、mercor2.trendSpan。值一律按字符串存取：
// 恢复时调用方自己校验（选项可能已被删掉），不合法就回退默认值。
// 隐私模式 / 存储满 / 被禁用时 localStorage 会抛错，这里吞掉 —— 记不住不该让页面挂掉。
function lsGet(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch (e) { return fallback; }
}
function lsSet(key, value) {
  try { localStorage.setItem(key, String(value)); } catch (e) {}
}

// 轻量 admin token 输入条：在右上角挂一个输入框 + 保存按钮。
// 历史原因：tele./av. 曾是独立子域，与主面板不同源、localStorage 不共享，需各自设置一次。
// 迁到本机后只有一个 origin(localhost:12312)，且这两个页面已作为同源 iframe 内嵌进主面板
// 的「🎬 影片」/「✈️ Telegram」标签，token 与主面板共享 —— 此时本输入条不会显示
// (getToken() 已有值时被跳过)。直接打开 /dashboard/av 等页面时它仍是兜底入口。
// 保存后写入本域 localStorage 并重载页面（让脚本重新读取 token）。
function mountTokenBar() {
  if (document.getElementById('common-token-bar')) return;
  const cur = getToken();
  // 已有 token 就不必挂了 —— 它是 position:fixed，内嵌进主面板时会浮在页面右上角挡内容。
  // 需要改 token 走主面板的设置入口；此处只作为「本域还没 token」时的兜底。
  if (cur) return;
  const wrap = document.createElement('div');
  wrap.id = 'common-token-bar';
  wrap.style.cssText = 'position:fixed;top:8px;right:8px;z-index:99999;display:flex;gap:6px;align-items:center;background:#222;color:#eee;padding:6px 8px;border-radius:8px;font:13px system-ui,sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.35)';
  const inp = document.createElement('input');
  inp.type = 'password';
  inp.placeholder = 'admin token';
  inp.value = cur;
  inp.style.cssText = 'padding:4px 6px;border:1px solid #444;border-radius:5px;background:#111;color:#eee;width:110px';
  const btn = document.createElement('button');
  btn.textContent = cur ? '✓ 已设置' : '保存';
  btn.title = '保存 admin token 到本域';
  btn.style.cssText = 'padding:4px 10px;border:0;border-radius:5px;background:var(--accent,#5fb3a3);color:#15211e;cursor:pointer;white-space:nowrap';
  btn.onclick = () => {
    localStorage.setItem('myspiders-token', inp.value.trim());
    location.reload();
  };
  wrap.appendChild(inp);
  wrap.appendChild(btn);
  document.body.appendChild(wrap);
}

// 简易 HTML 转义（common.js 自带，避免依赖各页面的 esc）。
function esc2(s) { const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }

// 属性值转义。注意 esc2 走的是 textContent→innerHTML，**不会转义引号** ——
// 用在 attr="..." 里时，数据一旦含 " 就会提前闭合属性。放进属性一律用这个。
function escAttr(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── 即时悬浮提示（图表柱子等窄元素专用）────────────────────
// 原生 title 有约 1 秒延迟、样式不可控、还会被 iframe 边界截断，
// 用在柱状图上体验很差（鼠标扫过去要等）。这里用事件委托实现零延迟版本。
//
// 用法：给元素加 data-tip="第一行\n第二行"，其余自动。
// 支持 \n 换行；文本一律转义，不接受 HTML（这些内容来自接口数据）。
// 元素同时有 title 时本函数会接管，title 由调用方自行决定去留。
(function initTipLayer() {
  if (window.__tipReady) return;
  window.__tipReady = true;

  let el = null;

  function ensure() {
    if (el) return el;
    el = document.createElement('div');
    el.id = 'dsh-tip';
    el.style.cssText = [
      'position:fixed', 'z-index:100001', 'pointer-events:none',
      'background:var(--surface2,#322e2b)', 'border:1px solid var(--border,#403a36)', 'border-radius:7px',
      'padding:7px 10px', 'font-size:12px', 'line-height:1.55', 'color:var(--text,#e8e2d9)',
      // pre-wrap 而不是 pre：pre 不折行，长行会冲出 max-width 的框
      'box-shadow:0 6px 22px rgba(0,0,0,.5)', 'white-space:pre-wrap', 'overflow-wrap:anywhere',
      'font-variant-numeric:tabular-nums', 'opacity:0',
      'transition:opacity .09s', 'max-width:320px',
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif',
    ].join(';');
    document.body.appendChild(el);
    return el;
  }

  function place(ev) {
    const t = ensure();
    const pad = 12;
    const r = t.getBoundingClientRect();
    // 默认放在光标右上方；贴边时翻转，避免被视口裁掉。
    let x = ev.clientX + pad;
    let y = ev.clientY - r.height - pad;
    if (x + r.width > window.innerWidth - 6) x = ev.clientX - r.width - pad;
    if (y < 6) y = ev.clientY + pad;
    if (x < 6) x = 6;
    t.style.left = x + 'px';
    t.style.top = y + 'px';
  }

  function show(target, ev) {
    const tip = target.getAttribute('data-tip');
    if (!tip) return;
    const t = ensure();
    t.textContent = tip;          // textContent 天然转义，且保留 \n（white-space:pre）
    t.style.opacity = '1';
    place(ev);
  }

  function hide() {
    if (el) el.style.opacity = '0';
  }

  document.addEventListener('mouseover', function (ev) {
    const target = ev.target.closest && ev.target.closest('[data-tip]');
    if (target) show(target, ev);
  });
  document.addEventListener('mousemove', function (ev) {
    if (el && el.style.opacity === '1' && ev.target.closest && ev.target.closest('[data-tip]')) place(ev);
  });
  document.addEventListener('mouseout', function (ev) {
    const target = ev.target.closest && ev.target.closest('[data-tip]');
    if (target && !target.contains(ev.relatedTarget)) hide();
  });
  // 滚动/切换标签时元素可能移走，留着的提示会变成孤儿
  window.addEventListener('scroll', hide, true);
  window.addEventListener('blur', hide);
})();

// ── Telegram 扫码登录弹窗 ──────────────────────────────
// 任意页面调用即可：触发后端启动扫码流程，轮询状态并实时刷新二维码，
// 扫码成功后自动提示。依赖本域已保存 admin token（getToken）。
function openTeleQrLogin() {
  if (document.getElementById('tele-qr-modal')) return;
  const token = getToken();
  const mask = document.createElement('div');
  mask.id = 'tele-qr-modal';
  mask.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center';
  const box = document.createElement('div');
  box.style.cssText = 'background:var(--surface,#262322);border:1px solid var(--border,#403a36);border-radius:12px;padding:20px;width:320px;max-width:90vw;text-align:center;color:var(--text,#e8e2d9);font:14px system-ui,sans-serif;box-shadow:0 8px 40px rgba(0,0,0,.5)';
  box.innerHTML =
    '<div style="font-size:15px;font-weight:600;margin-bottom:6px">📲 Telegram 扫码登录</div>' +
    '<div style="font-size:12px;color:var(--text-dim,#9a938a);margin-bottom:12px">手机 Telegram → 设置 → 设备 → 链接桌面设备，扫下方二维码（自动刷新）</div>' +
    '<div id="tele-qr-body" style="min-height:240px;display:flex;align-items:center;justify-content:center"><span style="color:var(--text-dim,#9a938a)">正在生成二维码…</span></div>' +
    '<div id="tele-qr-status" style="font-size:12px;color:var(--text-dim,#9a938a);margin-top:10px"></div>' +
    '<button id="tele-qr-close" style="margin-top:14px;padding:6px 16px;border:1px solid var(--border,#403a36);border-radius:6px;background:transparent;color:var(--text-dim,#9a938a);cursor:pointer">关闭</button>';
  mask.appendChild(box);
  document.body.appendChild(mask);

  let timer = null;
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  const close = () => { stop(); mask.remove(); };
  box.querySelector('#tele-qr-close').onclick = close;
  mask.onclick = (e) => { if (e.target === mask) close(); };

  const body = box.querySelector('#tele-qr-body');
  const statusEl = box.querySelector('#tele-qr-status');

  async function poll() {
    try {
      const r = await fetch('/api/telegram/qr-login/status', { headers: { 'x-token': token } });
      if (!r.ok) { statusEl.textContent = '状态查询失败 (HTTP ' + r.status + ')'; return; }
      const d = await r.json();
      if (d.status === 'success') {
        stop();
        body.innerHTML = '<div style="font-size:48px">✅</div>';
        statusEl.innerHTML = '登录成功' + (d.account ? '：<b>' + esc2(d.account) + '</b>' : '') + '，可以关闭了。';
        statusEl.style.setProperty('color', 'var(--success,#6fbf8a)');
      } else if (d.status === 'error') {
        stop();
        statusEl.textContent = '登录失败：' + (d.error || '未知错误');
        statusEl.style.setProperty('color', 'var(--red,#e0707f)');
      } else if (d.qr_png) {
        body.innerHTML = '<img alt="二维码" style="width:240px;height:240px;background:#fff;border-radius:6px" src="' + d.qr_png + '">';
        statusEl.textContent = '等待扫码…（二维码会自动刷新）';
        statusEl.style.setProperty('color', 'var(--text-dim,#9a938a)');
      }
    } catch (e) {
      statusEl.textContent = '网络错误：' + e.message;
    }
  }

  // 先启动后台扫码流程，再开始轮询当前二维码 / 状态
  fetch('/api/telegram/qr-login/start', { method: 'POST', headers: { 'x-token': token } })
    .then(r => {
      if (r.status === 401) { statusEl.textContent = 'admin token 无效或未设置'; statusEl.style.setProperty('color', 'var(--red,#e0707f)'); throw new Error('401'); }
    })
    .then(() => { poll(); timer = setInterval(poll, 2000); })
    .catch(() => {});
}

// ── Telegram 会话失效警告弹窗 ──────────────────────────
// 后端某操作返回 {code:'tele_session_expired'} 时调用：告知用户并提供
// 「扫码登录」按钮直接进入登录流程。
function teleSessionPopup(service) {
  if (document.getElementById('tele-expired-modal')) return;
  const mask = document.createElement('div');
  mask.id = 'tele-expired-modal';
  mask.style.cssText = 'position:fixed;inset:0;z-index:100001;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center';
  const box = document.createElement('div');
  box.style.cssText = 'background:var(--surface,#262322);border:1px solid var(--red,#e0707f);border-radius:12px;padding:20px;width:340px;max-width:90vw;color:var(--text,#e8e2d9);font:14px system-ui,sans-serif;box-shadow:0 8px 40px rgba(0,0,0,.5)';
  box.innerHTML =
    '<div style="font-size:15px;font-weight:600;margin-bottom:8px;color:var(--red,#e0707f)">⚠️ Telegram 登录已失效</div>' +
    '<div style="font-size:13px;line-height:1.6;margin-bottom:14px">' +
    (service ? '「' + esc2(service) + '」' : '某个操作') +
    '运行时检测到 Telegram 会话失效，需要重新扫码登录后才能继续使用。</div>' +
    '<div style="display:flex;gap:8px;justify-content:flex-end">' +
    '<button id="tele-expired-later" style="padding:6px 14px;border:1px solid var(--border,#403a36);border-radius:6px;background:transparent;color:var(--text-dim,#9a938a);cursor:pointer">稍后</button>' +
    '<button id="tele-expired-login" style="padding:6px 14px;border:0;border-radius:6px;background:var(--accent,#5fb3a3);color:#15211e;cursor:pointer">🔓 扫码登录</button>' +
    '</div>';
  mask.appendChild(box);
  document.body.appendChild(mask);
  box.querySelector('#tele-expired-later').onclick = () => mask.remove();
  box.querySelector('#tele-expired-login').onclick = () => { mask.remove(); openTeleQrLogin(); };
}

// ── 全局 fetch 拦截：Telegram 会话失效 ─────────────────
// 任何页面的任何请求只要后端返回 409 {code:'tele_session_expired'}，
// 就自动弹出扫码登录引导。这样所有「会用到 teleclient」的操作都被覆盖，
// 无需在每个 fetch 调用点单独处理。原响应原样返回，不影响调用方自身的错误处理。
(function installTeleSessionInterceptor() {
  if (window.__teleFetchPatched) return;
  window.__teleFetchPatched = true;
  const origFetch = window.fetch.bind(window);
  window.fetch = async function (...args) {
    const resp = await origFetch(...args);
    if (resp.status === 409) {
      try {
        const data = await resp.clone().json();
        if (data && data.code === 'tele_session_expired') teleSessionPopup(data.service || '');
      } catch (_) { /* 非 JSON 响应或读取失败，忽略 */ }
    }
    return resp;
  };
})();
