import { lazyPage } from './lazyPage';
import type { AreaModule, RouteHandle } from './types';

const SettingsSection = lazyPage(() => import('../features/settings/SettingsSection'), 'SettingsSection', ['settings']);
const SettingsPage = lazyPage(() => import('../features/settings/SettingsPage'), 'SettingsPage', ['settings']);
const AppearancePage = lazyPage(() => import('../features/settings/AppearancePage'), 'default', ['settings']);

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
