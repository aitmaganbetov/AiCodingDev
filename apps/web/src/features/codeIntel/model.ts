/**
 * Чистая логика экрана Code Intelligence (F8): состояние в URL, раскладка графа зависимостей,
 * сводка Context Pack. Без React — покрыто unit-тестами (model.test.ts).
 */
import type { DependencyEdge, DependencyGraph, DependencyNode, IndexingJob } from '../../api/codeIntel';
import type { AttemptContextPack } from '../../api/tasks';

export const CODE_TABS = ['explorer', 'symbols', 'graph', 'context', 'indexing', 'knowledge', 'history', 'search'] as const;
export type CodeTab = (typeof CODE_TABS)[number];

export type GraphDirection = 'out' | 'in' | 'both';
export const GRAPH_DIRECTIONS: readonly GraphDirection[] = ['both', 'out', 'in'];
export const GRAPH_DEPTHS = [1, 2, 3] as const;

/** Параметры URL экрана (всё состояние — ссылкой можно поделиться). */
export interface CodeParams {
  project: string;
  repo: string;
  /** Проиндексированный commit (hex SHA); '' — последний READY-снимок. */
  commit: string;
  tab: CodeTab;
  /** Файл в Code Explorer и выделенные строки. */
  file: string;
  line: number | null;
  end: number | null;
  /** Поиск символов: запрос, вид, путь (файл/каталог). */
  q: string;
  kind: string;
  spath: string;
  /** Корень графа: путь или id символа (+ подпись символа для показа). */
  groot: string;
  gsym: string;
  gsymName: string;
  depth: 1 | 2 | 3;
  dir: GraphDirection;
  /** Вид графа: холст React Flow или доступный список. */
  view: 'graph' | 'list';
  /** Context Pack: run → задача → попытка. */
  run: string;
  task: string;
  attempt: string;
}

function positiveInt(v: string | null): number | null {
  if (!v || !/^\d{1,7}$/.test(v)) return null;
  const n = Number(v);
  return n >= 1 ? n : null;
}

const HEX = /^[0-9a-f]{7,64}$/;

export function parseCodeParams(p: URLSearchParams): CodeParams {
  const tab = p.get('tab');
  const depth = Number(p.get('depth'));
  const dir = p.get('dir');
  const commit = (p.get('commit') ?? '').toLowerCase();
  const line = positiveInt(p.get('line'));
  const end = positiveInt(p.get('end'));
  return {
    project: p.get('project') ?? '',
    repo: p.get('repo') ?? '',
    commit: HEX.test(commit) ? commit : '',
    tab: (CODE_TABS as readonly string[]).includes(tab ?? '') ? (tab as CodeTab) : 'explorer',
    file: p.get('file') ?? '',
    line,
    end: line !== null && end !== null && end >= line ? end : line,
    q: p.get('q') ?? '',
    kind: p.get('kind') ?? '',
    spath: p.get('spath') ?? '',
    groot: p.get('groot') ?? '',
    gsym: p.get('gsym') ?? '',
    gsymName: p.get('gsymname') ?? '',
    depth: depth === 2 || depth === 3 ? depth : 1,
    dir: dir === 'out' || dir === 'in' ? dir : 'both',
    view: p.get('view') === 'list' ? 'list' : 'graph',
    run: p.get('run') ?? '',
    task: p.get('task') ?? '',
    attempt: p.get('attempt') ?? '',
  };
}

const URL_KEYS: Record<keyof CodeParams, string> = {
  project: 'project',
  repo: 'repo',
  commit: 'commit',
  tab: 'tab',
  file: 'file',
  line: 'line',
  end: 'end',
  q: 'q',
  kind: 'kind',
  spath: 'spath',
  groot: 'groot',
  gsym: 'gsym',
  gsymName: 'gsymname',
  depth: 'depth',
  dir: 'dir',
  view: 'view',
  run: 'run',
  task: 'task',
  attempt: 'attempt',
};

/** Сериализация: значения по умолчанию в URL не пишутся. */
export function codeParamsToSearch(s: CodeParams): URLSearchParams {
  const out = new URLSearchParams();
  for (const [key, name] of Object.entries(URL_KEYS) as [keyof CodeParams, string][]) {
    const v = s[key];
    if (v === null || v === '') continue;
    if (key === 'tab' && v === 'explorer') continue;
    if (key === 'depth' && v === 1) continue;
    if (key === 'dir' && v === 'both') continue;
    if (key === 'view' && v === 'graph') continue;
    if (key === 'end' && v === s.line) continue;
    out.set(name, String(v));
  }
  return out;
}

/** Ссылка внутри экрана (переход к файлу/строкам, графу, Context Pack). */
export function codeHref(workspaceId: string, s: Partial<CodeParams>): string {
  const full: CodeParams = { ...parseCodeParams(new URLSearchParams()), ...s };
  const qs = codeParamsToSearch(full).toString();
  return `/w/${workspaceId}/code${qs ? `?${qs}` : ''}`;
}

/* ─────────────────────── Граф зависимостей ─────────────────────── */

export const DEP_NODE_WIDTH = 232;
export const DEP_NODE_HEIGHT = 64;
const DEP_GAP_X = 72;
const DEP_GAP_Y = 20;

export type GraphSide = 'root' | 'out' | 'in';

export interface LaidOutNode {
  node: DependencyNode;
  side: GraphSide;
  /** Колонка: 0 — корни, > 0 — зависимости корня (out), < 0 — зависящие от корня (in). */
  column: number;
  x: number;
  y: number;
  /** Число исходящих/входящих рёбер узла в ответе. */
  outgoing: number;
  incoming: number;
}

/**
 * Раскладка подграфа по колонкам: корни в центре, зависимости (достижимые по рёбрам source → target)
 * справа на расстоянии `depth`, зависящие от корня — слева. Внутри колонки — по пути/ссылке.
 */
export function layoutDependencyGraph(graph: Pick<DependencyGraph, 'nodes' | 'edges' | 'roots'>): LaidOutNode[] {
  const ids = new Set(graph.nodes.map((n) => n.id));
  const edges = graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  const forward = new Map<string, string[]>();
  for (const e of edges) forward.set(e.source, [...(forward.get(e.source) ?? []), e.target]);
  const roots = new Set(graph.roots.filter((r) => ids.has(r)));
  for (const n of graph.nodes) if (n.depth === 0) roots.add(n.id);
  // Узлы, достижимые из корней по направлению рёбер, — зависимости корня (сторона out).
  const reachable = new Set<string>();
  const queue = [...roots];
  while (queue.length > 0) {
    const id = queue.shift() as string;
    for (const next of forward.get(id) ?? []) {
      if (roots.has(next) || reachable.has(next)) continue;
      reachable.add(next);
      queue.push(next);
    }
  }
  const outgoing = new Map<string, number>();
  const incoming = new Map<string, number>();
  for (const e of edges) {
    outgoing.set(e.source, (outgoing.get(e.source) ?? 0) + 1);
    incoming.set(e.target, (incoming.get(e.target) ?? 0) + 1);
  }
  const columns = new Map<number, DependencyNode[]>();
  const sideOf = new Map<string, GraphSide>();
  for (const n of graph.nodes) {
    const side: GraphSide = roots.has(n.id) ? 'root' : reachable.has(n.id) ? 'out' : 'in';
    const depth = Math.max(1, n.depth);
    const column = side === 'root' ? 0 : side === 'out' ? depth : -depth;
    sideOf.set(n.id, side);
    columns.set(column, [...(columns.get(column) ?? []), n]);
  }
  const out: LaidOutNode[] = [];
  for (const [column, nodes] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    nodes
      .sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b)))
      .forEach((node, row) => {
        out.push({
          node,
          side: sideOf.get(node.id) ?? 'in',
          column,
          x: column * (DEP_NODE_WIDTH + DEP_GAP_X),
          y: row * (DEP_NODE_HEIGHT + DEP_GAP_Y),
          outgoing: outgoing.get(node.id) ?? 0,
          incoming: incoming.get(node.id) ?? 0,
        });
      });
  }
  return out;
}

/** Подпись узла: путь файла или ссылка внешнего импорта. */
export function nodeLabel(n: DependencyNode): string {
  if (n.kind === 'file') return n.path ?? n.id;
  return n.ref ?? n.id.replace(/^external:/, '');
}

/** Соседи узла для списка: что он импортирует/тестирует (исходящие) и кто зависит от него (входящие). */
export function neighbours(id: string, edges: readonly DependencyEdge[]): { out: DependencyEdge[]; in: DependencyEdge[] } {
  return { out: edges.filter((e) => e.source === id), in: edges.filter((e) => e.target === id) };
}

/* ─────────────────────── Context Pack ─────────────────────── */

export const CONTEXT_SECTIONS = ['code', 'symbols_graph', 'tests', 'repo_map', 'docs'] as const;

export interface PackSummary {
  /** Commit снимка ≠ commit задачи: часть фрагментов может не соответствовать коду задачи. */
  staleSnapshot: boolean;
  staleFragments: number;
  bySection: { section: string; fragments: AttemptContextPack['fragments']; bytes: number }[];
  omittedByReason: { reason: string; count: number }[];
  /** Фрагменты из ответа неполны (сервер отдал не все). */
  fragmentsTruncated: boolean;
  omittedTruncated: boolean;
}

export function summarizePack(p: AttemptContextPack): PackSummary {
  const sections = new Map<string, AttemptContextPack['fragments']>();
  for (const f of p.fragments) sections.set(f.section, [...(sections.get(f.section) ?? []), f]);
  const order = (s: string) => {
    const i = (CONTEXT_SECTIONS as readonly string[]).indexOf(s);
    return i < 0 ? CONTEXT_SECTIONS.length : i;
  };
  const reasons = new Map<string, number>();
  for (const o of p.omitted) reasons.set(o.reason, (reasons.get(o.reason) ?? 0) + 1);
  return {
    staleSnapshot: p.snapshot_commit !== null && p.snapshot_commit !== p.base_commit,
    staleFragments: p.fragments.filter((f) => f.stale).length,
    bySection: [...sections.entries()]
      .sort((a, b) => order(a[0]) - order(b[0]) || a[0].localeCompare(b[0]))
      .map(([section, fragments]) => ({ section, fragments, bytes: fragments.reduce((s, f) => s + (f.bytes ?? 0), 0) })),
    omittedByReason: [...reasons.entries()].map(([reason, count]) => ({ reason, count })).sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason)),
    fragmentsTruncated: p.fragments_total > p.fragments.length,
    omittedTruncated: p.omitted_total > p.omitted.length,
  };
}

/** Фрагмент — файл репозитория (не документация `doc:` и не карта `<repo_map>`): на него есть ссылка в Code Explorer. */
export function isRepositoryPath(path: string): boolean {
  return path !== '' && !path.startsWith('doc:') && !path.startsWith('<') && !path.startsWith('/') && !path.split('/').includes('..');
}

/** Строки фрагмента [from, to] (контракт: массив из двух чисел или null). */
export function fragmentLines(lines: readonly number[] | null): { from: number; to: number } | null {
  if (!lines || lines.length < 1) return null;
  const from = lines[0] ?? 0;
  const to = lines[1] ?? from;
  return from >= 1 && to >= from ? { from, to } : null;
}

/* ─────────────────────── Индексация ─────────────────────── */

export const JOB_ACTIVE: readonly IndexingJob['status'][] = ['QUEUED', 'RUNNING'];

/** Длительность в секундах → «1 ч 2 мин», «3 мин 4 с», «5 с» (null — неизвестно). */
export function formatSeconds(value: number | null, units: { h: string; m: string; s: string }): string | null {
  if (value === null || !Number.isFinite(value) || value < 0) return null;
  const total = Math.round(value);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} ${units.h} ${m} ${units.m}`;
  if (m > 0) return `${m} ${units.m} ${s} ${units.s}`;
  return `${s} ${units.s}`;
}
