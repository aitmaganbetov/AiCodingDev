import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const AnalyticsPage = lazyPage(() => import('../features/analytics/AnalyticsPage'), 'AnalyticsPage', ['analytics']);

/** P8-07 Analytics (ТЗ §11, §16; FR-OBS-05, FR-CI-08): метрики workspace за период. Право run:read — все роли. */
export const analyticsArea: AreaModule = {
  id: 'analytics',
  nav: { labelKey: 'nav.analytics', path: 'analytics', icon: 'analytics', group: 'platform', order: 20, shortcut: 'n', permission: 'run:read' },
  workspaceRoutes: [
    {
      path: 'analytics',
      element: (
        <RequirePermission permission="run:read">
          <AnalyticsPage />
        </RequirePermission>
      ),
    },
  ],
};
