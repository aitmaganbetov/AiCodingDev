import { lazy } from 'react';
import { SettingsPage } from '../features/settings/SettingsPage';
import { SettingsSection } from '../features/settings/SettingsSection';
import type { AreaModule, RouteHandle } from './types';

const AppearancePage = lazy(() => import('../features/settings/AppearancePage'));

/**
 * P7-13 Settings (member:read — все роли; управление участниками — owner). С F2 (D-3) подразделы:
 * рабочее пространство и участники (`settings`), CLI-провайдеры (`settings/providers`, область providers),
 * аудит (`settings/audit`, область audit), внешний вид (`settings/appearance`: тема, сброс раскладки).
 */
export const settingsArea: AreaModule = {
  id: 'settings',
  nav: { labelKey: 'nav.settings', path: 'settings', icon: 'settings', group: 'platform', order: 40, shortcut: 'o' },
  workspaceRoutes: [
    {
      path: 'settings',
      element: (
        <SettingsSection>
          <SettingsPage />
        </SettingsSection>
      ),
    },
    {
      path: 'settings/appearance',
      handle: { width: 'form' } satisfies RouteHandle,
      element: (
        <SettingsSection>
          <AppearancePage />
        </SettingsSection>
      ),
    },
  ],
};
