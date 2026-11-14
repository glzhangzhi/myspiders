import { describe, expect, it } from 'vitest';
import { parseChipMessage } from '../pageChips';

describe('parseChipMessage', () => {
  it('合法消息原样解析', () => {
    expect(parseChipMessage({ type: 'page-chips', page: 'av', chips: [{ id: 'a', label: 'SSIS-001', tone: 'blue', title: '搜索中' }] }))
      .toEqual(['av', [{ id: 'a', label: 'SSIS-001', tone: 'blue', title: '搜索中' }]]);
  });
  it('别的消息、缺字段一律忽略', () => {
    expect(parseChipMessage({ type: 'other' })).toBeNull();
    expect(parseChipMessage('x')).toBeNull();
    expect(parseChipMessage({ type: 'page-chips', page: 'av' })).toBeNull();
  });
  it('坏条目丢掉，未知颜色回退 grey，空数组表示清空', () => {
    const r = parseChipMessage({ type: 'page-chips', page: 'av', chips: [{ id: 1 }, { id: 'b', label: 'x', tone: '#f00' }] });
    expect(r).toEqual(['av', [{ id: 'b', label: 'x', tone: 'grey', title: undefined }]]);
    expect(parseChipMessage({ type: 'page-chips', page: 'av', chips: [] })).toEqual(['av', []]);
  });
});
