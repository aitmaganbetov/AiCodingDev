import type { components } from '../../src/api/schema';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type Reply } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

const ME = '01900000-0000-7000-8000-0000000000aa';
const OTHER = { id: '01900000-0000-7000-8000-0000000000bb', display_name: 'Борис Иванов' };

/** Approval.context в контракте — свободный object (генератор типов даёт Record<string, never>). */
function ctx(value: Record<string, unknown>): S['Approval']['context'] {
  return value as unknown as S['Approval']['context'];
}

function approval(init: Partial<S['Approval']> & Pick<S['Approval'], 'id' | 'workspace_id' | 'action_type'>): S['Approval'] {
  return {
    run_id: null,
    status: 'PENDING',
    risk_level: 'medium',
    summary: 'Действие',
    requested_by: { actor_type: 'system' },
    can_decide: true,
    created_at: '2026-11-08T08:30:00Z',
    expires_at: '2030-01-01T00:00:00Z',
    decided_at: null,
    decided_by: null,
    decision_reason: null,
    task_id: null,
    context: {},
    version: 1,
    ...init,
  };
}

/**
 * AC-13: run ждёт утверждения плана (AWAITING_APPROVAL), merge запрошен самим пользователем (four-eyes),
 * деструктивная миграция — сервер отвечает four_eyes_violation, решённый production deploy.
 */
function seed(api: MockApi, role: S['WorkspaceRole'] = 'developer') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'AWAITING_APPROVAL', pending_approvals: 3, version: 4 });
  const approvals: S['Approval'][] = [
    approval({
      id: '01900000-0000-7000-8000-0000000a0001',
      workspace_id: ws.id,
      run_id: run.id,
      action_type: 'PLAN_APPROVAL',
      risk_level: 'medium',
      summary: 'План v1: 2 задачи (API, экспорт CSV) <b>не HTML</b>',
      context: ctx({ plan_version: 1, tasks: ['Спроектировать API', 'Реализовать экспорт'] }),
      version: 3,
    }),
    approval({
      id: '01900000-0000-7000-8000-0000000a0002',
      workspace_id: ws.id,
      run_id: run.id,
      action_type: 'MERGE',
      risk_level: 'high',
      summary: 'Слияние ветки feature/export в main',
      requested_by: { actor_type: 'user', user: { id: ME, display_name: 'Анна Петрова' } },
      can_decide: false,
      context: ctx({ target_branch: 'main', head_sha: 'c'.repeat(40) }),
    }),
    approval({
      id: '01900000-0000-7000-8000-0000000a0003',
      workspace_id: ws.id,
      run_id: run.id,
      action_type: 'DESTRUCTIVE_MIGRATION',
      risk_level: 'critical',
      summary: 'DROP COLUMN orders.legacy_status',
      requested_by: { actor_type: 'agent', role: 'database' },
    }),
    approval({
      id: '01900000-0000-7000-8000-0000000a0004',
      workspace_id: ws.id,
      action_type: 'PRODUCTION_DEPLOY',
      status: 'REJECTED',
      risk_level: 'critical',
      summary: 'Деплой v2.3.0',
      decided_at: '2026-11-08T08:45:00Z',
      decided_by: OTHER,
      decision_reason: 'Сначала нагрузочное тестирование',
      version: 2,
    }),
  ];
  // Сервер: four-eyes для миграции (инициатор — автор run) вопреки can_decide из устаревшего списка.
  const fourEyesDenied = new Set(['01900000-0000-7000-8000-0000000a0003']);
  const decisions: { id: string; headers: Record<string, string>; body: unknown }[] = [];

  api.route('GET', '/api/v1/approvals', 'listApprovals', ({ url, workspaceId }) => {
    const statuses = url.searchParams.getAll('status');
    const mine = url.searchParams.get('decidable_by_me') === 'true';
    const runId = url.searchParams.get('run_id');
    const items = approvals.filter(
      (a) =>
        a.workspace_id === workspaceId &&
        (statuses.length === 0 ? a.status === 'PENDING' : statuses.includes(a.status)) &&
        (!mine || a.can_decide) &&
        (!runId || a.run_id === runId),
    );
    return { status: 200, body: { items, next_cursor: null } };
  });
  api.route('GET', '/api/v1/approvals/{id}', 'getApproval', ({ id }) => {
    const a = approvals.find((x) => x.id === id);
    return a ? { status: 200, body: a, headers: { ETag: `"v${a.version}"` } } : problem(404, 'not_found');
  });
  api.route('POST', '/api/v1/approvals/{id}/decision', 'decideApproval', ({ id, headers, body }): Reply => {
    const a = approvals.find((x) => x.id === id);
    if (!a) return problem(404, 'not_found');
    decisions.push({ id: a.id, headers, body });
    if (!a.can_decide || fourEyesDenied.has(a.id)) return problem(403, 'four_eyes_violation');
    if (headers['if-match'] !== `"v${a.version}"`) return problem(412, 'precondition_failed');
    if (a.status !== 'PENDING') return problem(409, 'invalid_state_transition', { current_status: a.status } as Partial<S['Problem']>);
    const req = body as S['ApprovalDecisionRequest'];
    if ((req.decision === 'REJECTED' || a.risk_level === 'critical') && !req.reason) {
      return problem(422, 'validation_error', { errors: [{ pointer: '/reason', code: 'required' }] });
    }
    a.status = req.decision;
    a.decided_at = '2026-11-08T09:10:00Z';
    a.decided_by = { id: ME, display_name: 'Анна Петрова' };
    a.decision_reason = req.reason ?? null;
    a.can_decide = false;
    a.version += 1;
    // Решение публикуется событиями run (outbox): approval.decided и переход run по плану.
    api.addEvent(run, 'approval.decided', { approval_id: a.id, action_type: a.action_type, status: a.status, decided_by: ME });
    if (a.action_type === 'PLAN_APPROVAL' && a.status === 'APPROVED') {
      api.addEvent(run, 'run.state_changed', { from_status: 'AWAITING_APPROVAL', to_status: 'RUNNING', run_version: run.version + 1 });
    }
    return { status: 200, body: a, headers: { ETag: `"v${a.version}"` } };
  });
  return { ws, project, run, approvals, decisions };
}

test('AC-13: plan approval gates execution — run waits in AWAITING_APPROVAL until a human approves; live update via SSE', async ({ page, api }) => {
  const { ws, run, decisions } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Одобрения' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/approvals`);
  await expect(page.getByRole('heading', { level: 1, name: 'Одобрения' })).toBeFocused();

  const pending = page.getByRole('list', { name: 'Ожидающие подтверждения' });
  await expect(pending.getByRole('listitem')).toHaveCount(3);
  await expect(pending.locator('[data-approval-id="01900000-0000-7000-8000-0000000a0002"]')).toContainText('Решает другой участник');
  await expectNoSeriousA11yViolations(page, 'approvals list');

  // Фильтр «могу решить» уходит на сервер (decidable_by_me=true).
  // Состояние фильтра — в URL (навигация в transition): click + ожидание, а не check().
  await page.getByLabel('Только те, что я могу решить').click();
  await expect(page.getByLabel('Только те, что я могу решить')).toBeChecked();
  await expect(pending.getByRole('listitem')).toHaveCount(2);
  expect(api.requests.some((r) => r.operationId === 'listApprovals' && r.headers['x-workspace-id'] === ws.id)).toBe(true);
  await expect(page).toHaveURL(/mine=1/);

  // Решённые.
  await page.getByRole('tab', { name: 'Решённые' }).click();
  const decided = page.getByRole('list', { name: 'Решённые подтверждения' });
  await expect(decided.getByRole('listitem')).toHaveCount(1);
  await expect(decided).toContainText('Деплой в production');
  await expect(decided).toContainText('Борис Иванов');
  await page.getByRole('tab', { name: 'Ожидают решения' }).click();

  // Карточка плана: предмет, риск, срок, детали (как текст), run в AWAITING_APPROVAL.
  await pending.locator('[data-approval-id="01900000-0000-7000-8000-0000000a0001"]').getByRole('link', { name: /Открыть/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/approvals/01900000-0000-7000-8000-0000000a0001`);
  await expect(page.getByRole('heading', { level: 1, name: 'Подтверждение' })).toBeVisible();
  await expect(page.getByTestId('approval-summary')).toHaveText('План v1: 2 задачи (API, экспорт CSV) <b>не HTML</b>');
  await expect(page.getByTestId('approval-summary').locator('b')).toHaveCount(0);
  await expect(page.getByText('Риск: средний').first()).toBeVisible();
  await expect(page.getByText('Утверждение плана: задачи запуска не начнут выполняться до решения.')).toBeVisible();
  await expect(page.getByRole('figure').getByText('"plan_version": 1')).toBeVisible();
  await expect(page.getByTestId('approval-status')).toHaveText('Ожидает решения');
  await expect(page.getByText('Ожидает одобрения', { exact: true })).toBeVisible(); // статус run
  await expect(page.getByTestId('live-state')).toHaveAttribute('data-state', /open|connecting|reconnecting/);
  await expectNoSeriousA11yViolations(page, 'approval detail');
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await expectNoSeriousA11yViolations(page, 'approval detail (dark)');

  // Одобрение с подтверждением: диалог, затем POST с If-Match и Idempotency-Key.
  await page.getByRole('button', { name: 'Одобрить' }).click();
  const dialog = page.getByRole('dialog', { name: 'Одобрить действие?' });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Отменить решение нельзя');
  await expectNoSeriousA11yViolations(page, 'approve dialog');
  expect(decisions).toHaveLength(0); // до подтверждения ничего не отправлено
  await dialog.getByRole('button', { name: 'Да, одобрить' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId('approval-status')).toHaveText('Одобрено');
  expect(decisions).toHaveLength(1);
  expect(decisions[0]?.headers['if-match']).toBe('"v3"');
  expect(decisions[0]?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
  expect(decisions[0]?.body).toEqual({ decision: 'APPROVED' });
  // Переход run приходит по SSE (approval.decided + run.state_changed) — run продолжает выполнение.
  await expect(page.getByText('Выполняется', { exact: true })).toBeVisible();
  expect(run.status).toBe('RUNNING');
  await expect(page.getByText('Анна Петрова').first()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Одобрить' })).toHaveCount(0);
});

test('AC-13: requester cannot approve own merge (four-eyes); server four_eyes_violation and reject reason are handled', async ({ page, api }) => {
  const { ws, decisions, approvals } = seed(api);
  await login(page, api);

  // Merge, запрошенный самим пользователем: кнопок нет, объяснение.
  await page.goto(`/w/${ws.id}/approvals/01900000-0000-7000-8000-0000000a0002`);
  await expect(page.getByTestId('approval-summary')).toHaveText('Слияние ветки feature/export в main');
  await expect(page.getByTestId('cannot-decide')).toContainText('принцип четырёх глаз');
  await expect(page.getByRole('button', { name: 'Одобрить' })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page, 'approval four-eyes');

  // Миграция: сервер отклоняет решение (four_eyes_violation) — статус не меняется.
  await page.goto(`/w/${ws.id}/approvals/01900000-0000-7000-8000-0000000a0003`);
  await page.getByRole('button', { name: 'Одобрить' }).click();
  let dialog = page.getByRole('dialog', { name: 'Одобрить действие?' });
  // critical-риск: причина обязательна даже для одобрения.
  await dialog.getByRole('button', { name: 'Да, одобрить' }).click();
  await expect(dialog.getByText('Укажите причину.')).toBeVisible();
  await expect(dialog.getByLabel('Причина')).toBeFocused();
  expect(decisions).toHaveLength(0);
  await dialog.getByLabel('Причина').fill('Колонка не используется с v2.0');
  await dialog.getByRole('button', { name: 'Да, одобрить' }).click();
  await expect(dialog.getByRole('alert')).toHaveText('Это действие должен подтвердить другой участник.');
  expect(approvals.find((a) => a.id.endsWith('a0003'))?.status).toBe('PENDING');
  await dialog.getByRole('button', { name: 'Отмена' }).click();
  await expect(page.getByRole('button', { name: 'Одобрить' })).toBeFocused();

  // Отклонение плана: причина обязательна, затем REJECTED.
  await page.goto(`/w/${ws.id}/approvals/01900000-0000-7000-8000-0000000a0001`);
  await page.getByRole('button', { name: 'Отклонить' }).click();
  dialog = page.getByRole('dialog', { name: 'Отклонить действие?' });
  await dialog.getByRole('button', { name: 'Да, отклонить' }).click();
  await expect(dialog.getByText('Укажите причину.')).toBeVisible();
  await dialog.getByLabel('Причина').fill('План не покрывает миграцию данных');
  await dialog.getByRole('button', { name: 'Да, отклонить' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId('approval-status')).toHaveText('Отклонено');
  expect(decisions.at(-1)?.body).toEqual({ decision: 'REJECTED', reason: 'План не покрывает миграцию данных' });
  await expect(page.getByText('План не покрывает миграцию данных')).toBeVisible();
});

test('viewer sees approvals read-only', async ({ page, api }) => {
  const { ws } = seed(api, 'viewer');
  await login(page, api);
  await page.goto(`/w/${ws.id}/approvals/01900000-0000-7000-8000-0000000a0001`);
  await expect(page.getByTestId('approval-summary')).toBeVisible();
  await expect(page.getByText('Ваша роль может только просматривать подтверждения.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Одобрить' })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page, 'approval viewer');
});

test('approvals: empty and error states', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  let fail = true;
  api.route('GET', '/api/v1/approvals', 'listApprovals', () =>
    fail ? problem(500, 'internal_error') : { status: 200, body: { items: [], next_cursor: null } },
  );
  await login(page, api);
  await page.goto(`/w/${ws.id}/approvals`);
  await expect(page.getByTestId('error-state')).toContainText('Внутренняя ошибка сервера');
  fail = false;
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('heading', { name: 'Нет ожидающих подтверждений' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'approvals empty');
});

test('approvals: switching tabs and filters keeps keyboard focus on the control (query change is not a navigation)', async ({ page, api }) => {
  const { ws, run } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/approvals?run_id=${run.id}`);
  await expect(page.getByRole('heading', { level: 1, name: 'Одобрения' })).toBeFocused();
  const pendingTab = page.getByRole('tab', { name: 'Ожидают решения' });
  await pendingTab.focus();
  await page.keyboard.press('ArrowRight');
  const decidedTab = page.getByRole('tab', { name: 'Решённые' });
  await expect(page).toHaveURL(/tab=decided/);
  await expect(decidedTab).toHaveAttribute('aria-selected', 'true');
  await expect(decidedTab).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(page).toHaveURL(/tab=pending/);
  await expect(pendingTab).toBeFocused();
  await expect(page.getByRole('list', { name: 'Ожидающие подтверждения' }).getByRole('listitem')).toHaveCount(3);

  const mine = page.getByLabel('Только те, что я могу решить');
  await mine.focus();
  await page.keyboard.press('Space');
  await expect(page).toHaveURL(/mine=1/);
  await expect(mine).toBeChecked();
  await expect(mine).toBeFocused();
  await expect(page.getByRole('heading', { level: 1, name: 'Одобрения' })).not.toBeFocused();
});
