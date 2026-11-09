import { useTranslation } from 'react-i18next';
import { NavLink } from 'react-router';
import { Icon } from '../components/ui/Icon';
import { Tooltip } from '../components/ui/Tooltip';
import { cx } from '../components/ui/cx';
import { can } from '../lib/permissions';
import { NAV_GROUPS, navEntries, type NavEntry, type NavGroup } from '../routes';
import { useCurrentWorkspace } from '../workspace';
import { usePendingApprovals } from './shellData';

const GROUP_LABEL: Record<NavGroup, string> = {
  overview: 'nav.groupOverview',
  execution: 'nav.groupExecution',
  code: 'nav.groupCode',
  platform: 'nav.groupPlatform',
};

/** Разделы, доступные роли текущего workspace (скрытие — только UX; сервер проверяет права). */
export function useNavEntries(): NavEntry[] {
  const { membership } = useCurrentWorkspace();
  return navEntries().filter((e) => !e.permission || can(membership?.role, e.permission));
}

function linkClass(rail: boolean) {
  return ({ isActive }: { isActive: boolean }) =>
    cx(
      'group relative flex min-h-touch items-center rounded-md text-sm font-medium no-underline transition-colors duration-instant',
      rail ? 'justify-center px-0' : 'gap-3 px-3',
      isActive ? 'bg-accent-soft text-accent-soft-fg' : 'text-fg hover:bg-surface-muted',
    );
}

function CountBadge({ count, more, rail }: { count: number; more: boolean; rail: boolean }) {
  const { t } = useTranslation();
  if (count <= 0) return null;
  const text = more ? `${count}+` : String(count);
  return (
    <>
      <span
        aria-hidden="true"
        className={cx(
          'rounded-full bg-warning-soft font-semibold tabular-nums text-warning-fg ring-1 ring-warning-border',
          rail ? 'absolute right-1 top-1 min-w-[1.125rem] px-1 text-center text-[0.6875rem] leading-[1.125rem]' : 'ml-auto px-2 text-xs leading-5',
        )}
        data-testid="nav-badge"
      >
        {text}
      </span>
      <span className="sr-only">{t('shell.nav.pendingBadge', { count: text })}</span>
    </>
  );
}

/**
 * Основная навигация (план §6.1, D-3): 14 разделов в 4 группах. `rail` — свёрнутый режим (только
 * иконки, подпись — доступное имя ссылки и подсказка при наведении/фокусе). Активный пункт —
 * aria-current=page + цвет + полоса слева (не только цвет).
 */
export function SidebarNav({ rail = false, onNavigate }: { rail?: boolean; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const entries = useNavEntries();
  const approvals = usePendingApprovals(workspaceId, membership?.role);

  return (
    <nav aria-label={t('layout.mainNav')} className={cx('flex flex-1 flex-col gap-3 py-3', rail ? 'px-2' : 'px-3')} data-rail={rail ? '' : undefined}>
      {workspaceId && membership ? (
        NAV_GROUPS.map((group) => {
          const items = entries.filter((e) => e.group === group);
          if (items.length === 0) return null;
          const headingId = `nav-group-${group}${rail ? '-rail' : ''}`;
          return (
            <div key={group} className={cx(rail && 'border-t border-border pt-3 first:border-t-0 first:pt-0')}>
              <h2 id={headingId} className={cx(rail ? 'sr-only' : 'mb-1 px-3 text-xs font-semibold uppercase tracking-wide text-fg-subtle')}>
                {t(GROUP_LABEL[group])}
              </h2>
              <ul aria-labelledby={headingId} className="flex flex-col gap-0.5">
                {items.map((item) => {
                  const label = t(item.labelKey);
                  const badge =
                    item.badge === 'approvals' && approvals.count !== null ? (
                      <CountBadge count={approvals.count} more={approvals.more} rail={rail} />
                    ) : null;
                  const link = (
                    <NavLink to={`/w/${workspaceId}/${item.path}`} className={linkClass(rail)} onClick={onNavigate} data-nav-id={item.id}>
                      {({ isActive }) => (
                        <>
                          <span
                            aria-hidden="true"
                            className={cx('absolute left-0 top-1/2 h-6 w-1 -translate-y-1/2 rounded-r', isActive ? 'bg-accent' : 'bg-transparent')}
                          />
                          <Icon name={item.icon} className="h-5 w-5 shrink-0" />
                          <span className={rail ? 'sr-only' : 'min-w-0 flex-1 truncate'}>{label}</span>
                          {badge}
                        </>
                      )}
                    </NavLink>
                  );
                  return (
                    <li key={item.id}>
                      {rail ? (
                        <Tooltip content={label} side="right">
                          {link}
                        </Tooltip>
                      ) : (
                        link
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })
      ) : (
        <p className={cx('px-3 text-sm text-fg-muted', rail && 'sr-only')}>{t('layout.selectWorkspaceHint')}</p>
      )}
      <ul className="mt-auto flex flex-col gap-0.5 border-t border-border pt-3">
        <li>
          <WorkspacesLink rail={rail} onNavigate={onNavigate} />
        </li>
      </ul>
    </nav>
  );
}

/** Ссылка на все рабочие пространства (низ навигации). */
function WorkspacesLink({ rail = false, onNavigate }: { rail?: boolean; onNavigate?: () => void }) {
  const { t } = useTranslation();
  const label = t('nav.workspaces');
  const link = (
    <NavLink to="/workspaces" end className={linkClass(rail)} onClick={onNavigate}>
      <Icon name="layers" className="h-5 w-5 shrink-0" />
      <span className={rail ? 'sr-only' : undefined}>{label}</span>
    </NavLink>
  );
  return rail ? (
    <Tooltip content={label} side="right">
      {link}
    </Tooltip>
  ) : (
    link
  );
}
