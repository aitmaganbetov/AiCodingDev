import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const SecurityCenterPage = lazyPage(() => import('../features/security/SecurityCenterPage'), 'SecurityCenterPage', ['security', 'quality', 'settings']);

/**
 * F7 Центр безопасности (план §3.7, D-3 `g y`): находки всех run workspace (GET /findings, B1),
 * распределение по серьёзности, CWE/контроли из данных, риски зависимостей (SCA/SBOM) и секреты
 * (SECRETS) по типу gate, evidence проверок, решение по находке (FSM, RBAC, step-up).
 */
export const securityArea: AreaModule = {
  id: 'security',
  nav: { labelKey: 'nav.security', path: 'security', icon: 'security', group: 'code', order: 20, shortcut: 'y', permission: 'quality:read' },
  workspaceRoutes: [
    {
      path: 'security',
      element: (
        <RequirePermission permission="quality:read">
          <SecurityCenterPage />
        </RequirePermission>
      ),
    },
  ],
};
