import { useEffect, useState } from 'preact/hooks';

/** 订阅一条 SSE；断线由浏览器自动重连。返回连接状态。 */
export function useEventSource(url: string | null, event: string, onData: (data: unknown) => void): 'connecting' | 'open' | 'closed' {
  const [state, setState] = useState<'connecting' | 'open' | 'closed'>('connecting');
  useEffect(() => {
    if (!url) return;
    const es = new EventSource(url);
    es.onopen = () => setState('open');
    es.onerror = () => setState(es.readyState === EventSource.CLOSED ? 'closed' : 'connecting');
    const h = (e: MessageEvent) => { try { onData(JSON.parse(e.data)); } catch { /* 忽略坏包 */ } };
    es.addEventListener(event, h);
    return () => { es.removeEventListener(event, h); es.close(); };
  }, [url, event]);
  return state;
}
