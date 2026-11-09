import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useMatch } from 'react-router';
import { projectKeys, projectsApi } from '../api/projects';
import { ErrorState } from '../components/states';
import { Icon } from '../components/ui/Icon';
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/Popover';
import { can } from '../lib/permissions';
import { useCurrentWorkspace } from '../workspace';

/** Сколько проектов показывает переключатель (как сводка Dashboard; общий ключ кэша). */
export const SWITCHER_PROJECTS_LIMIT = 50;

/**
 * Переключатель проекта (план §3.1): текущий проект берётся из URL (`/w/:ws/projects/:projectId/…`),
 * список — первая страница `listProjects` (грузится при открытии). Popover со ссылками (навигация, не меню).
 */
export function ProjectSwitcher() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const match = useMatch('/w/:workspaceId/projects/:projectId/*');
  const projectId = match?.params.projectId && match.params.projectId !== 'new' ? match.params.projectId : null;
  const [open, setOpen] = useState(false);
  const allowed = Boolean(workspaceId && membership && can(membership.role, 'project:read'));

  const current = useQuery({
    queryKey: projectKeys.detail(projectId ?? ''),
    queryFn: ({ signal }) => projectsApi.get(projectId ?? '', signal),
    enabled: Boolean(allowed && projectId),
  });
  const list = useQuery({
    queryKey: projectKeys.summary(workspaceId ?? ''),
    queryFn: ({ signal }) => projectsApi.list(workspaceId ?? '', { limit: SWITCHER_PROJECTS_LIMIT }, signal),
    enabled: Boolean(allowed && open),
  });
  if (!allowed || !workspaceId) return null;

  const name = projectId ? (current.data?.name ?? (current.isPending ? t('state.loading') : t('shell.project.unknown'))) : t('shell.project.all');
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger className="btn-secondary max-w-full" data-testid="project-switcher">
        <Icon name="folder" className="h-4 w-4 shrink-0" />
        <span className="max-w-[10rem] truncate xl:max-w-[14rem]">
          <span className="sr-only">{t('shell.project.label')}: </span>
          {name}
        </span>
        <Icon name="chevronDown" className="h-4 w-4 shrink-0" />
      </PopoverTrigger>
      <PopoverContent label={t('shell.project.label')}>
        {list.isPending ? (
          <div aria-busy="true" className="flex flex-col gap-2 p-2">
            <span className="sr-only">{t('state.loading')}</span>
            {[0, 1, 2].map((i) => (
              <span key={i} aria-hidden="true" className="skeleton h-9 w-full" />
            ))}
          </div>
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} title={t('shell.project.error')} />
        ) : list.data.items.length === 0 ? (
          <p className="px-3 py-2 text-sm text-fg-muted">{t('shell.project.empty')}</p>
        ) : (
          <ul className="flex max-h-[50vh] flex-col overflow-y-auto">
            {list.data.items.map((p) => (
              <li key={p.id}>
                <Link
                  to={`/w/${workspaceId}/projects/${p.id}`}
                  onClick={() => setOpen(false)}
                  aria-current={p.id === projectId ? 'page' : undefined}
                  className="flex min-h-touch flex-col justify-center rounded px-3 py-1 text-fg no-underline hover:bg-surface-muted aria-[current=page]:bg-accent-soft aria-[current=page]:text-accent-soft-fg"
                >
                  <span className="truncate text-sm font-medium">{p.name}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-2 border-t border-border pt-2">
          <Link
            to={`/w/${workspaceId}/projects`}
            onClick={() => setOpen(false)}
            className="flex min-h-touch items-center rounded px-3 text-sm text-fg no-underline hover:bg-surface-muted"
          >
            {list.data?.next_cursor ? t('shell.project.allMore') : t('shell.project.allProjects')}
          </Link>
        </div>
      </PopoverContent>
    </Popover>
  );
}
