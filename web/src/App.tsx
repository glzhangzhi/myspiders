import { useEffect, useState } from 'preact/hooks';
import { lazy, LocationProvider, Route, Router, useLocation } from 'preact-iso';
import { ClaudeUsagePage } from './pages/ClaudeUsage';
import { Embedded } from './pages/Embedded';
import { NintendoPage } from './pages/Nintendo';
import { QimenPage } from './pages/Qimen';
import { SystemPage } from './pages/System';
import { legacyHashRoute, loadNav, moduleErrors, navById, navLoaded } from './shell/nav';
import { Palette } from './shell/Palette';
import { mobileOpen, Sidebar } from './shell/Sidebar';
import { loadSpiders, SpidersDrawer, useSpiderStream } from './shell/spiders';
import { TokenDialog } from './shell/TokenDialog';
import { Toolbox } from './shell/Toolbox';
import { toastError, toasts } from './lib/toast';
import { Empty, Skeleton } from './ui';

// 入籍考试页体积较大、只在手机刷题时打开：按需加载，不进首屏包
const LidPage = lazy(() => import('./pages/Lid').then((m) => m.LidPage));

const NATIVE_PAGES: Record<string, () => preact.JSX.Element> = {
  health: SystemPage, nintendo: NintendoPage, qimen: QimenPage, claude_usage: ClaudeUsagePage, lid: LidPage,
};
const DEFAULT = 'health';

function Page(p: { params?: { id?: string } }) {
  const id = p.params?.id || DEFAULT;
  const item = navById.value.get(id);
  const Native = NATIVE_PAGES[id];
  useEffect(() => { document.title = `${item?.title ?? 'MySpiders'} · MySpiders`; }, [item]);
  if (Native) return <Native />;
  if (!navLoaded.value) return <div class="page"><Skeleton lines={6} h={18} /></div>;
  if (item?.legacy_url) return <Embedded src={item.legacy_url} title={item.title} />;
  return <div class="page"><Empty>没有这个页面：{id}</Empty></div>;
}

function Topbar() {
  const loc = useLocation();
  const id = loc.path.replace(/^\/dashboard\/?/, '').split('/')[0] || DEFAULT;
  const item = navById.value.get(id);
  return (
    <header class="topbar">
      <button class="btn ghost sm show-mobile" onClick={() => (mobileOpen.value = true)} aria-label="打开导航">☰</button>
      <h1>{item?.icon} {item?.title ?? '系统'}</h1>
      <span class="spacer" />
      {moduleErrors.value.length > 0 && <span class="badge red" title={moduleErrors.value.map((m) => `${m.id}: ${m.error}`).join('\n')}>{moduleErrors.value.length} 个模块加载失败</span>}
    </header>
  );
}

function Shell() {
  const loc = useLocation();
  const [overlay, setOverlay] = useState<null | 'spiders' | 'tools' | 'palette'>(null);
  const conn = useSpiderStream();

  useEffect(() => {
    loadNav().catch(toastError);
    loadSpiders().catch(() => {});
    // 旧面板的 hash 书签（/dashboard#mercor）
    const r = legacyHashRoute(location.hash);
    if (r) { history.replaceState(null, '', r); loc.route(r, true); }
    const k = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setOverlay((o) => (o === 'palette' ? null : 'palette')); }
    };
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, []);

  const close = () => setOverlay(null);
  return (
    <div class="app">
      <Sidebar onSpiders={() => setOverlay('spiders')} onTools={() => setOverlay('tools')} onPalette={() => setOverlay('palette')} />
      <div class="main">
        <Topbar />
        <main class="content">
          <Router>
            <Route path="/dashboard" component={Page} />
            <Route path="/dashboard/:id" component={Page} />
            <Route default component={Page} />
          </Router>
        </main>
      </div>
      {overlay === 'spiders' && <SpidersDrawer onClose={close} conn={conn} />}
      {overlay === 'tools' && <Toolbox onClose={close} />}
      {overlay === 'palette' && <Palette onClose={close} onSpiders={() => setOverlay('spiders')} onTools={() => setOverlay('tools')} />}
      <TokenDialog />
      <div class="toasts" aria-live="polite">{toasts.value.map((t) => <div key={t.id} class={`toast ${t.kind}`}>{t.text}</div>)}</div>
    </div>
  );
}

export function App() {
  return <LocationProvider><Shell /></LocationProvider>;
}
