import type { RouteObject } from 'react-router';
import type { IconName } from '../components/ui/Icon';
import type { Permission } from '../lib/permissions';

export type NavGroup = 'work' | 'quality' | 'admin';

/** Пункт боковой навигации области (виден, когда выбран workspace). */
export interface AreaNav {
  /** Ключ i18n подписи (nav.json). */
  labelKey: string;
  /** Путь относительно /w/:workspaceId/. */
  path: string;
  icon: IconName;
  group: NavGroup;
  /** Порядок внутри группы. */
  order: number;
  /** Буква сочетания «g, затем буква». */
  shortcut?: string;
  /** Скрыть пункт для ролей без права (только UX; сервер всё равно проверяет). */
  permission?: Permission;
}

/**
 * Модуль области (экрана) Mission Control: маршруты + пункт навигации.
 * Каждая область — свой файл src/routes/<area>.tsx; App.tsx собирает их из src/routes/index.ts.
 */
export interface AreaModule {
  id: string;
  nav?: AreaNav;
  /** Маршруты внутри /w/:workspaceId/ (пути относительные, без ведущего «/»). Членство уже проверено. */
  workspaceRoutes?: RouteObject[];
  /** Маршруты вне workspace (внутри layout, после входа). */
  routes?: RouteObject[];
  /** Экран «домой» после входа: путь внутри workspace; выигрывает наибольший priority. */
  home?: { path: string; priority: number };
}
