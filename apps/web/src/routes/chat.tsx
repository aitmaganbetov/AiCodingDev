import { RunRedirect } from './redirect';
import type { AreaModule } from './types';

/**
 * P7-06 Live Agent Chat + P7-07 директивы. С F4 — центральная панель AI Mission Control (вид «Беседа
 * агентов», поручения — Command Center); прежний путь перенаправляется с сохранением фильтров.
 */
export const chatArea: AreaModule = {
  id: 'chat',
  workspaceRoutes: [{ path: 'projects/:projectId/runs/:runId/chat', element: <RunRedirect from="chat" /> }],
};
