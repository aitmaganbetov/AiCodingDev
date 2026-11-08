import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { Route, Routes } from 'react-router';
import { RequireAuth } from './auth/RequireAuth';
import { UnauthorizedBridge } from './auth/UnauthorizedBridge';
import { AppLayout } from './components/AppLayout';
import { HomeRedirect } from './pages/HomeRedirect';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { ProjectCreatePage } from './pages/ProjectCreatePage';
import { ProjectRunsPage } from './pages/ProjectRunsPage';
import { ProjectsPage } from './pages/ProjectsPage';
import { RunPage } from './pages/RunPage';
import { WorkspaceCreatePage } from './pages/WorkspaceCreatePage';
import { WorkspacesPage } from './pages/WorkspacesPage';

/** Маршруты web shell. Router передаётся снаружи (BrowserRouter / MemoryRouter в тестах). */
export function AppRoutes() {
  return (
    <>
      <UnauthorizedBridge />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppLayout />}>
            <Route index element={<HomeRedirect />} />
            <Route path="workspaces" element={<WorkspacesPage />} />
            <Route path="workspaces/new" element={<WorkspaceCreatePage />} />
            <Route path="w/:workspaceId/projects" element={<ProjectsPage />} />
            <Route path="w/:workspaceId/projects/new" element={<ProjectCreatePage />} />
            <Route path="w/:workspaceId/projects/:projectId/runs" element={<ProjectRunsPage />} />
            <Route path="w/:workspaceId/projects/:projectId/runs/:runId" element={<RunPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Routes>
    </>
  );
}

export function App({ queryClient }: { queryClient: QueryClient }) {
  return (
    <QueryClientProvider client={queryClient}>
      <AppRoutes />
    </QueryClientProvider>
  );
}
