import { matchRoutes } from 'react-router';
import { describe, expect, it } from 'vitest';
import { buildRoutes } from '../App';
import { AREAS, navEntries, workspaceHomePath } from '.';
import i18n from '../i18n';

describe('routes registry', () => {
  it('every sidebar entry resolves to a route inside the workspace and has a label', () => {
    const routes = buildRoutes();
    for (const entry of navEntries()) {
      const matches = matchRoutes(routes, `/w/ws-1/${entry.path}`);
      const leaf = matches?.at(-1)?.route;
      expect(leaf?.path, entry.id).toBe(entry.path);
      expect(i18n.t(entry.labelKey), entry.id).not.toBe(entry.labelKey);
    }
  });

  it('covers all Phase 7 sections with unique ids, paths and shortcuts', () => {
    const entries = navEntries();
    // IA редизайна (план §6.1, D-3): 14 пунктов в 4 группах; CLI-провайдеры и аудит — подразделы Настроек.
    expect(entries.map((e) => e.id)).toEqual([
      'dashboard',
      'projects',
      'mission-control',
      'agents',
      'runs',
      'approvals',
      'code',
      'security',
      'quality',
      'compliance',
      'integrations',
      'analytics',
      'knowledge',
      'settings',
    ]);
    expect(entries.map((e) => e.group)).toEqual([
      'overview',
      'overview',
      'execution',
      'execution',
      'execution',
      'execution',
      'code',
      'code',
      'code',
      'code',
      'platform',
      'platform',
      'platform',
      'platform',
    ]);
    const shortcuts = entries.map((e) => e.shortcut);
    expect(new Set(shortcuts).size).toBe(shortcuts.length);
    expect(new Set(AREAS.map((a) => a.id)).size).toBe(AREAS.length);
    const paths = AREAS.flatMap((a) => [...(a.workspaceRoutes ?? []), ...(a.routes ?? [])].map((r) => r.path));
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('home path is the area with the highest priority', () => {
    expect(workspaceHomePath()).toBe('dashboard');
    expect(workspaceHomePath(AREAS.filter((a) => a.id !== 'dashboard'))).toBe('projects');
    expect(workspaceHomePath([...AREAS, { id: 'x', home: { path: 'x', priority: 100 } }])).toBe('x');
  });
});

describe('redesign IA (F2)', () => {
  it('old URLs of sections moved into Settings still resolve (redirect routes) and widths come from route handles', async () => {
    const { mainWidthFor } = await import('.');
    const routes = buildRoutes();
    for (const path of ['providers', 'audit', 'settings/providers', 'settings/audit', 'settings/appearance', 'mission-control', 'agents', 'code', 'security']) {
      const leaf = matchRoutes(routes, `/w/ws-1/${path}`)?.at(-1)?.route;
      expect(leaf?.path, path).toBe(path);
    }
    expect(mainWidthFor('/w/ws-1/projects/new')).toBe('form');
    expect(mainWidthFor('/w/ws-1/mission-control')).toBe('full');
    expect(mainWidthFor('/w/ws-1/projects/p-1/runs/r-1/tasks/t-1')).toBe('full');
    expect(mainWidthFor('/w/ws-1/dashboard')).toBe('standard');
    expect(mainWidthFor('/workspaces/new')).toBe('form');
  });
});

describe('AI Mission Control (F4): old run screens redirect with the same selection', () => {
  it('run overview, graph and chat → mission-control with view/filters; Task Detail stays a route', async () => {
    const { missionControlTarget } = await import('./redirect');
    expect(missionControlTarget('r-1', 'run', '')).toBe('mission-control?run=r-1&view=system');
    expect(missionControlTarget('r-1', 'graph', '')).toBe('mission-control?run=r-1');
    expect(missionControlTarget('r-1', 'graph', '?view=list')).toBe('mission-control?run=r-1&nav=list');
    expect(missionControlTarget('r-1', 'chat', '?stream=role&role=reviewer&q=%D1%8D%D0%BA%D1%81')).toBe(
      'mission-control?run=r-1&view=conversation&stream=role&role=reviewer&q=%D1%8D%D0%BA%D1%81',
    );
    expect(missionControlTarget('r-1', 'chat', '?stream=task&task=t-9')).toBe('mission-control?run=r-1&view=conversation&stream=task&ctask=t-9&task=t-9');
    const routes = buildRoutes();
    expect(matchRoutes(routes, '/w/ws-1/projects/p-1/runs/r-1/tasks/t-1')?.at(-1)?.route.path).toBe('projects/:projectId/runs/:runId/tasks/:taskId');
  });
});
