import { RequirePermission } from '../auth/RequirePermission';
import { EvidencePage } from '../features/quality/EvidencePage';
import { FindingPage } from '../features/quality/FindingPage';
import { QualityIndexPage, RunQualityPage } from '../features/quality/RunQualityPage';
import type { AreaModule } from './types';

/**
 * P7-10 Evidence & Quality (FR-QG-07): выбор run → сводка verdict/gates (SKIPPED/ERROR ≠ PASS),
 * находки и решения по ним, evidence (AC-14), отчёт run. Живые обновления — SSE run.
 */
export const qualityArea: AreaModule = {
  id: 'quality',
  nav: { labelKey: 'nav.quality', path: 'quality', icon: 'quality', group: 'quality', order: 10, shortcut: 'q', permission: 'quality:read' },
  workspaceRoutes: [
    {
      path: 'quality',
      element: (
        <RequirePermission permission="quality:read">
          <QualityIndexPage />
        </RequirePermission>
      ),
    },
    {
      path: 'quality/runs/:runId',
      element: (
        <RequirePermission permission="quality:read">
          <RunQualityPage />
        </RequirePermission>
      ),
    },
    {
      path: 'quality/findings/:findingId',
      element: (
        <RequirePermission permission="quality:read">
          <FindingPage />
        </RequirePermission>
      ),
    },
    {
      path: 'quality/evidence/:evidenceId',
      element: (
        <RequirePermission permission="evidence:read">
          <EvidencePage />
        </RequirePermission>
      ),
    },
  ],
};
