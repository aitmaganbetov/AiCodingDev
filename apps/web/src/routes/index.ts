/**
 * Реестр областей Mission Control. Каждая область — свой файл src/routes/<area>.tsx
 * (маршруты + пункт навигации); здесь — только список импортов.
 * Новая область: создать файл с `AreaModule` и добавить одну строку импорта и элемент в AREAS.
 */
import { analyticsArea } from './analytics';
import { approvalsArea } from './approvals';
import { auditArea } from './audit';
import { chatArea } from './chat';
import { complianceArea } from './compliance';
import { dashboardArea } from './dashboard';
import { integrationsArea } from './integrations';
import { knowledgeArea } from './knowledge';
import { projectsArea } from './projects';
import { providersArea } from './providers';
import { qualityArea } from './quality';
import { runsArea } from './runs';
import { settingsArea } from './settings';
import { tasksArea } from './tasks';
import type { AreaModule, AreaNav, NavGroup } from './types';
import { workspacesArea } from './workspaces';

export const AREAS: readonly AreaModule[] = [
  dashboardArea,
  projectsArea,
  runsArea,
  chatArea,
  tasksArea,
  approvalsArea,
  qualityArea,
  complianceArea,
  knowledgeArea,
  analyticsArea,
  providersArea,
  integrationsArea,
  settingsArea,
  auditArea,
  workspacesArea,
];

export const NAV_GROUPS: readonly NavGroup[] = ['work', 'quality', 'admin'];

export interface NavEntry extends AreaNav {
  id: string;
}

/** Пункты навигации по группам, в порядке `order`. */
export function navEntries(areas: readonly AreaModule[] = AREAS): NavEntry[] {
  return areas
    .flatMap((a) => (a.nav ? [{ id: a.id, ...a.nav }] : []))
    .sort((a, b) => NAV_GROUPS.indexOf(a.group) - NAV_GROUPS.indexOf(b.group) || a.order - b.order);
}

/** Путь «домой» внутри workspace (область с наибольшим home.priority). */
export function workspaceHomePath(areas: readonly AreaModule[] = AREAS): string {
  let best: { path: string; priority: number } | undefined;
  for (const a of areas) if (a.home && (!best || a.home.priority > best.priority)) best = a.home;
  return best?.path ?? 'projects';
}

export type { AreaModule, AreaNav, NavGroup } from './types';
