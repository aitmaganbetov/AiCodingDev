import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule, RouteHandle } from './types';

const MissionControlPage = lazyPage(() => import('../features/missionControl/MissionControlPage'), 'default', [
  'missionControl',
  'runs',
  'chat',
  'directives',
  'tasks',
  'quality',
  'integrations',
  'settings',
]);

/**
 * AI Mission Control (F4, план §3.3, §6.2): центральный экран исполнения. `mission-control` — выбор
 * активного run; `?run=<id>` — Navigator (этапы, DAG/список задач) · Live Agent Chat (5 видов) ·
 * User Command Center · Agent Inspector (контекстная панель). Прежние экраны run/графа/чата
 * перенаправляются сюда (`routes/redirect.tsx`), Task Detail остаётся отдельным экраном.
 */
export const missionControlArea: AreaModule = {
  id: 'mission-control',
  nav: { labelKey: 'nav.missionControl', path: 'mission-control', icon: 'missionControl', group: 'execution', order: 10, shortcut: 'm', permission: 'run:read' },
  workspaceRoutes: [
    {
      path: 'mission-control',
      handle: { width: 'full' } satisfies RouteHandle,
      element: (
        <RequirePermission permission="run:read">
          <MissionControlPage />
        </RequirePermission>
      ),
    },
  ],
};
