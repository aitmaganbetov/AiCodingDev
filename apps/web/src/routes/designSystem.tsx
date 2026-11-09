import { lazyPage } from './lazyPage';
import type { AreaModule } from './types';

const DesignSystemPage = lazyPage(() => import('../features/designSystem/DesignSystemPage'), 'default', []);

/**
 * Справочник дизайн-системы (F1 редизайна) — для разработки UI. Пункта навигации нет (не входит
 * в меню продукта); страница грузится отдельным чанком и не влияет на начальный бандл.
 */
export const designSystemArea: AreaModule = {
  id: 'design-system',
  workspaceRoutes: [{ path: 'settings/design-system', element: <DesignSystemPage /> }],
};
