import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { isApiError } from '../api/client';
import { SEARCH_MAX_LENGTH, searchApi, searchKeys, type SearchResult } from '../api/search';
import { Icon } from '../components/ui/Icon';
import { cx } from '../components/ui/cx';
import { problemMessage } from '../lib/problem';
import { useCurrentWorkspace } from '../workspace';
import { SEARCH_TYPE_ICON, groupResults, normalizeQuery, searchResultHref } from './searchModel';

export const SEARCH_DEBOUNCE_MS = 250;
export const SEARCH_LIMIT = 20;

/** Запрос с задержкой (не отправлять на каждое нажатие; у /search свой rate limit 60/мин). */
export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** Поиск по workspace через `GET /search` (B6). Общий для поля в шапке и Command Palette. */
export function useWorkspaceSearch(raw: string, enabled: boolean, limit = SEARCH_LIMIT) {
  const { workspaceId, membership } = useCurrentWorkspace();
  const q = normalizeQuery(useDebounced(raw, SEARCH_DEBOUNCE_MS));
  const query = useQuery({
    queryKey: searchKeys.query(workspaceId ?? '', q ?? '', limit),
    queryFn: ({ signal }) => searchApi.search(workspaceId ?? '', { q: q ?? '', limit }, signal),
    enabled: Boolean(enabled && q && workspaceId && membership),
    staleTime: 30_000,
    retry: false,
    placeholderData: keepPreviousData,
  });
  const pendingDebounce = normalizeQuery(raw) !== q;
  return { q, query, pendingDebounce, workspaceId };
}

/**
 * Глобальный поиск (план §3.1, G-12/B6): поле в шапке — ARIA combobox со списком результатов,
 * сгруппированных по типу; ↑/↓ — выбор, Enter — открыть, Esc — закрыть/очистить. Только текст
 * результатов (snippet сервер редактирует от секретов); пропущенные из-за прав типы — подписаны.
 */
export function GlobalSearch() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [value, setValue] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const listId = useId();
  const hintId = useId();
  const { q, query, pendingDebounce, workspaceId } = useWorkspaceSearch(value, open);

  const groups = useMemo(() => groupResults(query.data?.items ?? []), [query.data]);
  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const showList = open && normalizeQuery(value) !== null;
  const activeIndex = flat.length === 0 ? -1 : Math.min(active, flat.length - 1);
  const optionId = (i: number) => `${listId}-opt-${i}`;

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', onDown);
    return () => document.removeEventListener('pointerdown', onDown);
  }, [open]);

  if (!workspaceId) return null;

  const go = (r: SearchResult) => {
    setOpen(false);
    setValue('');
    navigate(searchResultHref(workspaceId, r));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setOpen(true);
      if (flat.length > 0) setActive((i) => (i + 1) % flat.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (flat.length > 0) setActive((i) => (i - 1 + flat.length) % flat.length);
    } else if (e.key === 'Home' && showList && flat.length > 0 && e.ctrlKey) {
      e.preventDefault();
      setActive(0);
    } else if (e.key === 'End' && showList && flat.length > 0 && e.ctrlKey) {
      e.preventDefault();
      setActive(flat.length - 1);
    } else if (e.key === 'Enter') {
      const r = activeIndex >= 0 ? flat[activeIndex] : undefined;
      if (showList && r) {
        e.preventDefault();
        go(r);
      }
    } else if (e.key === 'Escape') {
      if (showList) {
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
      } else if (value) {
        e.preventDefault();
        setValue('');
      }
    }
  };

  const loading = showList && (pendingDebounce || query.isFetching) && !query.data;
  let status: string | null = null;
  if (open && value.trim().length > 0 && normalizeQuery(value) === null) status = t('shell.search.minLength');
  else if (showList && query.isError) {
    status =
      isApiError(query.error) && query.error.status === 429 ? t('shell.search.rateLimited') : `${t('shell.search.error')} ${problemMessage(t, query.error)}`;
  } else if (showList && query.data && !pendingDebounce && flat.length === 0) status = t('shell.search.noResults', { q });

  let index = -1;
  return (
    <div ref={rootRef} className="relative w-full min-w-[10rem] max-w-md" data-testid="global-search">
      <label htmlFor={`${listId}-input`} className="sr-only">
        {t('shell.search.label')}
      </label>
      <span aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-fg-muted">
        <Icon name="search" className="h-4 w-4" />
      </span>
      <input
        id={`${listId}-input`}
        type="search"
        role="combobox"
        autoComplete="off"
        spellCheck={false}
        maxLength={SEARCH_MAX_LENGTH}
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-describedby={hintId}
        aria-activedescendant={showList && activeIndex >= 0 ? optionId(activeIndex) : undefined}
        placeholder={t('shell.search.placeholder')}
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        className="input h-10 min-h-0 py-1.5 pl-9 pr-3 text-sm"
      />
      <span id={hintId} className="sr-only">
        {t('shell.search.hint')}
      </span>
      <div
        className={cx(
          'glass absolute left-0 right-0 top-full z-modal mt-1 max-h-[min(70vh,32rem)] overflow-y-auto rounded-lg shadow-e2',
          showList ? 'animate-fade-in' : 'hidden',
        )}
        data-testid="search-results"
        aria-busy={loading || undefined}
      >
        <div role="status" className={cx(status ? 'px-3 py-3 text-sm text-fg-muted' : 'sr-only')}>
          {status ?? (showList && query.data ? t('shell.search.count', { count: flat.length }) : '')}
        </div>
        {loading ? (
          <div aria-hidden="true" className="flex flex-col gap-2 p-3">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton h-8 w-full" />
            ))}
          </div>
        ) : null}
        <div id={listId} role="listbox" aria-label={t('shell.search.results')}>
          {showList
            ? groups.map((g) => (
                <div key={g.type} role="group" aria-labelledby={`${listId}-g-${g.type}`} className="py-1">
                  <div id={`${listId}-g-${g.type}`} role="presentation" className="px-3 pb-1 pt-2 text-xs font-semibold uppercase tracking-wide text-fg-subtle">
                    {t(`shell.search.types.${g.type}`)}
                  </div>
                  {g.items.map((r) => {
                    index += 1;
                    const i = index;
                    return (
                      // Фокус остаётся в поле (aria-activedescendant): клавиатура обрабатывается там (onKeyDown поля).
                      // eslint-disable-next-line jsx-a11y/click-events-have-key-events
                      <div
                        key={`${r.type}-${r.id}`}
                        id={optionId(i)}
                        role="option"
                        aria-selected={i === activeIndex}
                        tabIndex={-1}
                        data-search-type={r.type}
                        onPointerMove={() => setActive(i)}
                        onClick={() => go(r)}
                        className={cx(
                          'mx-1 flex cursor-pointer items-start gap-2 rounded-md px-2 py-2 text-sm',
                          i === activeIndex ? 'bg-accent-soft text-accent-soft-fg' : 'text-fg',
                        )}
                      >
                        <Icon name={SEARCH_TYPE_ICON[r.type]} className="mt-0.5 h-4 w-4 shrink-0" />
                        <span className="flex min-w-0 flex-col">
                          <span className="truncate font-medium">{r.title}</span>
                          {r.snippet ? <span className={cx('line-clamp-2 break-words text-xs', i === activeIndex ? '' : 'text-fg-muted')}>{r.snippet}</span> : null}
                        </span>
                      </div>
                    );
                  })}
                </div>
              ))
            : null}
        </div>
        {showList && query.data && (query.data.truncated || query.data.skipped_types.length > 0) ? (
          <div className="border-t border-border px-3 py-2 text-xs text-fg-muted">
            {query.data.truncated ? <p>{t('shell.search.truncated')}</p> : null}
            {query.data.skipped_types.length > 0 ? (
              <p>{t('shell.search.skipped', { types: query.data.skipped_types.map((ty) => t(`shell.search.types.${ty}`)).join(', ') })}</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
