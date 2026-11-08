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
    expect(entries.map((e) => e.id)).toEqual([
      'dashboard',
      'projects',
      'runs',
      'approvals',
      'quality',
      'compliance',
      'knowledge',
      'providers',
      'settings',
      'audit',
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
