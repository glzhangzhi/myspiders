import { useMemo, useState } from 'preact/hooks';
import { fmtDE } from '../lib/format';
import { useQuery } from '../lib/query';
import { Async, Badge, Card, Empty, Stat, Table } from '../ui';

interface Game { pid: string; title: string; original_price: number; current_price: number; discount_percent: number; updated_at?: string }

/**
 * 港服 eShop 折扣。全量价格一次取回（~1000 款），筛选与排序在浏览器里做 ——
 * 原先每改一个条件发一次请求、结果是一大段纯文本。搜索仍走后端：简体→繁体转换在服务端。
 */
export function NintendoPage() {
  const all = useQuery<Game[]>('/api/nintendo/prices', { staleMs: 300_000 });
  const fresh = useQuery<Game[]>('/api/nintendo/discounts/new', { staleMs: 300_000 });
  const [minDisc, setMinDisc] = useState(30);
  const [maxPrice, setMaxPrice] = useState('');
  const [q, setQ] = useState('');
  const [sort, setSort] = useState<'discount' | 'price'>('discount');
  const [showAll, setShowAll] = useState(false);
  const search = useQuery<Game[]>(q.trim().length >= 1 ? `/api/nintendo/search?q=${encodeURIComponent(q.trim())}` : null, { staleMs: 300_000 });

  const rows = useMemo(() => {
    const src = q.trim() ? search.data ?? [] : all.data ?? [];
    const mp = parseFloat(maxPrice);
    return src
      .filter((g) => q.trim() || g.discount_percent >= minDisc)
      .filter((g) => !(mp > 0) || g.current_price <= mp)
      .sort((a, b) => sort === 'discount' ? b.discount_percent - a.discount_percent : a.current_price - b.current_price);
  }, [all.data, search.data, q, minDisc, maxPrice, sort]);

  const discounted = (all.data ?? []).filter((g) => g.discount_percent > 0).length;
  return (
    <div class="page">
      <div class="stats">
        <Card><Stat label="追踪游戏" value={all.data?.length ?? '…'} /></Card>
        <Card><Stat label="正在打折" value={all.data ? discounted : '…'} /></Card>
        <Card><Stat label="今日新增折扣" value={fresh.data?.length ?? '…'} color={fresh.data?.length ? 'green' : undefined} /></Card>
      </div>

      {!!fresh.data?.length && (
        <Card title="🆕 今日新增折扣">
          <div class="row">{fresh.data.map((g) => <Badge key={g.pid} tone="green">{g.title} · HK${g.current_price} (-{g.discount_percent.toFixed(0)}%)</Badge>)}</div>
        </Card>
      )}

      <Card title="🎮 价格列表" sub={`${rows.length} 款`}>
        <div class="row" style={{ marginBottom: 12 }}>
          <input class="input" style={{ flex: '1 1 220px' }} placeholder="搜索游戏名（简体自动转繁体）" value={q} onInput={(e) => setQ((e.target as HTMLInputElement).value)} />
          <label class="row small muted">折扣 ≥
            <input class="input" type="number" style={{ width: '70px' }} value={minDisc} disabled={!!q.trim()} onInput={(e) => setMinDisc(Number((e.target as HTMLInputElement).value) || 0)} />%
          </label>
          <label class="row small muted">价格 ≤ HK$
            <input class="input" type="number" style={{ width: '80px' }} placeholder="不限" value={maxPrice} onInput={(e) => setMaxPrice((e.target as HTMLInputElement).value)} />
          </label>
          <select class="input" value={sort} onChange={(e) => setSort((e.target as HTMLSelectElement).value as 'discount' | 'price')}>
            <option value="discount">按折扣</option><option value="price">按现价</option>
          </select>
        </div>
        <Async q={q.trim() ? search : all} lines={8}>{() => rows.length ? (
          <Table rows={rows as unknown as Record<string, unknown>[]} rowKey={(r) => String(r.pid)} showAll={showAll} onShowAll={() => setShowAll(true)} limit={100}
            cols={[
              { key: 'title', label: '游戏' },
              { key: 'original_price', label: '原价', num: true, render: (r) => `HK$${r.original_price}` },
              { key: 'current_price', label: '现价', num: true, render: (r) => <b>HK${String(r.current_price)}</b> },
              { key: 'discount_percent', label: '折扣', num: true, render: (r) => (r.discount_percent as number) > 0 ? <Badge tone={(r.discount_percent as number) >= 70 ? 'green' : 'accent'}>-{(r.discount_percent as number).toFixed(0)}%</Badge> : '—' },
              { key: 'updated_at', label: '更新', render: (r) => <span class="muted small" style={{ whiteSpace: 'nowrap' }}>{fmtDE(r.updated_at as string, true)}</span> },
            ]} />
        ) : <Empty>没有满足条件的游戏</Empty>}</Async>
      </Card>
    </div>
  );
}
