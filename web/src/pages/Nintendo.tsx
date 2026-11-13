import { useMemo, useState } from 'preact/hooks';
import { oneOf, usePersisted } from '../lib/persist';
import { fmtDE } from '../lib/format';
import { useQuery } from '../lib/query';
import { Async, Badge, Card, Empty, Stat, Table } from '../ui';

interface Game {
  pid: string; title: string; original_price: number; current_price: number; discount_percent: number;
  updated_at?: string; on_sale?: boolean; sale_ended_at?: string | null;
}
type Sort = 'discount' | 'price' | 'updated';
function legacySort(): Sort {
  try { const v = localStorage.getItem('nin-sort'); if (v === 'price' || v === 'updated') return v; } catch { /* ignore */ }
  return 'discount';
}

const onSale = (g: Game) => g.on_sale !== false;
const ts = (s?: string | null) => (s ? new Date(s).getTime() : 0);

/**
 * 港服 eShop 折扣。数据源是「特价」列表页，所以库里每条记录都打过折；爬虫每次完整抓取后
 * 把不在列表里的标成「已结束」（on_sale=false），默认只显示在打折的。
 * 全量一次取回（~1000 款），筛选与排序在浏览器里做。搜索仍走后端：简体→繁体转换在服务端。
 */
export function NintendoPage() {
  const all = useQuery<Game[]>('/api/nintendo/prices', { staleMs: 300_000 });
  const fresh = useQuery<Game[]>('/api/nintendo/discounts/new', { staleMs: 300_000 });
  const [minDisc, setMinDisc] = usePersisted('nintendo.minDisc', 30);
  const [maxPrice, setMaxPrice] = usePersisted('nintendo.maxPrice', '');
  const [q, setQ] = useState('');
  // 旧 key `nin-sort` 是裸字符串，读一次当默认值，之后统一存到 ui.nintendo.sort
  const [sort, pickSort] = usePersisted<Sort>('nintendo.sort', legacySort(), oneOf('discount', 'price', 'updated'));
  const [withEnded, setWithEnded] = usePersisted('nintendo.withEnded', false);
  const [showAll, setShowAll] = useState(false);
  const search = useQuery<Game[]>(q.trim().length >= 1 ? `/api/nintendo/search?q=${encodeURIComponent(q.trim())}` : null, { staleMs: 300_000 });

  const rows = useMemo(() => {
    const searching = !!q.trim();
    const src = searching ? search.data ?? [] : all.data ?? [];
    const mp = parseFloat(maxPrice);
    const cmp: Record<Sort, (a: Game, b: Game) => number> = {
      discount: (a, b) => b.discount_percent - a.discount_percent,
      price: (a, b) => a.current_price - b.current_price,
      updated: (a, b) => ts(b.updated_at) - ts(a.updated_at),
    };
    return src
      // 搜索时不藏已结束的：搜一个游戏多半是想知道「它打没打过折、打到多少」
      .filter((g) => searching || withEnded || onSale(g))
      .filter((g) => searching || g.discount_percent >= minDisc)
      .filter((g) => !(mp > 0) || g.current_price <= mp)
      // 在打折的永远排在已结束的前面，组内再按所选方式排
      .sort((a, b) => Number(onSale(b)) - Number(onSale(a)) || cmp[sort](a, b));
  }, [all.data, search.data, q, minDisc, maxPrice, sort, withEnded]);

  const live = (all.data ?? []).filter(onSale).length;
  const ended = (all.data ?? []).length - live;
  return (
    <div class="page">
      <div class="stats">
        <Card><Stat label="正在打折" value={all.data ? live : '…'} /></Card>
        <Card><Stat label="今日新增折扣" value={fresh.data?.length ?? '…'} color={fresh.data?.length ? 'accent' : undefined} /></Card>
        <Card><Stat label="已结束（留档）" value={all.data ? ended : '…'} /></Card>
      </div>

      {!!fresh.data?.length && (
        <Card title="🆕 今日新增折扣" sub="新开始、重新打折或降价">
          <div class="row">{fresh.data.map((g) => <Badge key={g.pid} tone="accent">{g.title} · HK${g.current_price} (-{g.discount_percent.toFixed(0)}%)</Badge>)}</div>
        </Card>
      )}

      <Card title="🎮 价格列表" sub={`${rows.length} 款`}>
        <div class="row" style={{ marginBottom: 12 }}>
          <input class="input" style={{ flex: '1 1 220px' }} placeholder="搜索游戏名（简体自动转繁体，含已结束）" value={q} onInput={(e) => setQ((e.target as HTMLInputElement).value)} />
          <label class="row small muted">折扣 ≥
            <input class="input" type="number" style={{ width: '70px' }} value={minDisc} disabled={!!q.trim()} onInput={(e) => setMinDisc(Number((e.target as HTMLInputElement).value) || 0)} />%
          </label>
          <label class="row small muted">价格 ≤ HK$
            <input class="input" type="number" style={{ width: '80px' }} placeholder="不限" value={maxPrice} onInput={(e) => setMaxPrice((e.target as HTMLInputElement).value)} />
          </label>
          <select class="input" value={sort} onChange={(e) => pickSort((e.target as HTMLSelectElement).value as Sort)}>
            <option value="discount">按折扣</option><option value="price">按现价</option><option value="updated">按更新时间</option>
          </select>
          <label class="row small muted" style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={withEnded} disabled={!!q.trim()} onChange={(e) => setWithEnded((e.target as HTMLInputElement).checked)} />含已结束
          </label>
        </div>
        <Async q={q.trim() ? search : all} lines={8}>{() => rows.length ? (
          <Table rows={rows as unknown as Record<string, unknown>[]} rowKey={(r) => String(r.pid)} showAll={showAll} onShowAll={() => setShowAll(true)} limit={100}
            cols={[
              { key: 'title', label: '游戏', render: (r) => onSale(r as unknown as Game) ? String(r.title) : <span class="muted">{String(r.title)}</span> },
              { key: 'original_price', label: '原价', num: true, render: (r) => `HK$${r.original_price}` },
              { key: 'current_price', label: '现价', num: true, render: (r) => onSale(r as unknown as Game)
                ? <b>HK${String(r.current_price)}</b>
                : <span class="muted" title="折扣已结束，这是结束前的折后价">HK${String(r.current_price)}</span> },
              { key: 'discount_percent', label: '折扣', num: true, render: (r) => {
                const g = r as unknown as Game;
                if (!onSale(g)) return <Badge title={`结束于 ${fmtDE(g.sale_ended_at, true)}`}>已结束</Badge>;
                return g.discount_percent > 0 ? <Badge tone={g.discount_percent >= 70 ? 'green' : 'accent'}>-{g.discount_percent.toFixed(0)}%</Badge> : '—';
              } },
              { key: 'updated_at', label: '更新', render: (r) => <span class="muted small" style={{ whiteSpace: 'nowrap' }}>{fmtDE(r.updated_at as string, true)}</span> },
            ]} />
        ) : <Empty>没有满足条件的游戏</Empty>}</Async>
      </Card>
    </div>
  );
}
