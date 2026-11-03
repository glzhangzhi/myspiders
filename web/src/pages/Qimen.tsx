import { useState } from 'preact/hooks';
import { api } from '../lib/api';
import { localStamp } from '../lib/format';
import { Card, ErrorBox, Skeleton } from '../ui';

interface Palace { palace_num: number; di_pan_stem: string; tian_pan_stem: string; star: string; gate: string; spirit: string }
interface Pan {
  datetime_str: string; si_zhu: string; jie_qi: string; jie_qi_start: string; days_in_jie_qi: number; dun_type: string;
  yuan: string; ju_num: number; xun_shou: string; xun_yi: string; xun_kong: string[]; zhi_fu_xing: string; zhi_shi_men: string;
  di_pan: Record<string, string>; tian_pan: Record<string, string>; palaces: Record<string, Palace>; formatted_text: string;
}

const NATURE_COLOR: Record<string, string> = { 吉: 'green', 凶: 'red', 中: 'yellow' };
const DIR: Record<number, string> = { 1: '北', 2: '西南', 3: '东', 4: '东南', 5: '中', 6: '西北', 7: '西', 8: '东北', 9: '南' };
const NAME: Record<number, string> = { 1: '坎', 2: '坤', 3: '震', 4: '巽', 5: '中', 6: '乾', 7: '兑', 8: '艮', 9: '离' };
const STAR: Record<string, string> = { 天蓬: '凶', 天芮: '凶', 天冲: '吉', 天辅: '吉', 天禽: '中', 天心: '吉', 天柱: '凶', 天任: '吉', 天英: '中' };
const GATE: Record<string, string> = { 休门: '吉', 死门: '凶', 伤门: '凶', 杜门: '中', 开门: '吉', 惊门: '凶', 生门: '吉', 景门: '中' };
const LAYOUT = [[4, 9, 2], [3, 5, 7], [8, 1, 6]];

function Tag(p: { text: string; nature?: string }) {
  const c = p.nature ? NATURE_COLOR[p.nature] : undefined;
  return <span style={c ? { color: `var(--${c})` } : undefined}>{c && <span class={`dot ${c}`} style={{ width: 6, height: 6, marginRight: 4, verticalAlign: 'middle' }} />}{p.text}</span>;
}

export function QimenPage() {
  const [dt, setDt] = useState('');
  const [pan, setPan] = useState<Pan | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const run = async (e?: Event) => {
    e?.preventDefault();
    setBusy(true); setErr(null);
    // 留空用浏览器本地时间，而不是服务器时区
    try { setPan(await api<Pan>('/api/qimen', { body: { datetime_str: dt.trim() || localStamp() } })); }
    catch (x) { setErr(x); } finally { setBusy(false); }
  };
  return (
    <div class="page">
      <Card title="🔮 奇门遁甲排盘">
        <form class="row" onSubmit={run}>
          <input class="input" style={{ width: '200px' }} placeholder="YYYY-MM-DD HH:MM（留空=现在）" value={dt} onInput={(e) => setDt((e.target as HTMLInputElement).value)} />
          <button class="btn primary" disabled={busy}>{busy ? '排盘中…' : '排盘'}</button>
          <button type="button" class="btn ghost" onClick={() => setDt(localStamp())}>填入当前时间</button>
        </form>
      </Card>
      {err ? <ErrorBox error={err} /> : null}
      {busy && !pan && <Skeleton lines={8} h={20} />}
      {pan && (
        <div class="grid qm-layout">
          <Card title={`${pan.dun_type}${pan.ju_num}局 · ${pan.yuan}`} sub={pan.datetime_str}>
            <table class="qm-grid">
              <tbody>
                {LAYOUT.map((row) => (
                  <tr key={row.join()}>
                    {row.map((n) => {
                      if (n === 5) return (
                        <td key={n} class="center">
                          <div class="pos"><span>中 · 五宫</span><span style={{ color: 'var(--yellow)' }}>寄坤</span></div>
                          <div class="ln muted" style={{ justifyContent: 'center' }}>天禽</div>
                          <div class="ln" style={{ justifyContent: 'center' }}>天{pan.tian_pan['5'] || pan.tian_pan['2']} · 地{pan.di_pan['5'] || pan.di_pan['2']}</div>
                        </td>
                      );
                      const p = pan.palaces[String(n)];
                      if (!p) return <td key={n} />;
                      return (
                        <td key={n}>
                          <div class="pos"><span>{DIR[n]} · {NAME[n]}{n}宫</span></div>
                          <div class="ln"><span style={{ color: 'var(--blue)' }}>{p.spirit}</span><Tag text={p.star} nature={STAR[p.star]} /></div>
                          <div class="ln"><span>天 <b>{p.tian_pan_stem}</b></span><Tag text={p.gate} nature={GATE[p.gate]} /></div>
                          <div class="ln muted"><span>地 {p.di_pan_stem}</span></div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <Card title="盘面信息">
            <table class="table">
              <tbody>
                <tr><td class="muted">四柱</td><td>{pan.si_zhu}</td></tr>
                <tr><td class="muted">节气</td><td>{pan.jie_qi}（{pan.jie_qi_start} 起，第 {pan.days_in_jie_qi + 1} 天）</td></tr>
                <tr><td class="muted">旬首</td><td>{pan.xun_shou} 旬（仪 = {pan.xun_yi}）</td></tr>
                <tr><td class="muted">旬空</td><td>{pan.xun_kong.join('、')}</td></tr>
                <tr><td class="muted">值符</td><td>{pan.zhi_fu_xing}</td></tr>
                <tr><td class="muted">值使</td><td>{pan.zhi_shi_men}</td></tr>
              </tbody>
            </table>
            <details style={{ marginTop: 12 }}>
              <summary class="muted small" style={{ cursor: 'pointer' }}>排盘步骤</summary>
              <pre class="mono" style={{ whiteSpace: 'pre-wrap', margin: '8px 0 0' }}>{pan.formatted_text}</pre>
            </details>
          </Card>
        </div>
      )}
    </div>
  );
}
