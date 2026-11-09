/**
 * Symbol Search (B3 `listRepositorySymbols`): ранжированные результаты снимка индекса, фильтры вида и
 * пути, cursor-пагинация, переход к файлу и строкам в Code Explorer, граф зависимостей от символа.
 * Сигнатура — текст из кода (секреты редактирует сервер): только как текст.
 */
import { useInfiniteQuery } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import { CODE_SYMBOL_KINDS, codeIntelApi, codeIntelKeys, type CodeSymbol } from '../../api/codeIntel';
import { TextField } from '../../components/form';
import { Badge, Button, ButtonLink, ErrorState, LoadMore, Skeleton } from '../../components/ui';
import type { CodeParams } from './model';
import { NoIndexState, SnapshotLine } from './shared';

export const SYMBOLS_PAGE_SIZE = 25;
// Контракт: 1–200 символов, без управляющих символов (иначе 422).
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

export interface TabProps {
  workspaceId: string;
  projectId: string;
  repositoryId: string;
  params: CodeParams;
  update: (patch: Partial<CodeParams>) => void;
  /** Ссылка на экран с текущими project/repo/commit и изменениями. */
  href: (patch: Partial<CodeParams>) => string;
}

function SymbolRow({ symbol, href }: { symbol: CodeSymbol; href: TabProps['href'] }) {
  const { t, i18n } = useTranslation();
  const lines = symbol.end_line > symbol.start_line ? `${symbol.start_line}–${symbol.end_line}` : String(symbol.start_line);
  return (
    <li className="flex flex-col gap-2 border-b border-border py-3 last:border-b-0" data-symbol-id={symbol.id}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="break-all font-mono font-semibold text-fg">{symbol.name}</span>
        <Badge tone="info">{t(`codeIntel.symbolKind.${symbol.kind}`, { defaultValue: symbol.kind })}</Badge>
        <Badge tone="neutral">{t(`codeIntel.symbols.match.${symbol.match}`)}</Badge>
        <span className="text-xs text-fg-muted">
          {t('codeIntel.symbols.score', { score: new Intl.NumberFormat(i18n.language, { maximumFractionDigits: 2, minimumFractionDigits: 2 }).format(symbol.score) })}
        </span>
      </div>
      {symbol.qualified_name && symbol.qualified_name !== symbol.name ? <p className="break-all font-mono text-xs text-fg-muted">{symbol.qualified_name}</p> : null}
      <p className="break-all font-mono text-sm text-fg">
        {symbol.path}
        <span className="text-fg-muted">:{lines}</span>
      </p>
      {symbol.signature ? (
        <pre className="max-w-full overflow-hidden whitespace-pre-wrap break-all rounded-md bg-surface-muted px-2 py-1 font-mono text-code-sm text-fg">
          <code>{symbol.signature}</code>
        </pre>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <ButtonLink
          size="sm"
          variant="secondary"
          icon="code"
          to={href({ tab: 'explorer', file: symbol.path, line: symbol.start_line, end: Math.max(symbol.start_line, symbol.end_line) })}
        >
          {t('codeIntel.symbols.open')}
          <span className="sr-only">: {t('codeIntel.symbols.openLabel', { name: symbol.name, path: symbol.path, line: symbol.start_line })}</span>
        </ButtonLink>
        <ButtonLink size="sm" variant="ghost" icon="workflow" to={href({ tab: 'graph', gsym: symbol.id, gsymName: symbol.name, groot: '' })}>
          {t('codeIntel.symbols.graph')}
          <span className="sr-only">: {symbol.name}</span>
        </ButtonLink>
      </div>
    </li>
  );
}

export function SymbolSearch({ workspaceId, projectId, repositoryId, params, update, href }: TabProps) {
  const { t } = useTranslation();
  const uid = useId();
  const [draft, setDraft] = useState({ q: params.q, kind: params.kind, spath: params.spath });
  const [error, setError] = useState<string | undefined>(undefined);
  const kind = (CODE_SYMBOL_KINDS as readonly string[]).includes(params.kind) ? (params.kind as CodeSymbol['kind']) : undefined;
  const query = {
    q: params.q,
    ...(kind ? { kind } : {}),
    ...(params.spath ? { path: params.spath } : {}),
    ...(params.commit ? { commit: params.commit } : {}),
    limit: SYMBOLS_PAGE_SIZE,
  };
  const symbols = useInfiniteQuery({
    queryKey: codeIntelKeys.symbols(projectId, repositoryId, query),
    queryFn: ({ pageParam, signal }) => codeIntelApi.symbols(projectId, repositoryId, { ...query, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: params.q !== '',
    retry: (count, e) => !(isApiError(e) && (e.status === 404 || e.status === 422)) && count < 2,
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const q = draft.q.trim();
    if (q === '' || CONTROL.test(q)) {
      setError(t('codeIntel.symbols.queryInvalid'));
      return;
    }
    setError(undefined);
    update({ q, kind: draft.kind, spath: draft.spath.trim().replace(/^\/+|\/+$/g, '') });
  };

  const items = symbols.data?.pages.flatMap((p) => p.items) ?? [];
  const snapshot = symbols.data?.pages[0]?.snapshot;
  const indexingHref = href({ tab: 'indexing' });
  let body;
  if (params.q === '') body = <p className="text-sm text-fg-muted">{t('codeIntel.symbols.prompt')}</p>;
  else if (symbols.isPending) body = <Skeleton lines={4} />;
  else if (symbols.isError && items.length === 0) {
    body =
      isApiError(symbols.error) && symbols.error.status === 404 && params.commit ? (
        <NoIndexState workspaceId={workspaceId} projectId={projectId} indexingHref={indexingHref} commit={params.commit} latestHref={href({ commit: '' })} />
      ) : (
        <ErrorState error={symbols.error} onRetry={() => void symbols.refetch()} />
      );
  } else if (snapshot === null) body = <NoIndexState workspaceId={workspaceId} projectId={projectId} indexingHref={indexingHref} />;
  else
    body = (
      <div className="flex flex-col gap-2">
        {snapshot ? <SnapshotLine snapshot={snapshot} /> : null}
        <p className="text-sm text-fg" role="status" data-testid="symbol-count">
          {symbols.hasNextPage ? t('codeIntel.symbols.shownMore', { count: items.length }) : t('codeIntel.symbols.shown', { count: items.length })}
        </p>
        {items.length === 0 ? (
          <p className="text-sm text-fg-muted">{t('codeIntel.symbols.empty')}</p>
        ) : (
          <ol className="flex flex-col" aria-label={t('codeIntel.symbols.results', { q: params.q })} data-testid="symbol-results">
            {items.map((s) => (
              <SymbolRow key={s.id} symbol={s} href={href} />
            ))}
          </ol>
        )}
        {symbols.hasNextPage || symbols.isFetchNextPageError ? (
          <LoadMore
            shown={items.length}
            hasNextPage={symbols.hasNextPage}
            isFetchingNextPage={symbols.isFetchingNextPage}
            nextPageError={symbols.isFetchNextPageError ? symbols.error : null}
            onLoadMore={() => void symbols.fetchNextPage()}
          />
        ) : null}
      </div>
    );

  return (
    <div className="flex flex-col gap-4" data-testid="symbol-search">
      <form className="flex flex-wrap items-end gap-3" role="search" aria-label={t('codeIntel.symbols.formLabel')} onSubmit={submit} noValidate>
        <div className="min-w-0 flex-1 basis-56">
          <TextField
            id={`${uid}-q`}
            label={t('codeIntel.symbols.query')}
            hint={t('codeIntel.symbols.queryHint')}
            autoComplete="off"
            spellCheck={false}
            maxLength={200}
            value={draft.q}
            onChange={(e) => setDraft({ ...draft, q: e.target.value })}
            error={error}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-kind`} className="text-sm font-medium text-fg">
            {t('codeIntel.symbols.kind')}
          </label>
          <select id={`${uid}-kind`} className="select" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
            <option value="">{t('codeIntel.symbols.kindAll')}</option>
            {CODE_SYMBOL_KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`codeIntel.symbolKind.${k}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="min-w-0 basis-56">
          <TextField
            id={`${uid}-path`}
            label={t('codeIntel.symbols.path')}
            hint={t('codeIntel.symbols.pathHint')}
            autoComplete="off"
            spellCheck={false}
            maxLength={4096}
            value={draft.spath}
            onChange={(e) => setDraft({ ...draft, spath: e.target.value })}
          />
        </div>
        <Button type="submit" variant="primary" icon="search">
          {t('codeIntel.symbols.submit')}
        </Button>
      </form>
      {body}
    </div>
  );
}
