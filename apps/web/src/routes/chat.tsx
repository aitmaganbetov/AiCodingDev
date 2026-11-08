import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/** P7-06 Live Agent Chat + P7-07 директивы (namespace chat/directives). Контекстный экран run — без пункта меню. */
export const chatArea: AreaModule = {
  id: 'chat',
  workspaceRoutes: [{ path: 'projects/:projectId/runs/:runId/chat', element: <AreaPlaceholderPage titleKey="chat.title" /> }],
};
