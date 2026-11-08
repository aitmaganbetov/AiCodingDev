import { RequirePermission } from '../auth/RequirePermission';
import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-13 Audit (право audit:read: owner/admin). Заглушка «в разработке»: замените element страницей области. */
export const auditArea: AreaModule = {
  id: 'audit',
  nav: { labelKey: 'nav.audit', path: 'audit', icon: 'audit', group: 'admin', order: 30, shortcut: 'u', permission: 'audit:read' },
  workspaceRoutes: [
    {
      path: 'audit',
      element: (
        <RequirePermission permission="audit:read">
          <AreaPlaceholderPage titleKey="audit.title" />
        </RequirePermission>
      ),
    },
  ],
};
