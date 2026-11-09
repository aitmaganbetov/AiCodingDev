import { Navigate, useLocation, useParams } from 'react-router';

/**
 * Редирект старого пути внутри workspace на новый (F2, план §6.1): query и hash сохраняются,
 * чтобы прежние deep links (фильтры аудита, `?provider=`) открывали тот же контент.
 */
export function WorkspaceRedirect({ to }: { to: string }) {
  const { workspaceId } = useParams();
  const { search, hash } = useLocation();
  return <Navigate replace to={`/w/${workspaceId ?? ''}/${to}${search}${hash}`} />;
}
