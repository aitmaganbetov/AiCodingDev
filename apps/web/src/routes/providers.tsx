import { SettingsSection } from '../features/settings/SettingsSection';
import { ProvidersPage } from '../features/providers/ProvidersPage';
import { WorkspaceRedirect } from './redirect';
import type { AreaModule } from './types';

/**
 * P7-09 CLI Providers (provider:read — все роли; probe/настройки — owner/admin). С F2 — подраздел
 * Настроек (D-3): `settings/providers`; прежний `providers` — редирект с сохранением query (deep links).
 * Статус Codex/Claude Code — чипы в верхней панели (layout/ProviderChips).
 */
export const providersArea: AreaModule = {
  id: 'providers',
  workspaceRoutes: [
    {
      path: 'settings/providers',
      element: (
        <SettingsSection>
          <ProvidersPage />
        </SettingsSection>
      ),
    },
    { path: 'providers', element: <WorkspaceRedirect to="settings/providers" /> },
  ],
};
