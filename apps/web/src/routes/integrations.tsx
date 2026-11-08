import { GitHubPage, GitHubSetupRedirect } from '../features/integrations/GitHubPage';
import type { AreaModule } from './types';

/**
 * Интеграции (P8-02, OQ-47 (2)): установки GitHub App (github:read — все роли, управление — owner/admin).
 * `/integrations/github/setup` — Setup URL GitHub App (вне workspace): возврат с GitHub после установки.
 */
export const integrationsArea: AreaModule = {
  id: 'integrations',
  nav: { labelKey: 'nav.integrations', path: 'integrations', icon: 'layers', group: 'admin', order: 15, shortcut: 'i', permission: 'github:read' },
  workspaceRoutes: [{ path: 'integrations', element: <GitHubPage /> }],
  routes: [{ path: 'integrations/github/setup', element: <GitHubSetupRedirect /> }],
};
