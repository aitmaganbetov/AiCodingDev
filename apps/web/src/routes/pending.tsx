import { RequirePermission } from '../auth/RequirePermission';
import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/**
 * Раздел нового IA (план §6.1, D-3), экран которого появится в следующей фазе (F8). До тех пор —
 * честное состояние «Раздел в разработке» с фазой и ссылками на существующие экраны с теми же
 * данными (никакого демо-содержимого, принцип 2 плана).
 */
export const codeIntelligenceArea: AreaModule = {
  id: 'code',
  nav: { labelKey: 'nav.code', path: 'code', icon: 'code', group: 'code', order: 10, shortcut: 'x', permission: 'project:read' },
  workspaceRoutes: [
    {
      path: 'code',
      element: (
        <RequirePermission permission="project:read">
          <AreaPlaceholderPage
            titleKey="nav.code"
            phase="F8"
            descriptionKey="shell.pending.code"
            related={[
              { to: 'projects', labelKey: 'shell.pending.toProjects' },
              { to: 'knowledge', labelKey: 'shell.pending.toKnowledge' },
            ]}
          />
        </RequirePermission>
      ),
    },
  ],
};
