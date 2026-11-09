import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule, RouteHandle } from './types';

const CodeReviewPage = lazyPage(() => import('../features/review/CodeReviewPage'), 'default', ['review', 'projects', 'tasks', 'runs', 'quality', 'approvals', 'directives', 'chat', 'settings']);

/**
 * Code Review Workspace (F6, план §3.5; ADR-0015): diff run/задачи, inline-комментарии B5, находки,
 * проверки и отправка ревью. Контекстный экран проекта/run — без пункта меню (вход: проект › Запуски,
 * Task Detail › «Ревью кода», worktrees). Monaco — отдельный ленивый чанк внутри экрана.
 */
export const reviewArea: AreaModule = {
  id: 'review',
  workspaceRoutes: [
    {
      path: 'projects/:projectId/review/:runId',
      handle: { width: 'full' } satisfies RouteHandle,
      element: (
        <RequirePermission permission="review:read">
          <CodeReviewPage />
        </RequirePermission>
      ),
    },
  ],
};
