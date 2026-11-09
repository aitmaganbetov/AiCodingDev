import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const ComplianceIndexPage = lazyPage(() => import('../features/compliance/ComplianceIndexPage'), 'ComplianceIndexPage', ['compliance', 'quality']);
const ProjectControlsPage = lazyPage(() => import('../features/compliance/ProjectControlsPage'), 'ProjectControlsPage', ['compliance', 'quality']);
const RunCompliancePage = lazyPage(() => import('../features/compliance/RunCompliancePage'), 'RunCompliancePage', ['compliance', 'quality']);

/**
 * P7-10 Standards & Compliance (FR-STD-05, ТЗ §13): матрица standard → control → applicability → роль →
 * check → evidence → verdict по run, вердикт человека; применимость/blocking контролей проекта.
 */
export const complianceArea: AreaModule = {
  id: 'compliance',
  nav: { labelKey: 'nav.compliance', path: 'compliance', icon: 'compliance', group: 'code', order: 40, shortcut: 's', permission: 'quality:read' },
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
