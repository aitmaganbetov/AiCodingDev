import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import { WorkspaceRedirect } from './redirect';
import type { AreaModule } from './types';

const SettingsSection = lazyPage(() => import('../features/settings/SettingsSection'), 'SettingsSection', ['settings']);
const AuditPage = lazyPage(() => import('../features/audit/AuditPage'), 'AuditPage', ['audit']);

/** P7-13 Audit (право audit:read: owner/admin). С F2 — подраздел Настроек (D-3); `audit` — редирект. */
export const auditArea: AreaModule = {
  id: 'audit',
  workspaceRoutes: [
    {
      path: 'settings/audit',
      element: (
        <SettingsSection>
          <RequirePermission permission="audit:read">
            <AuditPage />
          </RequirePermission>
        </SettingsSection>
      ),
    },
    { path: 'audit', element: <WorkspaceRedirect to="settings/audit" /> },
  ],
};
