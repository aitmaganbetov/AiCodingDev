import { Navigate } from 'react-router';
import { useSession } from '../auth/session';
import { lastWorkspace } from '../lib/storage';
import { workspaceHomePath } from '../routes';

export function HomeRedirect() {
  const { data: session } = useSession();
  const workspaces = session?.workspaces ?? [];
  const remembered = lastWorkspace();
  const target = workspaces.find((w) => w.workspace_id === remembered) ?? workspaces[0];
  return <Navigate to={target ? `/w/${target.workspace_id}/${workspaceHomePath()}` : '/workspaces'} replace />;
}
