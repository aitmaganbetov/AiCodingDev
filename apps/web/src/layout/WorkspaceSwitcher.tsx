import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { useSession } from '../auth/session';
import { Icon } from '../components/ui/Icon';
import { Popover, PopoverContent, PopoverTrigger } from '../components/ui/Popover';
import { workspaceHomePath } from '../routes';
import { useCurrentWorkspace } from '../workspace';

/**
 * Переключатель workspace: Popover со списком ссылок (это навигация, а не команды меню —
 * поэтому не role=menu). Escape/клик снаружи закрывают и возвращают фокус на кнопку.
 */
export function WorkspaceSwitcher({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  const { data: session } = useSession();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [open, setOpen] = useState(false);
  const workspaces = session?.workspaces ?? [];
  const home = workspaceHomePath();

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger className="btn-secondary max-w-full">
        <Icon name="layers" className="h-4 w-4 shrink-0" />
        <span className={compact ? 'max-w-[7rem] truncate' : 'max-w-[9rem] truncate sm:max-w-[14rem]'}>
          <span className="sr-only">{t('layout.workspaceSwitcher')}: </span>
          {membership?.name ?? t('layout.noWorkspaceSelected')}
        </span>
        <Icon name="chevronDown" className="h-4 w-4 shrink-0" />
      </PopoverTrigger>
      <PopoverContent label={t('layout.workspaceSwitcher')}>
        {workspaces.length > 0 ? (
          <ul className="flex flex-col">
            {workspaces.map((w) => (
              <li key={w.workspace_id}>
                <Link
                  to={`/w/${w.workspace_id}/${home}`}
                  onClick={() => setOpen(false)}
                  aria-current={w.workspace_id === workspaceId ? 'page' : undefined}
                  className="flex min-h-touch flex-col justify-center rounded px-3 py-1 text-fg no-underline hover:bg-surface-muted aria-[current=page]:bg-accent-soft aria-[current=page]:text-accent-soft-fg"
                >
                  <span className="text-sm font-medium">{w.name}</span>
                  <span className="text-xs text-fg-muted">
                    {w.slug} · {t(`enums.workspace_role.${w.role}`, { defaultValue: w.role })}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-3 py-2 text-sm text-fg-muted">{t('layout.noWorkspaces')}</p>
        )}
        <div className="mt-2 border-t border-border pt-2">
          <Link
            to="/workspaces"
            onClick={() => setOpen(false)}
            className="flex min-h-touch items-center rounded px-3 text-sm text-fg no-underline hover:bg-surface-muted"
          >
            {t('layout.allWorkspaces')}
          </Link>
        </div>
      </PopoverContent>
    </Popover>
  );
}
