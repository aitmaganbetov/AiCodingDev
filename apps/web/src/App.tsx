import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { Suspense, type ReactNode } from 'react';
import { useRoutes, type RouteObject } from 'react-router';
import { StreamManagerProvider } from './api/useEventStream';
import type { StreamManager } from './api/streamManager';
import { RequireAuth } from './auth/RequireAuth';
import { UnauthorizedBridge } from './auth/UnauthorizedBridge';
import { LoadingState } from './components/states';
import { ToastProvider } from './components/ui/Toast';
import { TooltipProvider } from './components/ui/Tooltip';
import { AppLayout } from './layout/AppLayout';
import { WorkspaceGuard } from './layout/WorkspaceGuard';
import { HomeRedirect } from './pages/HomeRedirect';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { AREAS } from './routes';

/**
 * Дерево маршрутов собирается из реестра областей (src/routes). App.tsx при добавлении
 * экрана не меняется: экран = файл области + страница.
 */
export function buildRoutes(): RouteObject[] {
  return [
    { path: '/login', element: <LoginPage /> },
    {
      element: <RequireAuth />,
      children: [
        {
          element: <AppLayout />,
          children: [
            { index: true, element: <HomeRedirect /> },
            ...AREAS.flatMap((a) => a.routes ?? []),
            {
              path: 'w/:workspaceId',
              element: <WorkspaceGuard />,
              children: [...AREAS.flatMap((a) => a.workspaceRoutes ?? []), { path: '*', element: <NotFoundPage /> }],
            },
            { path: '*', element: <NotFoundPage /> },
          ],
        },
      ],
    },
  ];
}

const ROUTES = buildRoutes();

/** Маршруты приложения. Router передаётся снаружи (BrowserRouter / MemoryRouter в тестах). */
export function AppRoutes() {
  const element = useRoutes(ROUTES);
  return (
    <>
      <UnauthorizedBridge />
      <Suspense fallback={<LoadingState />}>{element}</Suspense>
    </>
  );
}

/** Провайдеры приложения: данные, SSE-менеджер, подсказки, уведомления. */
export function AppProviders({
  queryClient,
  streamManager,
  children,
}: {
  queryClient: QueryClient;
  streamManager?: StreamManager | undefined;
  children: ReactNode;
}) {
  const inner = (
    <TooltipProvider delayDuration={400}>
      <ToastProvider>{children}</ToastProvider>
    </TooltipProvider>
  );
  return (
    <QueryClientProvider client={queryClient}>
      {streamManager ? <StreamManagerProvider manager={streamManager}>{inner}</StreamManagerProvider> : inner}
    </QueryClientProvider>
  );
}

export function App({ queryClient, streamManager }: { queryClient: QueryClient; streamManager?: StreamManager }) {
  return (
    <AppProviders queryClient={queryClient} streamManager={streamManager}>
      <AppRoutes />
    </AppProviders>
  );
}
