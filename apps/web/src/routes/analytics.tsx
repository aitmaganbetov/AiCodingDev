import { RequirePermission } from '../auth/RequirePermission';
import { AnalyticsPage } from '../features/analytics/AnalyticsPage';
import type { AreaModule } from './types';

/** P8-07 Analytics (ТЗ §11, §16; FR-OBS-05, FR-CI-08): метрики workspace за период. Право run:read — все роли. */
export const analyticsArea: AreaModule = {
  id: 'analytics',
  nav: { labelKey: 'analytics.nav', path: 'analytics', icon: 'layers', group: 'quality', order: 40, shortcut: 'n', permission: 'run:read' },
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
