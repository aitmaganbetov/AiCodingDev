import { RequirePermission } from '../auth/RequirePermission';
import { AreaPlaceholderPage } from '../pages/AreaPlaceholderPage';
import type { AreaModule } from './types';

/**
 * Разделы нового IA (план §6.1, D-3), экраны которых появятся в следующих фазах. До тех пор —
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

export const securityArea: AreaModule = {
  id: 'security',
  nav: { labelKey: 'nav.security', path: 'security', icon: 'security', group: 'code', order: 20, shortcut: 'y', permission: 'quality:read' },
  workspaceRoutes: [
    {
      path: 'security',
      element: (
        <RequirePermission permission="quality:read">
          <AreaPlaceholderPage
            titleKey="nav.security"
            phase="F7"
            descriptionKey="shell.pending.security"
            related={[{ to: 'quality', labelKey: 'shell.pending.toQuality' }]}
          />
        </RequirePermission>
      ),
    },
  ],
};
