/* 计时 · service worker
 *
 * 两件事：
 *   1. 离线也能打开页面：页面与静态资源**网络优先**，失败才用缓存 —— 改了页面刷新就能
 *      看到（与面板其他页「改 HTML 免重启」的习惯一致），只有没网时才吃缓存。
 *   2. 后台同步：离线时记的操作，联网后即使 App 已经关了也能推上去（Background Sync）。
 *      否则服务端一直看不到这段计时，会误发「已经 30 分钟没有计时」的空闲提醒。
 *
 * 接口请求（/api/）一律不经缓存。
 */
importScripts('/timelog/core.js');

const CACHE = 'timelog-shell-v1';
const SHELL = [
  '/timelog', '/timelog/core.js', '/dashboard/theme.css',
  '/timelog/manifest.webmanifest', '/timelog/icon-192.png', '/timelog/icon-512.png',
];

self.addEventListener('install', event => {
  // 不用 cache.addAll：Access 会话过期时某个资源是 302，addAll 会让整个安装失败
  event.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.all(SHELL.map(async url => {
      try {
        const res = await fetch(url, { credentials: 'same-origin', redirect: 'manual' });
        if (res.ok) await c.put(url, res);
      } catch (_) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const req = event.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  event.respondWith((async () => {
    try {
      // 原样转发（导航请求的 redirect 模式是 manual）：Access 过期时返回 opaqueredirect，
      // 浏览器会照常跳去登录页
      const res = await fetch(req);
      if (res.ok && res.type === 'basic') {
        const key = url.pathname;               // 去掉 ?action=…：同一个页面只存一份
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(key, copy));
      }
      return res;
    } catch (_) {
      const hit = await caches.match(url.pathname);
      return hit || Response.error();
    }
  })());
});

self.addEventListener('sync', event => {
  if (event.tag !== 'timelog-flush') return;
  event.waitUntil(TL.flush().then(r => {
    // 抛错 = 让浏览器稍后重试；登录过期 / 令牌错重试也没用，不抛
    if (r.status === 'offline' || r.status === 'error') throw new Error(r.status);
  }));
});
