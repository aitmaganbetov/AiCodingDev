import { useTranslation } from 'react-i18next';
import { NavLink } from 'react-router';
import { Icon } from '../components/ui/Icon';
import { cx } from '../components/ui/cx';
import { can } from '../lib/permissions';
import { NAV_GROUPS, navEntries } from '../routes';
import { useCurrentWorkspace } from '../workspace';

const GROUP_LABEL = { work: 'nav.groupWork', quality: 'nav.groupQuality', admin: 'nav.groupAdmin' } as const;

const linkClass = ({ isActive }: { isActive: boolean }) =>
  cx(
    'flex min-h-touch items-center gap-3 rounded-md px-3 text-sm font-medium no-underline',
    isActive ? 'bg-accent-soft text-accent-soft-fg' : 'text-fg hover:bg-surface-muted',
  );

/**
 * Основная навигация: разделы Mission Control из реестра областей (src/routes), по группам.
 * Активный пункт — aria-current=page (NavLink) + цвет и полоса, не только цвет.
 */
export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const entries = navEntries().filter((e) => !e.permission || can(membership?.role, e.permission));

  return (
    <nav aria-label={t('layout.mainNav')} className="flex flex-1 flex-col gap-4 px-3 py-4">
      {workspaceId && membership ? (
        NAV_GROUPS.map((group) => {
          const items = entries.filter((e) => e.group === group);
          if (items.length === 0) return null;
          const headingId = `nav-group-${group}`;
          return (
            <div key={group}>
              <h2 id={headingId} className="mb-1 px-3 text-xs font-semibold uppercase tracking-wide text-fg-muted">
                {t(GROUP_LABEL[group])}
              </h2>
              <ul aria-labelledby={headingId} className="flex flex-col gap-0.5">
                {items.map((item) => (
                  <li key={item.id}>
                    <NavLink
                      to={`/w/${workspaceId}/${item.path}`}
                      className={linkClass}
                      onClick={onNavigate}
                      data-nav-id={item.id}
                    >
                      {({ isActive }) => (
                        <>
                          <span aria-hidden="true" className={cx('-ml-3 h-6 w-1 rounded-r', isActive ? 'bg-accent' : 'bg-transparent')} />
                          <Icon name={item.icon} className="h-5 w-5 shrink-0" />
                          <span className="min-w-0 flex-1 truncate">{t(item.labelKey)}</span>
                        </>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          );
        })
      ) : (
        <p className="px-3 text-sm text-fg-muted">{t('layout.selectWorkspaceHint')}</p>
      )}
      <ul className="mt-auto flex flex-col gap-0.5 border-t border-border pt-3">
        <li>
          <NavLink to="/workspaces" end className={linkClass} onClick={onNavigate}>
            <Icon name="layers" className="h-5 w-5 shrink-0" />
            <span>{t('nav.workspaces')}</span>
          </NavLink>
        </li>
      </ul>
    </nav>
  );
}
