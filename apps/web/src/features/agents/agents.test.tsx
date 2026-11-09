import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { AgentProfile } from '../../api/agents';
import type { Provider } from '../../api/providers';
import type { WorkspaceTask } from '../../api/tasks';
import { AGENT_ROLE_ORDER, type AgentRole } from '../../lib/agentRoles';
import { json, mockFetch, renderApp, session, WS_ID } from '../../test/utils';
import {
  AGENTS_VIEW_KEY,
  activeRoleCount,
  activityByRole,
  eligibilityRows,
  profilesByRole,
  resolveView,
  roleState,
  roleStats,
  taskLinks,
} from './model';

function profile(role: AgentRole, init: Partial<AgentProfile> = {}): AgentProfile {
  return {
    role,
    template_version: '1.0.0',
    template_ref: `${role}@1.0.0`,
    template_sha256: 'a'.repeat(64),
    permissions: {
      write_scope: role === 'developer' ? 'worktree' : 'none',
      network: 'restricted',
      path_policy: role === 'developer' ? ['src/**'] : [],
      path_deny: ['.git/**'],
      task_paths_allowed: true,
      comments_only_paths: [],
    },
    prototype_permissions: null,
    allowed_tools: ['read', 'search'],
    required_capabilities: ['json_output'],
    acp_send: ['HANDOFF', 'TASK_COMPLETED'],
    complexity_band: { min: 'L1', max: 'L5' },
    independent_check_from: role === 'developer' ? 'L3' : null,
    must_differ_from_author: role === 'reviewer',
    can_send_fix_request: role === 'reviewer',
    eligible_providers: ['codex'],
    catalog: null,
    ...init,
  };
}

function wsTask(role: AgentRole, title: string, status: WorkspaceTask['status'] = 'RUNNING', n = 1): WorkspaceTask {
  return {
    id: `01900000-0000-7000-c000-${String(n).padStart(12, '0')}`,
    run_id: '01900000-0000-7000-d000-000000000001',
    project_id: '01900000-0000-7000-9000-000000000001',
    agent_role: role,
    title,
    status,
    blocked_reason: null,
    complexity: 'L2',
    risk_level: 'low',
    current_provider: 'codex',
    attempt_count: 1,
    max_attempts: 3,
    depends_on: [],
    version: 1,
    updated_at: '2026-11-08T08:00:00Z',
    run_status: 'RUNNING',
    created_at: '2026-11-08T07:00:00Z',
    current_attempt: {
      id: `01900000-0000-7000-e000-${String(n).padStart(12, '0')}`,
      attempt_no: 1,
      status: 'RUNNING',
      provider: 'codex',
      started_at: '2026-11-08T08:00:00Z',
      finished_at: null,
    },
  };
}

const SETTINGS = { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, settings_version: 1 };
function providers(): Provider[] {
  return [
    {
      provider_id: '01900000-0000-7000-b000-000000000001',
      code: 'codex',
      display_name: 'Codex CLI',
      enabled_globally: true,
      installations: [],
      workspace_settings: { ...SETTINGS, allowed_roles: ['developer', 'reviewer'] },
    },
    {
      provider_id: '01900000-0000-7000-b000-000000000002',
      code: 'claude',
      display_name: 'Claude Code CLI',
      enabled_globally: true,
      installations: [],
      workspace_settings: { ...SETTINGS, enabled: false, allowed_roles: ['developer'] },
    },
  ];
}

afterEach(() => {
  window.localStorage.clear();
});

describe('agents model', () => {
  it('groups only LEASED/RUNNING tasks of the 12 roles; never "online"', () => {
    const page = {
      items: [wsTask('developer', 'A', 'RUNNING', 1), wsTask('developer', 'B', 'LEASED', 2), wsTask('qa', 'C', 'SUCCEEDED', 3)],
      next_cursor: null,
    };
    const map = activityByRole(page);
    expect(map?.get('developer')?.tasks.map((t) => t.title)).toEqual(['A', 'B']);
    expect(map?.get('qa')?.tasks).toEqual([]);
    expect(roleState(map?.get('developer'), { pending: false, error: false })).toBe('active');
    expect(roleState(map?.get('qa'), { pending: false, error: false })).toBe('idle');
    expect(roleState(undefined, { pending: true, error: false })).toBe('loading');
    expect(roleState(undefined, { pending: false, error: true })).toBe('unknown');
    expect(activeRoleCount(map)).toEqual({ count: 1, more: false });
    expect(activityByRole(undefined)).toBeNull();
  });

  it('incomplete sample: a role without tasks is "unknown", not idle; count is a lower bound', () => {
    const map = activityByRole({ items: [wsTask('developer', 'A')], next_cursor: 'abc' });
    expect(roleState(map?.get('qa'), { pending: false, error: false })).toBe('unknown');
    expect(roleState(map?.get('developer'), { pending: false, error: false })).toBe('active');
    expect(activeRoleCount(map)).toEqual({ count: 1, more: true });
  });

  it('view: URL wins over stored choice, grid by default', () => {
    expect(resolveView(null, null)).toBe('grid');
    expect(resolveView(null, 'list')).toBe('list');
    expect(resolveView('grid', 'list')).toBe('grid');
    expect(resolveView('bogus', null)).toBe('grid');
    expect(AGENTS_VIEW_KEY.startsWith('aicd.layout.')).toBe(true);
  });

  it('stats: missing by_role row is null (no attempts), not zeros; eligibility from settings + server verdict', () => {
    expect(roleStats(undefined, 'qa')).toBeNull();
    const rows = eligibilityRows('reviewer', providers(), profile('reviewer'));
    expect(rows).toEqual([
      { code: 'codex', displayName: 'Codex CLI', enabledGlobally: true, enabledInWorkspace: true, allowed: true, eligible: true },
      { code: 'claude', displayName: 'Claude Code CLI', enabledGlobally: true, enabledInWorkspace: false, allowed: false, eligible: false },
    ]);
    expect(eligibilityRows('reviewer', providers(), undefined)[0]?.eligible).toBeNull();
    expect(profilesByRole([profile('qa'), { ...profile('qa'), role: 'orchestrator' as AgentRole }]).size).toBe(1);
    expect(taskLinks('ws', { id: 't', run_id: 'r', project_id: 'p' })).toEqual({
      detail: '/w/ws/projects/p/runs/r/tasks/t',
      mission: '/w/ws/mission-control?run=r&task=t',
    });
  });
});

describe('AgentsPage', () => {
  it('renders 12 role cards with activity from tasks, zero-stat roles as "no attempts", and switches to list (persisted)', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/agent-profiles': () => json(200, { items: AGENT_ROLE_ORDER.map((r) => profile(r)) }),
      'GET /api/v1/tasks': () => json(200, { items: [wsTask('developer', 'Экспорт CSV')], next_cursor: null }),
      'GET /api/v1/analytics/summary': () =>
        json(200, {
          tasks: { by_role: [{ role: 'developer', attempts: 4, succeeded: 3, failed: 1, retries: 1, verified_completion_rate: 0.75 }] },
        }),
    });
    renderApp(`/w/${WS_ID}/agents`);
    const catalog = await screen.findByRole('list', { name: 'Роли агентов' });
    expect(within(catalog).getAllByRole('article')).toHaveLength(12);
    const dev = within(catalog).getByRole('article', { name: 'Разработчик' });
    expect(await within(dev).findByText('Активна · выполняет 1 задачу')).toBeInTheDocument();
    expect(within(dev).getByRole('link', { name: 'Экспорт CSV' })).toHaveAttribute('href', expect.stringContaining('mission-control?run='));
    expect(within(dev).getByTestId('role-results')).toHaveTextContent('75%');
    const qa = within(catalog).getByRole('article', { name: 'QA' });
    expect(within(qa).getByText('Не активна · нет выполняемых задач')).toBeInTheDocument();
    expect(within(qa).getByTestId('role-no-attempts')).toBeInTheDocument();
    expect(within(catalog).queryByText(/онлайн|online/i)).toBeNull();
    const tasksCall = calls.find((c) => c.url.pathname === '/api/v1/tasks');
    expect(tasksCall?.url.searchParams.getAll('status')).toEqual(['LEASED', 'RUNNING']);

    await user.click(screen.getByRole('button', { name: 'Список' }));
    expect(screen.getByRole('button', { name: 'Список' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(window.localStorage.getItem(AGENTS_VIEW_KEY)).toBe('list');
  });
});
