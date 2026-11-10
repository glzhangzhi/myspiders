import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fmtBytes, fmtDE, fmtDur, fmtLeft, fmtRel, localStamp } from '../format';
import { fuzzy } from '../../shell/Palette';
import { legacyHashRoute } from '../../shell/nav';

describe('format', () => {
  it('fmtDE 按德国时区显示，空值是破折号', () => {
    expect(fmtDE('2026-07-01T10:00:00Z')).toContain('12:00'); // 夏令时 UTC+2
    expect(fmtDE('2026-12-01T10:00:00Z')).toContain('11:00'); // 冬令时 UTC+1
    expect(fmtDE(null)).toBe('—');
    expect(fmtDE('garbage')).toBe('—');
  });
  it('fmtDE 接受 epoch 秒', () => {
    expect(fmtDE(1782900000)).not.toBe('—');
  });
  it('fmtRel', () => {
    const now = Date.parse('2026-11-03T12:00:00Z');
    expect(fmtRel('2026-11-03T11:59:50Z', now)).toBe('刚刚');
    expect(fmtRel('2026-11-03T11:55:00Z', now)).toBe('5 分钟前');
    expect(fmtRel('2026-11-03T15:00:00Z', now)).toBe('3 小时后');
  });
  it('fmtLeft 精确到时分 / 天时，向下取整', () => {
    const now = Date.parse('2026-11-03T12:00:00Z');
    expect(fmtLeft('2026-11-03T12:45:30Z', now)).toBe('45 分钟后');
    expect(fmtLeft('2026-11-03T14:15:00Z', now)).toBe('2 小时 15 分后');
    expect(fmtLeft('2026-11-10T07:59:00Z', now)).toBe('6 天 19 小时后');
    expect(fmtLeft('2026-11-03T11:00:00Z', now)).toBe('即将');
  });
  it('fmtBytes / fmtDur', () => {
    expect(fmtBytes(0)).toBe('0 B');
    expect(fmtBytes(1536)).toBe('1.5 KB');
    expect(fmtBytes(5 * 1024 ** 3)).toBe('5.0 GB');
    expect(fmtDur(3.2)).toBe('3.2 秒');
    expect(fmtDur(125)).toBe('2 分 5 秒');
  });
  it('localStamp 补零', () => {
    expect(localStamp(new Date(2026, 0, 5, 7, 3))).toBe('2026-01-05 07:03');
  });
});

describe('palette fuzzy', () => {
  it('子串与按序子序列都算命中', () => {
    expect(fuzzy('nin', 'Nintendo')).toBe(true);
    expect(fuzzy('ntd', 'Nintendo')).toBe(true);
    expect(fuzzy('菜', '菜谱')).toBe(true);
    expect(fuzzy('xyz', 'Nintendo')).toBe(false);
    expect(fuzzy('', 'anything')).toBe(true);
  });
});

describe('旧书签', () => {
  it('#mercor / #agent 映射到新路由', () => {
    expect(legacyHashRoute('#mercor')).toBe('/dashboard/mercor');
    expect(legacyHashRoute('#agent')).toBe('/dashboard/claude_usage');
    expect(legacyHashRoute('')).toBeNull();
  });
});

describe('api', () => {
  beforeEach(() => { localStorage.clear(); vi.resetModules(); });
  it('带 x-token，401 时弹出令牌框', async () => {
    const { setToken, tokenPrompt } = await import('../token');
    const { api, ApiError } = await import('../api');
    setToken('t1');
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'invalid admin token' }), { status: 401 }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(api('/api/x')).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock.mock.calls[0][1].headers['x-token']).toBe('t1');
    expect(tokenPrompt.value).toBe(true);
  });
  it('POST 对象自动 JSON 序列化', async () => {
    const { api } = await import('../api');
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    expect(await api('/api/y', { body: { a: 1 } })).toEqual({ ok: true });
    const init = fetchMock.mock.calls[0][1];
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"a":1}');
  });
});

describe('query 缓存', () => {
  it('同一个 key 的并发请求只发一次', async () => {
    const { fetchKey, _resetQueryCache } = await import('../query');
    _resetQueryCache();
    let n = 0;
    const fetcher = () => new Promise<number>((r) => setTimeout(() => r(++n), 5));
    const [a, b] = await Promise.all([fetchKey('/k', fetcher), fetchKey('/k', fetcher)]);
    expect(a).toBe(1); expect(b).toBe(1); expect(n).toBe(1);
  });
});
