/**
 * Dependency Graph (B3 `getRepositoryDependencies`): корень — путь (файл/каталог) или символ, глубина 1–3,
 * направление out/in/both. Граф — React Flow (ленивый чанк), рядом — доступный список узлов со связями
 * (на экранах < 768 px — только список). Неполный граф (пределы сервера) — явное предупреждение.
 */
import { useQuery } from '@tanstack/react-query';
import { Suspense, lazy, useCallback, useId, useMemo, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { isApiError } from '../../api/client';
import { codeIntelApi, codeIntelKeys, type DependenciesQuery, type DependencyEdge } from '../../api/codeIntel';
import { TextField } from '../../components/form';
import { Badge, Button, Callout, EmptyState, ErrorState, SkeletonBlock, Table, type Column } from '../../components/ui';
import { MQ_DESKTOP, useMediaQuery } from '../../lib/layoutPrefs';
import { GRAPH_DEPTHS, GRAPH_DIRECTIONS, layoutDependencyGraph, neighbours, nodeLabel, type GraphDirection, type LaidOutNode } from './model';
import { NoIndexState, SnapshotLine } from './shared';
import type { TabProps } from './SymbolSearch';

const DependencyGraphCanvas = lazy(() => import('./DependencyGraphCanvas'));

function EdgeList({ edges, label, labelOf, dir }: { edges: readonly DependencyEdge[]; label: string; labelOf: (id: string) => string; dir: 'out' | 'in' }) {
  const { t } = useTranslation();
  if (edges.length === 0) return null;
  return (
    <div>
      <p className="text-xs font-semibold text-fg-muted">{label}</p>
      <ul className="flex flex-col gap-0.5">
        {edges.map((e, i) => (
          <li key={`${e.source}-${e.target}-${i}`} className="break-all font-mono text-xs">
            {labelOf(dir === 'out' ? e.target : e.source)}{' '}
            <span className="font-sans text-fg-muted">({t(`codeIntel.edgeType.${e.edge_type}`, { defaultValue: e.edge_type })})</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function GraphList({
  items,
  edges,
  fileHref,
  rootHref,
}: {
  items: readonly LaidOutNode[];
  edges: readonly DependencyEdge[];
  fileHref: (path: string) => string;
  rootHref: (path: string) => string;
}) {
  const { t } = useTranslation();
  const byId = useMemo(() => new Map(items.map((i) => [i.node.id, i])), [items]);
  const labelOf = useCallback((id: string) => {
    const n = byId.get(id);
    return n ? nodeLabel(n.node) : id;
  }, [byId]);
  const columns: Column<LaidOutNode>[] = [
    {
      id: 'node',
      header: t('codeIntel.graph.col.node'),
      rowHeader: true,
      sortValue: (r) => nodeLabel(r.node),
      cell: (r) => (
        <span className="flex flex-col gap-1">
          <span className="break-all font-mono">{nodeLabel(r.node)}</span>
          <span className="flex flex-wrap gap-1">
            {r.side === 'root' ? <Badge tone="info">{t('codeIntel.graph.root')}</Badge> : null}
            {r.node.kind === 'external' ? (
              <Badge tone="neutral" icon="externalLink">
                {t('codeIntel.graph.external')}
              </Badge>
            ) : null}
            {r.node.is_test ? <Badge tone="neutral">{t('codeIntel.graph.test')}</Badge> : null}
            {r.node.language ? <span className="text-xs text-fg-muted">{r.node.language}</span> : null}
          </span>
        </span>
      ),
    },
    {
      id: 'side',
      header: t('codeIntel.graph.col.side'),
      sortValue: (r) => r.column,
      cell: (r) => (
        <span>
          {t(`codeIntel.graph.side.${r.side}`)}
          {r.side !== 'root' ? <span className="block text-xs text-fg-muted">{t('codeIntel.graph.distance', { count: r.node.depth })}</span> : null}
        </span>
      ),
    },
    {
      id: 'links',
      header: t('codeIntel.graph.col.links'),
      sortValue: (r) => r.outgoing + r.incoming,
      cell: (r) => {
        const nb = neighbours(r.node.id, edges);
        if (nb.out.length + nb.in.length === 0) return <span className="text-fg-muted">—</span>;
        return (
          <details>
            <summary className="min-h-target cursor-pointer text-sm">{t('codeIntel.graph.linksSummary', { out: nb.out.length, in: nb.in.length })}</summary>
            <div className="mt-1 flex flex-col gap-2">
              <EdgeList edges={nb.out} label={t('codeIntel.graph.dependsOn')} labelOf={labelOf} dir="out" />
              <EdgeList edges={nb.in} label={t('codeIntel.graph.usedBy')} labelOf={labelOf} dir="in" />
            </div>
          </details>
        );
      },
    },
    {
      id: 'actions',
      header: t('codeIntel.graph.col.actions'),
      cell: (r) =>
        r.node.kind === 'file' && r.node.path ? (
          <span className="flex flex-col gap-1">
            <Link className="link" to={fileHref(r.node.path)}>
              {t('codeIntel.graph.openFile')}
              <span className="sr-only">: {r.node.path}</span>
            </Link>
            {r.side !== 'root' ? (
              <Link className="link" to={rootHref(r.node.path)}>
                {t('codeIntel.graph.makeRoot')}
                <span className="sr-only">: {r.node.path}</span>
              </Link>
            ) : null}
          </span>
        ) : (
          <span className="text-fg-muted">{t('codeIntel.graph.noFile')}</span>
        ),
    },
  ];
  return (
    <div data-testid="dependency-graph-list">
      <Table caption={t('codeIntel.graph.listCaption')} columns={columns} rows={items} rowKey={(r) => r.node.id} density="compact" minWidthClass="min-w-[36rem]" />
    </div>
  );
}

export function DependencyGraph({ workspaceId, projectId, repositoryId, params, update, href }: TabProps) {
  const { t, i18n } = useTranslation();
  const uid = useId();
  const desktop = useMediaQuery(MQ_DESKTOP, true);
  const [draft, setDraft] = useState({ root: params.groot || (params.gsym ? '' : params.file), depth: params.depth, dir: params.dir });
  const [error, setError] = useState<string | undefined>(undefined);
  const query: DependenciesQuery | null = params.gsym
    ? { symbol: params.gsym, depth: params.depth, direction: params.dir, ...(params.commit ? { commit: params.commit } : {}) }
    : params.groot
      ? { path: params.groot, depth: params.depth, direction: params.dir, ...(params.commit ? { commit: params.commit } : {}) }
      : null;
  const graph = useQuery({
    queryKey: codeIntelKeys.dependencies(projectId, repositoryId, query ?? {}),
    queryFn: ({ signal }) => codeIntelApi.dependencies(projectId, repositoryId, query ?? {}, signal),
    enabled: query !== null,
    retry: (count, e) => !(isApiError(e) && (e.status === 404 || e.status === 422)) && count < 2,
  });
  const items = useMemo(() => (graph.data ? layoutDependencyGraph(graph.data) : []), [graph.data]);
  const fileHref = useCallback((path: string) => href({ tab: 'explorer', file: path, line: null, end: null }), [href]);
  const rootHref = useCallback((path: string) => href({ tab: 'graph', groot: path, gsym: '', gsymName: '' }), [href]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const root = draft.root.trim().replace(/^\/+|\/+$/g, '');
    if (!params.gsym && root === '') {
      setError(t('codeIntel.graph.rootRequired'));
      return;
    }
    if (root.split('/').some((p) => p === '..' || p === '.' || p === '.git')) {
      setError(t('codeIntel.graph.rootInvalid'));
      return;
    }
    setError(undefined);
    update(root ? { groot: root, gsym: '', gsymName: '', depth: draft.depth, dir: draft.dir } : { depth: draft.depth, dir: draft.dir });
  };

  const showGraph = desktop && params.view === 'graph';
  const indexingHref = href({ tab: 'indexing' });
  let body;
  if (!query) body = <p className="text-sm text-fg-muted">{t('codeIntel.graph.prompt')}</p>;
  else if (graph.isPending) body = <SkeletonBlock className="h-64" />;
  else if (graph.isError) {
    const status = isApiError(graph.error) ? graph.error.status : 0;
    body =
      status === 404 && params.commit ? (
        <NoIndexState workspaceId={workspaceId} projectId={projectId} indexingHref={indexingHref} commit={params.commit} latestHref={href({ commit: '' })} />
      ) : status === 404 ? (
        <EmptyState title={t('codeIntel.graph.rootMissingTitle')} description={t('codeIntel.graph.rootMissing')} icon="search" />
      ) : (
        <ErrorState error={graph.error} onRetry={() => void graph.refetch()} />
      );
  } else if (graph.data.snapshot === null) body = <NoIndexState workspaceId={workspaceId} projectId={projectId} indexingHref={indexingHref} />;
  else {
    const g = graph.data;
    const external = g.nodes.filter((n) => n.kind === 'external').length;
    body = (
      <div className="flex flex-col gap-3">
        {g.snapshot ? <SnapshotLine snapshot={g.snapshot} /> : null}
        <p className="text-sm text-fg" role="status" data-testid="graph-summary">
          {t('codeIntel.graph.summary', {
            nodes: new Intl.NumberFormat(i18n.language).format(g.nodes.length),
            edges: new Intl.NumberFormat(i18n.language).format(g.edges.length),
            external,
            depth: g.depth,
            direction: t(`codeIntel.graph.dir.${g.direction}`),
          })}
        </p>
        {g.truncated ? (
          <Callout tone="warning" title={t('codeIntel.graph.truncatedTitle')}>
            {t('codeIntel.graph.truncated', { nodes: g.limits.max_nodes, edges: g.limits.max_edges })}
          </Callout>
        ) : null}
        {g.nodes.length === 0 ? (
          <p className="text-sm text-fg-muted">{t('codeIntel.graph.empty')}</p>
        ) : (
          <>
            {desktop ? (
              <div className="flex flex-wrap items-center gap-2" role="group" aria-label={t('codeIntel.graph.viewLabel')}>
                <Button size="sm" variant={params.view === 'graph' ? 'secondary' : 'ghost'} icon="workflow" aria-pressed={params.view === 'graph'} onClick={() => update({ view: 'graph' })}>
                  {t('codeIntel.graph.viewGraph')}
                </Button>
                <Button size="sm" variant={params.view === 'list' ? 'secondary' : 'ghost'} icon="viewList" aria-pressed={params.view === 'list'} onClick={() => update({ view: 'list' })}>
                  {t('codeIntel.graph.viewList')}
                </Button>
              </div>
            ) : (
              <p className="text-sm text-fg-muted" data-testid="graph-narrow-note">
                {t('codeIntel.graph.narrow')}
              </p>
            )}
            {showGraph ? (
              <>
                <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-fg-muted" aria-label={t('codeIntel.graph.legend')}>
                  <li className="flex items-center gap-1">
                    <span className="inline-block h-3 w-5 rounded-sm border-2 border-accent" aria-hidden="true" />
                    {t('codeIntel.graph.root')}
                  </li>
                  <li className="flex items-center gap-1">
                    <span className="inline-block h-3 w-5 rounded-sm border-2 border-border-strong" aria-hidden="true" />
                    {t('codeIntel.graph.legendFile')}
                  </li>
                  <li className="flex items-center gap-1">
                    <span className="inline-block h-3 w-5 rounded-sm border-2 border-dashed border-border-strong" aria-hidden="true" />
                    {t('codeIntel.graph.legendExternal')}
                  </li>
                  <li>{t('codeIntel.graph.legendSides')}</li>
                </ul>
                <Suspense fallback={<SkeletonBlock className="h-[60vh] min-h-[22rem]" />}>
                  <DependencyGraphCanvas items={items} edges={g.edges} fileHref={fileHref} label={t('codeIntel.graph.canvasLabel')} />
                </Suspense>
              </>
            ) : (
              <GraphList items={items} edges={g.edges} fileHref={fileHref} rootHref={rootHref} />
            )}
          </>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="dependency-graph">
      <form className="flex flex-wrap items-end gap-3" aria-label={t('codeIntel.graph.formLabel')} onSubmit={submit} noValidate>
        {params.gsym ? (
          <div className="flex min-w-0 flex-1 basis-56 flex-col gap-1">
            <span className="text-sm font-medium text-fg">{t('codeIntel.graph.rootSymbol')}</span>
            <span className="flex flex-wrap items-center gap-2">
              <code className="break-all font-mono text-sm" data-testid="graph-root-symbol">
                {params.gsymName || params.gsym}
              </code>
              <Button size="sm" variant="ghost" onClick={() => update({ gsym: '', gsymName: '' })}>
                {t('codeIntel.graph.clearSymbol')}
              </Button>
            </span>
          </div>
        ) : (
          <div className="min-w-0 flex-1 basis-56">
            <TextField
              id={`${uid}-root`}
              label={t('codeIntel.graph.rootPath')}
              hint={t('codeIntel.graph.rootPathHint')}
              autoComplete="off"
              spellCheck={false}
              maxLength={4096}
              value={draft.root}
              onChange={(e) => setDraft({ ...draft, root: e.target.value })}
              error={error}
            />
          </div>
        )}
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-depth`} className="text-sm font-medium text-fg">
            {t('codeIntel.graph.depth')}
          </label>
          <select id={`${uid}-depth`} className="select" value={draft.depth} onChange={(e) => setDraft({ ...draft, depth: Number(e.target.value) as 1 | 2 | 3 })}>
            {GRAPH_DEPTHS.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-dir`} className="text-sm font-medium text-fg">
            {t('codeIntel.graph.direction')}
          </label>
          <select id={`${uid}-dir`} className="select" value={draft.dir} onChange={(e) => setDraft({ ...draft, dir: e.target.value as GraphDirection })}>
            {GRAPH_DIRECTIONS.map((d) => (
              <option key={d} value={d}>
                {t(`codeIntel.graph.dir.${d}`)}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" variant="primary" icon="workflow">
          {t('codeIntel.graph.submit')}
        </Button>
      </form>
      {body}
    </div>
  );
}
