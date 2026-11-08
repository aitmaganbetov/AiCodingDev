import { SettingsPage } from '../features/settings/SettingsPage';
import type { AreaModule } from './types';

/** P7-13 Settings (member:read — все роли; управление участниками — owner). */
export const settingsArea: AreaModule = {
  id: 'settings',
  nav: { labelKey: 'nav.settings', path: 'settings', icon: 'settings', group: 'admin', order: 20, shortcut: 'o' },
  workspaceRoutes: [{ path: 'settings', element: <SettingsPage /> }],
};
