import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { projectKeys, projectsApi } from '../../api/projects';
import { ErrorState, LoadingState } from '../../components/states';

/** Максимум контракта (Limit ≤ 200): селектор проекта показывает первую страницу. */
export const PROJECT_SELECT_LIMIT = 200;

export function useProjectOptions(workspaceId: string) {
  return useQuery({
    queryKey: [...projectKeys.summary(workspaceId), PROJECT_SELECT_LIMIT] as const,
    queryFn: ({ signal }) => projectsApi.list(workspaceId, { limit: PROJECT_SELECT_LIMIT }, signal),
  });
}

/** Нативный select проекта (клавиатура и скринридеры — без дополнительного кода). */
export function ProjectSelect({
  id,
  workspaceId,
  value,
  onChange,
  label,
  error,
}: {
  id: string;
  workspaceId: string;
  value: string;
  onChange: (projectId: string) => void;
  label: string;
  error?: string | undefined;
}) {
  const { t } = useTranslation();
  const query = useProjectOptions(workspaceId);
  if (query.isPending) return <LoadingState inline label={t('knowledge.projectsLoading')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const errorId = error ? `${id}-error` : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <select
        id={id}
        className="input"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-invalid={Boolean(error)}
        aria-describedby={errorId}
      >
        <option value="">{t('knowledge.selectProject')}</option>
        {query.data.items.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      {error ? (
        <p id={errorId} className="text-sm font-medium text-danger-text">
          {error}
        </p>
      ) : null}
      {query.data.items.length === 0 ? <p className="text-sm text-fg-muted">{t('knowledge.noProjects')}</p> : null}
    </div>
  );
}
