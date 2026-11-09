/**
 * Реестр областей Mission Control. Каждая область — свой файл src/routes/<area>.tsx
 * (маршруты + пункт навигации); здесь — только список импортов.
 * Новая область: создать файл с `AreaModule` и добавить одну строку импорта и элемент в AREAS.
 */
import { agentsArea } from './agents';
import { analyticsArea } from './analytics';
import { approvalsArea } from './approvals';
import { auditArea } from './audit';
import { chatArea } from './chat';
import { complianceArea } from './compliance';
import { dashboardArea } from './dashboard';
import { designSystemArea } from './designSystem';
import { integrationsArea } from './integrations';
import { knowledgeArea } from './knowledge';
import { missionControlArea } from './missionControl';
import { codeIntelligenceArea } from './codeIntelligence';
import { projectsArea } from './projects';
import { providersArea } from './providers';
import { qualityArea } from './quality';
import { reviewArea } from './review';
import { runsArea } from './runs';
import { securityArea } from './security';
import { settingsArea } from './settings';
import { tasksArea } from './tasks';
import { matchPath, type RouteObject } from 'react-router';
import type { AreaModule, AreaNav, MainWidth, NavGroup, RouteHandle } from './types';
import { workspacesArea } from './workspaces';

export const AREAS: readonly AreaModule[] = [
  dashboardArea,
  projectsArea,
  missionControlArea,
  agentsArea,
  runsArea,
  chatArea,
  tasksArea,
  reviewArea,
  approvalsArea,
  codeIntelligenceArea,
  securityArea,
  qualityArea,
  complianceArea,
  knowledgeArea,
  analyticsArea,
  providersArea,
  integrationsArea,
  settingsArea,
  auditArea,
  workspacesArea,
  designSystemArea,
];

export const NAV_GROUPS: readonly NavGroup[] = ['overview', 'execution', 'code', 'platform'];

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

/** Ширина рабочей области для пути (handle.width маршрута; по умолчанию `standard`). */
export function mainWidthFor(pathname: string, areas: readonly AreaModule[] = AREAS): MainWidth {
  const check = (routes: readonly RouteObject[] | undefined, prefix: string): MainWidth | null => {
    for (const r of routes ?? []) {
      const width = (r.handle as RouteHandle | undefined)?.width;
      if (width && r.path && matchPath({ path: `${prefix}${r.path}`, end: true }, pathname)) return width;
    }
    return null;
  };
  for (const a of areas) {
    const w = check(a.workspaceRoutes, '/w/:workspaceId/') ?? check(a.routes, '/');
    if (w) return w;
  }
  return 'standard';
}

export type { AreaModule, AreaNav, MainWidth, NavGroup, RouteHandle } from './types';
