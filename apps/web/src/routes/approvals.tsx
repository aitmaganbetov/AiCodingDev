import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const ApprovalsPage = lazyPage(() => import('../features/approvals/ApprovalsPages'), 'ApprovalsPage', ['approvals', 'quality', 'settings']);
const ApprovalDetailPage = lazyPage(() => import('../features/approvals/ApprovalsPages'), 'ApprovalDetailPage', ['approvals', 'quality', 'settings']);

/**
 * P7-11 Approvals (FR-APR-03, AC-13): ожидающие/решённые подтверждения, карточка (предмет, риск, срок),
 * approve/reject с подтверждением и причиной (If-Match + Idempotency-Key), обновления по SSE run.
 */
export const approvalsArea: AreaModule = {
  id: 'approvals',
  nav: { labelKey: 'nav.approvals', path: 'approvals', icon: 'approval', group: 'execution', order: 40, shortcut: 'a', permission: 'approval:read', badge: 'approvals' },
  workspaceRoutes: [
    {
      path: 'approvals',
      element: (
        <RequirePermission permission="approval:read">
          <ApprovalsPage />
        </RequirePermission>
      ),
    },
    {
      path: 'approvals/:approvalId',
      element: (
        <RequirePermission permission="approval:read">
          <ApprovalDetailPage />
        </RequirePermission>
      ),
    },
  ],
};
