/**
 * Ленивые namespace i18n (F4, бюджет начального JS): статическая проверка, что
 *  - код начального бандла (всё, что достижимо из main.tsx, кроме экранов `lazyPage`) использует
 *    только ключи eager-namespace (common/nav/ds) и фоново догружаемых строк оболочки;
 *  - каждый экран `lazyPage(() => import('…'), 'Name', [ns…])` объявил все namespace, чьи ключи
 *    использует он сам и всё, что он импортирует.
 * Ключи ищутся в вызовах `t('group.…')`/`t(\`group.…\`)`, `labelKey/titleKey/descriptionKey: 'group.…'`
 * и `t('ns:…')`; группа верхнего уровня → файл namespace (группы уникальны, см. i18n.test.ts).
 */
import { describe, expect, it } from 'vitest';
import { EAGER_NAMESPACES, NAMESPACES, SHELL_DEFERRED_NAMESPACES } from '../i18n';
import i18n from '../i18n';

/** Исходники приложения как текст (ключ — путь от src/, например `features/chat/ChatPage.tsx`). */
const raw = import.meta.glob<string>(['../**/*.{ts,tsx}', '!../**/*.test.{ts,tsx}', '!../**/*.d.ts', '!../test/**'], {
  query: '?raw',
  import: 'default',
  eager: true,
});
const SOURCE = new Map(Object.entries(raw).map(([k, v]) => [k.startsWith('./') ? `routes/${k.slice(2)}` : k.replace(/^\.\.\//, ''), v]));
const FILES = [...SOURCE.keys()];

function normalize(path: string): string {
  const out: string[] = [];
  for (const part of path.split('/')) {
    if (part === '' || part === '.') continue;
    if (part === '..') out.pop();
    else out.push(part);
  }
  return out.join('/');
}
const dirname = (f: string) => f.split('/').slice(0, -1).join('/');

function resolveImport(from: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null;
  const base = normalize(`${dirname(from)}/${spec}`);
  for (const c of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]) if (SOURCE.has(c)) return c;
  return null;
}

/** Импорты файла: статические (`import … from`, `export … from`) и динамические (`import('…')`). */
function imports(file: string): { static: string[]; dynamic: string[] } {
  const src = SOURCE.get(file) ?? '';
  const stat = [...src.matchAll(/(?:^|\n)\s*(?:import|export)\s[^'"`;]*?from\s*['"]([^'"]+)['"]/g), ...src.matchAll(/(?:^|\n)\s*import\s*['"]([^'"]+)['"]/g)]
    .map((m) => resolveImport(file, m[1] ?? ''))
    .filter((x): x is string => x !== null);
  const dyn = [...src.matchAll(/import\(\s*['"]([^'"]+)['"]\s*\)/g)].map((m) => resolveImport(file, m[1] ?? '')).filter((x): x is string => x !== null);
  return { static: stat, dynamic: dyn };
}

/** Экраны lazyPage (route-файлы, ленивые слои оболочки): модуль → объявленные namespace. */
function lazyPages(): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const file of FILES.filter((f) => f !== 'routes/lazyPage.tsx')) {
    const src = SOURCE.get(file) ?? '';
    for (const m of src.matchAll(/lazyPage\(\s*\(\)\s*=>\s*import\(\s*['"]([^'"]+)['"]\s*\)\s*,\s*['"]\w+['"]\s*,\s*\[([^\]]*)\]\s*,?\s*\)/g)) {
      const target = resolveImport(file, m[1] ?? '');
      if (!target) throw new Error(`lazyPage target not found: ${m[1] ?? ''} in ${file}`);
      const ns = [...(m[2] ?? '').matchAll(/['"]([\w-]+)['"]/g)].map((x) => x[1] ?? '');
      out.set(target, [...(out.get(target) ?? []), ...ns]);
    }
  }
  return out;
}

const GROUP_NS = new Map<string, string>();
for (const ns of NAMESPACES) for (const group of Object.keys(i18n.getResourceBundle('ru', ns) as object)) GROUP_NS.set(group, ns);

function namespacesUsed(file: string): Set<string> {
  // i18n/index.ts только описывает соглашение (примеры ключей в комментариях).
  if (file === 'i18n/index.ts') return new Set();
  const src = SOURCE.get(file) ?? '';
  const found = new Set<string>();
  const add = (group: string | undefined) => {
    const ns = group ? GROUP_NS.get(group) : undefined;
    if (ns) found.add(ns);
  };
  for (const m of src.matchAll(/\bt\(\s*['"`]([A-Za-z]+)[.:]/g)) {
    const g = m[1] ?? '';
    if ((NAMESPACES as readonly string[]).includes(g) && src.includes(`${g}:`)) found.add(g);
    add(g);
  }
  for (const m of src.matchAll(/\b(?:labelKey|titleKey|descriptionKey|i18nKey)\s*[:=]\s*['"`]([A-Za-z]+)\./g)) add(m[1]);
  return found;
}

function closure(entries: readonly string[], stopAt: ReadonlySet<string>): Set<string> {
  const seen = new Set<string>();
  const stack = [...entries];
  while (stack.length > 0) {
    const f = stack.pop() as string;
    if (seen.has(f)) continue;
    seen.add(f);
    const { static: s, dynamic: d } = imports(f);
    for (const next of [...s, ...d]) if (!stopAt.has(next) && !seen.has(next)) stack.push(next);
  }
  return seen;
}

const short = (f: string) => f;

describe('i18n namespaces per lazy screen', () => {
  const pages = lazyPages();
  const boundaries = new Set(pages.keys());

  it('route files declare lazy screens', () => {
    expect(pages.size).toBeGreaterThan(20);
    for (const [page, ns] of pages) for (const n of ns) expect(NAMESPACES, `${short(page)} declares unknown "${n}"`).toContain(n);
  });

  it('initial bundle code uses only eager (and background-preloaded shell) namespaces', () => {
    const allowed = new Set<string>([...EAGER_NAMESPACES, ...SHELL_DEFERRED_NAMESPACES]);
    const problems: string[] = [];
    for (const f of closure(['main.tsx'], boundaries)) {
      for (const ns of namespacesUsed(f)) if (!allowed.has(ns)) problems.push(`${short(f)} → ${ns}`);
    }
    expect(problems).toEqual([]);
  });

  it('every lazy screen declares the namespaces used by its module graph', () => {
    const problems: string[] = [];
    for (const [page, declared] of pages) {
      const allowed = new Set<string>([...EAGER_NAMESPACES, ...declared]);
      for (const f of closure([page], new Set([...boundaries].filter((b) => b !== page)))) {
        for (const ns of namespacesUsed(f)) if (!allowed.has(ns)) problems.push(`${short(page)}: ${short(f)} → ${ns}`);
      }
    }
    expect(problems).toEqual([]);
  });
});
