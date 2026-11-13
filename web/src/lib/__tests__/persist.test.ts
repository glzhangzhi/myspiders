import { beforeEach, describe, expect, it } from 'vitest';
import { loadPersisted, oneOf, savePersisted } from '../persist';

describe('persist', () => {
  beforeEach(() => localStorage.clear());
  it('没存过回默认值，存过回存的值', () => {
    expect(loadPersisted('a', 'x')).toBe('x');
    savePersisted('a', 'y');
    expect(loadPersisted('a', 'x')).toBe('y');
    expect(localStorage.getItem('ui.a')).toBe('"y"');
  });
  it('类型不对或不在选项里回默认值（选项被删、旧版本存的值）', () => {
    savePersisted('n', 'abc');
    expect(loadPersisted('n', 30)).toBe(30);
    savePersisted('t', 'gone');
    expect(loadPersisted('t', 'new', oneOf('new', 'library'))).toBe('new');
    savePersisted('p', null);
    expect(loadPersisted<string | null>('p', null, (v) => v === null || typeof v === 'string')).toBe(null);
  });
  it('坏 JSON 不炸', () => {
    localStorage.setItem('ui.b', '{oops');
    expect(loadPersisted('b', 1)).toBe(1);
  });
});
