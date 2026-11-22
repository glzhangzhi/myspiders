// 一次性的 POST + SSE：EventSource 只能 GET 且带不了 x-token 头，而且断线会自动重连 ——
// 对「查一次就完」的长查询，重连等于把整个查询再发一遍（再打几十次 Trip）。所以用 fetch 读流，
// 收到 done / busy / fail 或流结束就收手，绝不重连。
import { token, tokenPrompt } from '../../lib/token';

export interface SseHandlers { onEvent: (event: string, data: unknown) => void }

export async function postStream(path: string, body: unknown, h: SseHandlers, signal: AbortSignal): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json', Accept: 'text/event-stream' };
  if (token.value) headers['x-token'] = token.value;
  const res = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body), signal });
  if (!res.ok) {
    if (res.status === 401) tokenPrompt.value = true;
    let msg = `HTTP ${res.status}`;
    try { const j = await res.json(); if (j?.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch { /* 非 JSON */ }
    throw new Error(msg);
  }
  if (!res.body) throw new Error('浏览器不支持流式读取');
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
    let i: number;
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      let event = 'message';
      const data: string[] = [];
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      }
      if (!data.length) continue; // ping 注释行
      let parsed: unknown = data.join('\n');
      try { parsed = JSON.parse(parsed as string); } catch { /* 原样 */ }
      h.onEvent(event, parsed);
    }
  }
}
