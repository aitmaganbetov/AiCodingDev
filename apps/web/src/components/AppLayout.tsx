import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, NavLink, Outlet, useNavigate } from 'react-router';
import { api } from '../api/client';
import { clearSession, useSession } from '../auth/session';
import { useCurrentWorkspace } from '../workspace';
import { Disclosure } from './Disclosure';

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `inline-flex min-h-[44px] items-center rounded-md px-3 text-sm font-medium ${
    isActive ? 'bg-blue-50 text-blue-800 underline underline-offset-4' : 'text-slate-800 hover:bg-slate-100'
  }`;

function WorkspaceSwitcher() {
  const { t } = useTranslation();
  const { data: session } = useSession();
  const { workspaceId, membership } = useCurrentWorkspace();
  const workspaces = session?.workspaces ?? [];

  return (
    <Disclosure
      label={
        <span className="max-w-[10rem] truncate sm:max-w-[16rem]">
          <span className="sr-only">{t('layout.workspaceSwitcher')}: </span>
          {membership?.name ?? t('layout.noWorkspaceSelected')}
        </span>
      }
      panelClassName="absolute left-0 z-20 mt-2 w-72 max-w-[calc(100vw-2rem)] rounded-md border border-slate-200 bg-white p-2 shadow-lg"
    >
      <nav aria-label={t('layout.workspaceSwitcher')}>
        {workspaces.length > 0 ? (
          <ul className="flex flex-col">
            {workspaces.map((w) => (
              <li key={w.workspace_id}>
                <Link
                  to={`/w/${w.workspace_id}/projects`}
                  aria-current={w.workspace_id === workspaceId ? 'page' : undefined}
                  className="flex min-h-[44px] flex-col justify-center rounded px-3 py-1 hover:bg-slate-100 aria-[current=page]:bg-blue-50"
                >
                  <span className="text-sm font-medium">{w.name}</span>
                  <span className="text-xs text-slate-600">
                    {w.slug} · {t(`enums.workspace_role.${w.role}`, { defaultValue: w.role })}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="px-3 py-2 text-sm text-slate-700">{t('layout.noWorkspaces')}</p>
        )}
        <div className="mt-2 border-t border-slate-200 pt-2">
          <Link to="/workspaces" className="flex min-h-[44px] items-center rounded px-3 text-sm hover:bg-slate-100">
            {t('layout.allWorkspaces')}
          </Link>
        </div>
      </nav>
    </Disclosure>
  );
}

function UserMenu() {
  const { t } = useTranslation();
  const { data: session } = useSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const logout = useMutation({
    mutationFn: () => api.logout(),
    // Локальную сессию очищаем в любом случае: даже если сервер ответил 401/сеть недоступна.
    onSettled: () => {
      clearSession(queryClient, { explicitLogout: true });
      navigate('/login', { replace: true });
    },
  });

  if (!session) return null;
  return (
    <Disclosure
      label={
        <span className="max-w-[8rem] truncate sm:max-w-[14rem]">
          <span className="sr-only">{t('layout.userMenu')}: </span>
          {session.user.display_name}
        </span>
      }
    >
      <div className="flex flex-col gap-2 p-1">
        <p className="break-all px-2 text-sm text-slate-700">{session.user.email}</p>
        <button
          type="button"
          className="btn-secondary w-full"
          onClick={() => logout.mutate()}
          disabled={logout.isPending}
        >
          {logout.isPending ? t('auth.loggingOut') : t('auth.logout')}
        </button>
      </div>
    </Disclosure>
  );
}

export function AppLayout() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();

  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-white px-4 py-2 font-medium text-blue-800 focus:not-sr-only focus:absolute focus:left-4 focus:top-4"
        onClick={(e) => {
          e.preventDefault();
          document.getElementById('main')?.focus();
        }}
      >
        {t('layout.skipToContent')}
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <Link to="/" className="mr-auto rounded text-lg font-semibold text-slate-900">
            {t('app.name')}
          </Link>
          <WorkspaceSwitcher />
          <UserMenu />
        </div>
        <nav aria-label={t('layout.mainNav')} className="mx-auto max-w-5xl px-4 pb-2">
          <ul className="flex flex-wrap gap-1">
            {workspaceId ? (
              <li>
                <NavLink to={`/w/${workspaceId}/projects`} className={navLinkClass}>
                  {t('nav.projects')}
                </NavLink>
              </li>
            ) : null}
            <li>
              <NavLink to="/workspaces" className={navLinkClass}>
                {t('nav.workspaces')}
              </NavLink>
            </li>
          </ul>
        </nav>
      </header>
      <main id="main" tabIndex={-1} className="mx-auto w-full max-w-5xl flex-1 px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
