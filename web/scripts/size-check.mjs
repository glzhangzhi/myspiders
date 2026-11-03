// 首屏体积守门：入口 JS + CSS 合计 gzip 后不得超过预算（uPlot 等按需 chunk 不计）。
import { readFileSync, readdirSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join } from 'node:path';

const BUDGET_KB = 80;
const dist = new URL('../../app/dashboard/dist/', import.meta.url).pathname;
const html = readFileSync(join(dist, 'index.html'), 'utf8');
const initial = [...html.matchAll(/(?:src|href)="\/dashboard\/(assets\/[^"]+\.(?:js|css))"/g)].map((m) => m[1]);
let total = 0;
for (const f of initial) {
  const n = gzipSync(readFileSync(join(dist, f))).length;
  total += n;
  console.log(`  ${f}  ${(n / 1024).toFixed(1)} KB gz`);
}
const lazy = readdirSync(join(dist, 'assets')).filter((f) => !initial.includes(`assets/${f}`));
console.log(`首屏 ${(total / 1024).toFixed(1)} KB gz（预算 ${BUDGET_KB} KB）；按需 chunk ${lazy.length} 个`);
if (total / 1024 > BUDGET_KB) { console.error('超出首屏体积预算'); process.exit(1); }
