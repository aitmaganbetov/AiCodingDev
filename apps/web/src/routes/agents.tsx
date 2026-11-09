import { RequirePermission } from '../auth/RequirePermission';
import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const AgentsPage = lazyPage(() => import('../features/agents/AgentsPage'), 'AgentsPage', ['agents']);
const AgentRolePage = lazyPage(() => import('../features/agents/AgentRolePage'), 'AgentRolePage', ['agents']);

/**
 * F5 «Агенты» (план §3.4, §5.7; D-2): каталог 12 ролей AgentRole (сетка/список) и карточка роли
 * `agents/:role` — профиль B7, активность по выполняемым задачам (B1), результаты analytics by_role,
 * провайдеры по настройкам workspace, история задач роли. Роли — логические профили, не сервисы.
 */
export const agentsArea: AreaModule = {
  id: 'agents',
  nav: { labelKey: 'nav.agents', path: 'agents', icon: 'bot', group: 'execution', order: 20, shortcut: 'e', permission: 'run:read' },
  workspaceRoutes: [
    {
      path: 'agents',
      element: (
        <RequirePermission permission="run:read">
          <AgentsPage />
        </RequirePermission>
      ),
    },
    {
      path: 'agents/:role',
      element: (
        <RequirePermission permission="run:read">
          <AgentRolePage />
        </RequirePermission>
      ),
    },
  ],
};
