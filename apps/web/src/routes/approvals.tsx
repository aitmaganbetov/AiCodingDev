import { RequirePermission } from '../auth/RequirePermission';
import { ApprovalDetailPage, ApprovalsPage } from '../features/approvals/ApprovalsPages';
import type { AreaModule } from './types';

/**
 * P7-11 Approvals (FR-APR-03, AC-13): ожидающие/решённые подтверждения, карточка (предмет, риск, срок),
 * approve/reject с подтверждением и причиной (If-Match + Idempotency-Key), обновления по SSE run.
 */
export const approvalsArea: AreaModule = {
  id: 'approvals',
  nav: { labelKey: 'nav.approvals', path: 'approvals', icon: 'approval', group: 'work', order: 40, shortcut: 'a', permission: 'approval:read' },
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
