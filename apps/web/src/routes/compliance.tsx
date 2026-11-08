import { RequirePermission } from '../auth/RequirePermission';
import { ComplianceIndexPage } from '../features/compliance/ComplianceIndexPage';
import { ProjectControlsPage } from '../features/compliance/ProjectControlsPage';
import { RunCompliancePage } from '../features/compliance/RunCompliancePage';
import type { AreaModule } from './types';

/**
 * P7-10 Standards & Compliance (FR-STD-05, ТЗ §13): матрица standard → control → applicability → роль →
 * check → evidence → verdict по run, вердикт человека; применимость/blocking контролей проекта.
 */
export const complianceArea: AreaModule = {
  id: 'compliance',
  nav: { labelKey: 'nav.compliance', path: 'compliance', icon: 'compliance', group: 'quality', order: 20, shortcut: 's', permission: 'quality:read' },
  workspaceRoutes: [
    {
      path: 'compliance',
      element: (
        <RequirePermission permission="quality:read">
          <ComplianceIndexPage />
        </RequirePermission>
      ),
    },
    {
      path: 'compliance/runs/:runId',
      element: (
        <RequirePermission permission="quality:read">
          <RunCompliancePage />
        </RequirePermission>
      ),
    },
    {
      path: 'compliance/projects/:projectId/controls',
      element: (
        <RequirePermission permission="project:read">
          <ProjectControlsPage />
        </RequirePermission>
      ),
    },
  ],
};
