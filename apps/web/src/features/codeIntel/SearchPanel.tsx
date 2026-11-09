/**
 * Поиск по workspace (B6 `GET /search`) среди типов, связанных с кодом: задачи, находки, источники
 * документации, проекты. По тексту кода `GET /search` не ищет — для этого «Символы» (B3). Фильтр
 * «только этот проект» — на клиенте по `project_id` результата (подписано).
 */
import { useQuery } from '@tanstack/react-query';
import { useId, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { SEARCH_MAX_LENGTH, searchApi, searchKeys, type SearchResultType } from '../../api/search';
import { TextField } from '../../components/form';
import { Button, ErrorState, Icon, Skeleton } from '../../components/ui';
import { SEARCH_TYPE_ICON, groupResults, normalizeQuery, searchResultHref } from '../../layout/searchModel';

export const CODE_SEARCH_TYPES: readonly SearchResultType[] = ['task', 'finding', 'documentation_source', 'project'];
export const CODE_SEARCH_LIMIT = 50;

export function SearchPanel({ workspaceId, projectId }: { workspaceId: string; projectId: string }) {
  const { t } = useTranslation();
  const uid = useId();
  const [draft, setDraft] = useState('');
  const [q, setQ] = useState<string | null>(null);
  const [error, setError] = useState<string | undefined>(undefined);
  const [onlyProject, setOnlyProject] = useState(true);
  const search = useQuery({
    queryKey: [...searchKeys.query(workspaceId, q ?? '', CODE_SEARCH_LIMIT), 'code', ...CODE_SEARCH_TYPES] as const,
    queryFn: ({ signal }) => searchApi.search(workspaceId, { q: q ?? '', types: [...CODE_SEARCH_TYPES], limit: CODE_SEARCH_LIMIT }, signal),
    enabled: q !== null,
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const n = normalizeQuery(draft);
    if (!n) {
      setError(t('codeIntel.search.tooShort'));
      return;
    }
    setError(undefined);
    setQ(n);
  };
  const all = search.data?.items ?? [];
  const items = onlyProject ? all.filter((r) => r.project_id === projectId || (r.type === 'project' && r.id === projectId) || r.project_id === null) : all;
  const groups = groupResults(items);
  return (
    <div className="flex flex-col gap-3" data-testid="code-search">
      <p className="text-sm text-fg-muted">{t('codeIntel.search.note')}</p>
      <form className="flex flex-wrap items-end gap-3" role="search" aria-label={t('codeIntel.search.formLabel')} onSubmit={submit} noValidate>
        <div className="min-w-0 flex-1 basis-56">
          <TextField
            id={`${uid}-q`}
            label={t('codeIntel.search.query')}
            autoComplete="off"
            maxLength={SEARCH_MAX_LENGTH}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            error={error}
          />
        </div>
        <label className="flex min-h-touch items-center gap-2 text-sm">
          <input type="checkbox" className="checkbox" checked={onlyProject} onChange={(e) => setOnlyProject(e.target.checked)} />
          {t('codeIntel.search.onlyProject')}
        </label>
        <Button type="submit" variant="primary" icon="search">
          {t('codeIntel.search.submit')}
        </Button>
      </form>
      {q === null ? null : search.isPending ? (
        <Skeleton lines={3} />
      ) : search.isError ? (
        <ErrorState error={search.error} onRetry={() => void search.refetch()} />
      ) : (
        <div className="flex flex-col gap-3">
          <p className="text-sm" role="status">
            {t('codeIntel.search.count', { count: items.length })}
            {search.data.truncated ? ` ${t('codeIntel.search.truncated')}` : ''}
          </p>
          {search.data.skipped_types.length > 0 ? (
            <p className="text-sm text-fg-muted">{t('shell.search.skipped', { types: search.data.skipped_types.map((ty) => t(`shell.search.types.${ty}`)).join(', ') })}</p>
          ) : null}
          {groups.map((g) => (
            <section key={g.type} aria-labelledby={`${uid}-${g.type}`}>
              <h3 id={`${uid}-${g.type}`} className="mb-1 flex items-center gap-2 text-sm font-semibold">
                <Icon name={SEARCH_TYPE_ICON[g.type]} className="h-4 w-4" />
                {t(`shell.search.types.${g.type}`)}
              </h3>
              <ul className="flex flex-col gap-1">
                {g.items.map((r) => (
                  <li key={`${r.type}-${r.id}`} className="flex flex-col rounded-md border border-border px-3 py-2">
                    <Link className="link break-words font-medium" to={searchResultHref(workspaceId, r)}>
                      {r.title}
                    </Link>
                    {r.snippet ? <span className="break-words text-sm text-fg-muted">{r.snippet}</span> : null}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
