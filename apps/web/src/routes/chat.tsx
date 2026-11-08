import { lazy } from 'react';
import { RequirePermission } from '../auth/RequirePermission';
import type { AreaModule } from './types';

const ChatPage = lazy(() => import('../features/chat/ChatPage'));

/** P7-06 Live Agent Chat + P7-07 директивы (namespace chat/directives). Контекстный экран run — без пункта меню. */
export const chatArea: AreaModule = {
  id: 'chat',
  workspaceRoutes: [
    {
      path: 'projects/:projectId/runs/:runId/chat',
      element: (
        <RequirePermission permission="chat:read">
          <ChatPage />
        </RequirePermission>
      ),
    },
  ],
};
