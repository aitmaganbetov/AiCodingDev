import { describe, expect, it } from 'vitest';
import i18n, { DEFAULT_NS, NAMESPACES } from '.';

const EXPECTED = [
  'common',
  'nav',
  'runs',
  'chat',
  'directives',
  'tasks',
  'quality',
  'compliance',
  'approvals',
  'providers',
  'projects',
  'knowledge',
  'settings',
  'audit',
  'dashboard',
  'analytics',
  'integrations',
];

function leafKeys(value: unknown, prefix = ''): string[] {
  if (typeof value !== 'object' || value === null) return [prefix];
  return Object.entries(value).flatMap(([k, v]) => leafKeys(v, prefix ? `${prefix}.${k}` : k));
}

describe('i18n namespaces', () => {
  it('loads one namespace per area file', () => {
    expect([...NAMESPACES].sort()).toEqual([...EXPECTED].sort());
    expect(NAMESPACES[0]).toBe(DEFAULT_NS);
    for (const ns of EXPECTED) expect(i18n.hasResourceBundle('ru', ns)).toBe(true);
  });

  it('top-level groups are unique across files (global key paths), area files use their own group', () => {
    const owner = new Map<string, string>();
    for (const ns of NAMESPACES) {
      for (const group of Object.keys(i18n.getResourceBundle('ru', ns) as object)) {
        expect(owner.get(group), `group "${group}" in ${ns} and ${owner.get(group)}`).toBeUndefined();
        owner.set(group, ns);
      }
    }
    for (const area of ['approvals', 'providers', 'quality', 'compliance', 'knowledge', 'settings', 'audit', 'dashboard', 'chat', 'tasks', 'directives', 'projects', 'runs']) {
      expect(owner.get(area)).toBe(area);
    }
  });

  it('existing keys resolve without a namespace prefix (fallbackNS), and with an explicit one', () => {
    expect(i18n.t('layout.skipToContent')).toBe('Перейти к основному содержимому');
    expect(i18n.t('run.stream.open')).toBe('Поток событий подключён.');
    expect(i18n.t('enums.run_status.RUNNING')).toBe('Выполняется');
    expect(i18n.t('list.shown', { count: 5 })).toBe('Показано 5 элементов');
    expect(i18n.t('approvals:approvals.title')).toBe('Одобрения');
    expect(i18n.t('approvals.title')).toBe('Одобрения');
  });

  it('has no empty strings', () => {
    for (const ns of NAMESPACES) {
      const bundle = i18n.getResourceBundle('ru', ns) as object;
      for (const key of leafKeys(bundle)) expect(i18n.t(`${ns}:${key}`), `${ns}:${key}`).not.toBe('');
    }
  });
});
