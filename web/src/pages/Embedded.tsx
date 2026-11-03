import { useEffect, useRef, useState } from 'preact/hooks';
import { applyTheme, themeMode } from '../lib/theme';
import { Skeleton } from '../ui';

/** 尚未迁移的旧页面：同源 iframe 嵌入，并把主题同步进去。 */
export function Embedded(p: { src: string; title: string }) {
  const ref = useRef<HTMLIFrameElement>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => { setLoaded(false); }, [p.src]);
  useEffect(() => {
    const doc = ref.current?.contentDocument;
    if (doc && loaded) applyTheme(doc, themeMode.value);
  }, [themeMode.value, loaded]);
  return (
    <div style={{ position: 'relative', height: '100%' }}>
      {!loaded && <div style={{ position: 'absolute', inset: 0, padding: '22px' }}><Skeleton lines={6} h={18} /></div>}
      <iframe ref={ref} class="embed" src={p.src} title={p.title}
        style={{ visibility: loaded ? 'visible' : 'hidden' }}
        onLoad={() => { setLoaded(true); if (ref.current?.contentDocument) applyTheme(ref.current.contentDocument, themeMode.value); }} />
    </div>
  );
}
