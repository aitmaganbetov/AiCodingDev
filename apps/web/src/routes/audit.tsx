import { RequirePermission } from '../auth/RequirePermission';
import { AuditPage } from '../features/audit/AuditPage';
import type { AreaModule } from './types';

/** P7-13 Audit (право audit:read: owner/admin). */
export const auditArea: AreaModule = {
  id: 'audit',
  nav: { labelKey: 'nav.audit', path: 'audit', icon: 'audit', group: 'admin', order: 30, shortcut: 'u', permission: 'audit:read' },
  workspaceRoutes: [
    {
      path: 'audit',
      element: (
        <RequirePermission permission="audit:read">
          <AuditPage />
        </RequirePermission>
      ),
    },
  ],
};
