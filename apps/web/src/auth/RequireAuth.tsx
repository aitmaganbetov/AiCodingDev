import { Navigate, Outlet, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ErrorState, LoadingState } from '../components/states';
import { useSession, wasLoggedOut } from './session';

/** Охрана маршрутов на клиенте — только UX; доступ к данным проверяет сервер. */
export function RequireAuth() {
  const { t } = useTranslation();
  const location = useLocation();
  const session = useSession();

  if (session.isPending) {
    return (
      <main id="main" tabIndex={-1} className="mx-auto max-w-5xl px-4">
        <LoadingState label={t('auth.checkingSession')} />
      </main>
    );
  }
  if (session.isError) {
    return (
      <main id="main" tabIndex={-1} className="mx-auto max-w-5xl px-4 py-10">
        <ErrorState error={session.error} onRetry={() => void session.refetch()} />
      </main>
    );
  }
  if (!session.data) {
    const next = `${location.pathname}${location.search}`;
    const plain = next === '/' || wasLoggedOut();
    return <Navigate to={plain ? '/login' : `/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <Outlet />;
}
