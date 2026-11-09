import { useEffect } from 'react';
import { Outlet } from 'react-router';
import { rememberWorkspace } from '../lib/storage';
import { WorkspaceNotFound } from '../pages/NotFoundPage';
import { useCurrentWorkspace } from '../workspace';

/** /w/:workspaceId/*: без членства — «не найдено» (как 404 сервера, API_DESIGN §4). */
export function WorkspaceGuard() {
  const { workspaceId, membership } = useCurrentWorkspace();
  useEffect(() => {
    if (membership) rememberWorkspace(membership.workspace_id);
  }, [membership]);
  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return <Outlet />;
}
