import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { NavLink } from 'react-router';
import { Icon, type IconName } from '../../components/ui/Icon';
import { cx } from '../../components/ui/cx';
import { can, type Permission } from '../../lib/permissions';
import { useCurrentWorkspace } from '../../workspace';

const SECTIONS: readonly { path: string; labelKey: string; icon: IconName; permission?: Permission; end?: boolean }[] = [
  { path: 'settings', labelKey: 'settings.sections.workspace', icon: 'settings', end: true },
  { path: 'settings/providers', labelKey: 'settings.sections.providers', icon: 'terminal', permission: 'provider:read' },
  { path: 'settings/audit', labelKey: 'settings.sections.audit', icon: 'audit', permission: 'audit:read' },
  { path: 'settings/appearance', labelKey: 'settings.sections.appearance', icon: 'palette' },
];

/**
 * Подразделы Настроек (D-3, план §6.1 п. 13): рабочее пространство и участники, CLI-провайдеры,
 * аудит (owner/admin), внешний вид. Навигация ссылками (aria-current), каждый подраздел — свой экран
 * с h1 и deep link; старые `providers` и `audit` перенаправляются сюда.
 */
export function SettingsSection({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  if (!workspaceId) return <>{children}</>;
  return (
    <div className="flex flex-col gap-4">
      <nav aria-label={t('settings.sections.label')} className="-mx-1 overflow-x-auto" data-testid="settings-nav">
        <ul className="flex flex-wrap gap-1 border-b border-border px-1 sm:min-w-max sm:flex-nowrap">
          {SECTIONS.filter((s) => !s.permission || can(membership?.role, s.permission)).map((s) => (
            <li key={s.path}>
              <NavLink
                to={`/w/${workspaceId}/${s.path}`}
                end={s.end}
                className={({ isActive }) =>
                  cx(
                    '-mb-px inline-flex min-h-touch items-center gap-2 border-b-2 px-3 text-sm font-medium no-underline transition-colors duration-instant',
                    isActive ? 'border-accent text-fg' : 'border-transparent text-fg-muted hover:text-fg',
                  )
                }
              >
                <Icon name={s.icon} className="h-4 w-4" />
                {t(s.labelKey)}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
      {children}
    </div>
  );
}
