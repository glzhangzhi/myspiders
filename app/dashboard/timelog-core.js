/* 计时 · 本地存储与同步（页面与 service worker 共用：页面 <script src>，sw importScripts）
 *
 * IndexedDB 两个仓：
 *   queue  待同步的操作，keyPath=id。**只按 id 增删，从不整体覆盖** —— 页面与 sw 可能
 *          同时在同步，整体覆盖会把对方刚加的操作抹掉；按 id 删则两边各删各的、重复无害
 *          （服务端按 id 去重）。
 *   kv     token、上次同步拿到的 state、被拒收的操作留档。
 *
 * 一条规则：所有请求都 `redirect: 'manual'`。panel. 在 Cloudflare Access 后面，会话
 * 过期时请求被 302 到 cloudflareaccess.com；跟随重定向的话那是跨域且没有 CORS 头，
 * fetch 只抛一个和断网**一模一样**的 TypeError —— 于是会当成离线永远重试下去，
 * 而真正需要的是让人重新登录。manual 时拿到的是 type='opaqueredirect'，一眼分得清。
 */
var TL = (() => {
  'use strict';
  const DB = 'timelog', VER = 1, BATCH = 500;
  let dbp = null;

  function open() {
    if (!dbp) dbp = new Promise((resolve, reject) => {
      const r = indexedDB.open(DB, VER);
      r.onupgradeneeded = () => {
        const d = r.result;
        d.createObjectStore('queue', { keyPath: 'id' });
        d.createObjectStore('kv');
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => { dbp = null; reject(r.error); };
    });
    return dbp;
  }

  /** 在一个事务里跑 fn(store)，事务**提交后**才 resolve（不是请求成功就算）。 */
  async function tx(name, mode, fn) {
    const d = await open();
    return new Promise((resolve, reject) => {
      const t = d.transaction(name, mode), s = t.objectStore(name);
      let out;
      const r = fn(s);
      if (r) r.onsuccess = () => { out = r.result; };
      t.oncomplete = () => resolve(out);
      t.onerror = t.onabort = () => reject(t.error);
    });
  }

  const kvGet = k => tx('kv', 'readonly', s => s.get(k));
  const kvSet = (k, v) => tx('kv', 'readwrite', s => s.put(v, k));
  const kvDel = k => tx('kv', 'readwrite', s => s.delete(k));
  const queueAll = () => tx('queue', 'readonly', s => s.getAll());
  const queuePut = op => tx('queue', 'readwrite', s => s.put(op));
  const queueDelete = ids => tx('queue', 'readwrite', s => { ids.forEach(id => s.delete(id)); });

  /** 带 token 的接口调用。返回 {status, body?}；status ∈ ok/offline/login/bad-token/error。 */
  async function api(method, path, body) {
    const token = await kvGet('token');
    if (!token) return { status: 'no-token' };
    const init = { method, headers: { 'x-token': token }, redirect: 'manual', credentials: 'same-origin', cache: 'no-store' };
    if (body !== undefined) { init.headers['content-type'] = 'application/json'; init.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, init); } catch (_) { return { status: 'offline' }; }
    if (res.type === 'opaqueredirect' || res.status === 0) return { status: 'login' };
    if (res.status === 401) return { status: 'bad-token' };
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) return { status: 'error', code: res.status, detail: data && data.detail };
    return { status: 'ok', body: data };
  }

  /**
   * 推一批待同步操作并取回最新 state；队列空时只取 state。
   * 服务端回的 accepted / duplicate / rejected **都**移出队列：前两者已入库，
   * 后者是形状错误、重发也不会成功（留档到 kv.rejected 供排查）。
   */
  async function flush() {
    const ops = (await queueAll()).sort((a, b) => a.ts - b.ts).slice(0, BATCH);
    const r = ops.length ? await api('POST', '/api/timelog/sync', { ops }) : await api('GET', '/api/timelog/state');
    if (r.status !== 'ok') return r;
    let state = r.body, rejected = [];
    if (ops.length) {
      state = r.body.state;
      rejected = r.body.rejected || [];
      const done = [...r.body.accepted, ...r.body.duplicate, ...rejected.map(x => x.id).filter(Boolean)];
      await queueDelete(done);
      if (rejected.length) {
        const byId = Object.fromEntries(ops.map(o => [o.id, o]));
        const log = (await kvGet('rejected')) || [];
        rejected.forEach(x => log.push({ at: Date.now(), reason: x.reason, op: byId[x.id] || null }));
        await kvSet('rejected', log.slice(-50));
      }
    }
    state.synced_at = Date.now();
    await kvSet('state', state);
    return { status: 'ok', state, rejected, more: ops.length === BATCH };
  }

  return { kvGet, kvSet, kvDel, queueAll, queuePut, queueDelete, api, flush };
})();
