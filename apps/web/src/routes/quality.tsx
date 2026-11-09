import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const EvidencePage = lazyPage(() => import('../features/quality/EvidencePage'), 'EvidencePage', ['quality']);
const FindingPage = lazyPage(() => import('../features/quality/FindingPage'), 'FindingPage', ['quality']);
const QualityIndexPage = lazyPage(() => import('../features/quality/RunQualityPage'), 'QualityIndexPage', ['quality']);
const RunQualityPage = lazyPage(() => import('../features/quality/RunQualityPage'), 'RunQualityPage', ['quality']);

/**
 * P7-10 Evidence & Quality (FR-QG-07): выбор run → сводка verdict/gates (SKIPPED/ERROR ≠ PASS),
 * находки и решения по ним, evidence (AC-14), отчёт run. Живые обновления — SSE run.
 */
export const qualityArea: AreaModule = {
  id: 'quality',
  nav: { labelKey: 'nav.quality', path: 'quality', icon: 'quality', group: 'code', order: 30, shortcut: 'q', permission: 'quality:read' },
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
