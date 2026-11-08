import { useMatch } from 'react-router';
import type { WorkspaceMembership } from './api/client';
import { useSession } from './auth/session';

/** Текущий workspace берётся из URL (/w/:workspaceId/...), роль — из членств сессии. */
export function useCurrentWorkspace(): { workspaceId: string | null; membership: WorkspaceMembership | null } {
  const match = useMatch('/w/:workspaceId/*');
  const { data: session } = useSession();
  const workspaceId = match?.params.workspaceId ?? null;
  const membership = session?.workspaces.find((w) => w.workspace_id === workspaceId) ?? null;
  return { workspaceId, membership };
}
