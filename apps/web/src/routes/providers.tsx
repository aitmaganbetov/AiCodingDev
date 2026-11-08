import { ProvidersPage } from '../features/providers/ProvidersPage';
import type { AreaModule } from './types';

/** P7-09 CLI Providers (provider:read — все роли; probe/настройки — owner/admin). */
export const providersArea: AreaModule = {
  id: 'providers',
  nav: { labelKey: 'nav.providers', path: 'providers', icon: 'terminal', group: 'admin', order: 10, shortcut: 'c' },
  workspaceRoutes: [{ path: 'providers', element: <ProvidersPage /> }],
};
