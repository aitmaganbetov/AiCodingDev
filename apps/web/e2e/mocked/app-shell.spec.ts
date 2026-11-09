import type { Page } from '@playwright/test';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

/**
 * F2 редизайна — оболочка «Neural Engineering Command Center» (план §3.1, §5.8, §5.9, §6.1):
 * sidebar (rail, сохранение), Command Palette (Ctrl+K), глобальный поиск (`GET /search`), чипы CLI
 * из `listProviders`, уведомления из потока workspace, правая контекстная панель, редиректы старых
 * путей, отсутствие горизонтального переполнения, reduced motion, axe в обеих темах.
 */

function installation(over: Partial<S['ProviderInstallation']> = {}): S['ProviderInstallation'] {
  return {
    installation_id: '01900000-0000-7000-b000-000000000011',
    host: 'worker-exec-1',
    detected_version: '0.42.0',
    compatibility: 'SUPPORTED',
    auth_status: 'AUTHENTICATED',
    availability: 'AVAILABLE',
    capabilities: {},
    last_error_category: null,
    checked_at: '2026-11-08T08:00:00Z',
    ...over,
  };
}

function providers(codex: Partial<S['ProviderInstallation']>, claude: Partial<S['ProviderInstallation']> | null): S['Provider'][] {
  const settings: S['ProviderSettings'] = { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, allowed_roles: [], settings_version: 1 };
  return [
    { provider_id: '01900000-0000-7000-b000-000000000001', code: 'codex', display_name: 'Codex CLI', enabled_globally: true, installations: [installation(codex)], workspace_settings: settings },
    {
      provider_id: '01900000-0000-7000-b000-000000000002',
      code: 'claude',
      display_name: 'Claude Code CLI',
      enabled_globally: true,
      installations: claude ? [installation({ installation_id: '01900000-0000-7000-b000-000000000012', ...claude })] : [],
      workspace_settings: settings,
    },
  ];
}

function seed(api: MockApi) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const p = api.addProject(ws.id, 'Платёжный шлюз', 'Сервис платежей');
  const active = api.addRun(p, { objective: 'Добавить экспорт отчётов в CSV', status: 'RUNNING' });
  api.addTask(active, 'Реализовать экспорт', { status: 'RUNNING', agent_role: 'developer' });
  api.addTask(active, 'Ревью экспорта', { status: 'PENDING', agent_role: 'reviewer' });
  const done = api.addRun(p, { objective: 'Обновить зависимости', status: 'COMPLETED', finished_at: '2026-11-08T08:30:00Z' });
  return { ws, p, active, done };
}

const sidebar = (page: Page) => page.getByTestId('sidebar');

test('sidebar: 14 sections in 4 groups, collapse to an icon rail (tooltip, accessible names), state persists across reloads', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws } = seed(api);
  await login(page, api);
  const nav = page.getByRole('navigation', { name: 'Основная навигация' });
  for (const group of ['Обзор', 'Исполнение', 'Код и качество', 'Платформа']) await expect(nav.getByRole('list', { name: group })).toBeVisible();
  await expect(nav.getByRole('listitem')).toHaveCount(15); // 14 разделов + «Рабочие пространства»
  await expect(nav.getByRole('link', { name: 'CLI-провайдеры' })).toHaveCount(0); // подраздел Настроек (D-3)
  const width = () => sidebar(page).evaluate((el) => el.getBoundingClientRect().width);
  expect(await width()).toBeGreaterThan(200);

  const toggle = page.getByRole('button', { name: 'Свернуть навигацию' });
  await toggle.click();
  await expect(page.getByRole('button', { name: 'Развернуть навигацию' })).toBeFocused();
  await expect(page.locator('[data-sidebar]')).toHaveAttribute('data-sidebar', 'rail');
  await expect.poll(width).toBeLessThan(80);
  // В rail подпись — доступное имя ссылки и подсказка при фокусе.
  const missionLink = nav.getByRole('link', { name: 'AI Mission Control' });
  await expect(missionLink).toBeVisible();
  await missionLink.focus();
  await expect(page.getByRole('tooltip', { name: 'AI Mission Control' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'sidebar rail');

  await page.reload();
  await expect(page.getByRole('button', { name: 'Развернуть навигацию' })).toBeVisible();
  expect(await width()).toBeLessThan(80);
  await page.getByRole('button', { name: 'Развернуть навигацию' }).click();
  await expect.poll(width).toBeGreaterThan(200);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Свернуть навигацию' })).toBeVisible();

  // Без явного выбора на 768–1279 px — rail; явный выбор пользователя сохраняется.
  await page.evaluate(() => window.localStorage.removeItem('aicd.layout.shell.sidebar'));
  await page.setViewportSize({ width: 1024, height: 800 });
  await page.goto(`/w/${ws.id}/dashboard`);
  await expect.poll(width).toBeLessThan(80);
});

test('command palette: Ctrl+K, keyboard-only navigation, theme switch, recent runs/projects and search results; Esc returns focus', async ({ page, api }) => {
  const { ws, p, active } = seed(api);
  api.route('GET', '/api/v1/search', 'searchWorkspace', ({ url }) => ({
    status: 200,
    body: {
      query: url.searchParams.get('q') ?? '',
      types: ['project', 'run', 'task', 'finding', 'approval', 'documentation_source', 'chat_message'],
      skipped_types: [],
      truncated: false,
      items: [
        {
          type: 'run',
          id: active.id,
          title: active.objective,
          snippet: 'Добавить экспорт отчётов в CSV',
          score: 0.9,
          created_at: '2026-11-08T08:00:00Z',
          project_id: p.id,
          run_id: active.id,
          task_id: null,
          api_path: `/api/v1/runs/${active.id}`,
        },
      ],
    } satisfies S['SearchResults'],
  }));
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: /^Обзор/ })).toBeFocused();

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog', { name: 'Командная палитра' });
  await expect(palette).toBeVisible();
  const input = palette.getByRole('combobox');
  await expect(input).toBeFocused();
  await expect(palette.getByRole('option', { name: /Добавить экспорт отчётов в CSV/ })).toBeVisible(); // недавние запуски
  await expect(palette.getByRole('option', { name: 'Платёжный шлюз' })).toBeVisible(); // проекты
  await expectNoSeriousA11yViolations(page, 'command palette');

  // Только клавиатура: фильтр → стрелки → Enter.
  await page.keyboard.type('Гейты');
  await expect(palette.getByRole('option', { name: /Перейти: Гейты качества/ })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Enter');
  await expect(palette).toHaveCount(0);
  await expect(page).toHaveURL(`/w/${ws.id}/quality`);
  await expect(page.getByRole('heading', { level: 1, name: 'Гейты качества' })).toBeFocused();

  // Тема из палитры.
  await page.keyboard.press('Control+k');
  await expect(input).toBeFocused();
  await page.keyboard.type('Тёмная');
  await expect(palette.getByRole('option', { name: /Тема: Тёмная/ })).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('Enter');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

  // Поиск по workspace внутри палитры → открыть найденный run.
  await page.keyboard.press('Meta+k');
  await expect(input).toBeFocused();
  await page.keyboard.type('экспорт');
  const found = palette.getByRole('group', { name: 'Поиск по рабочему пространству' }).getByRole('option', { name: /Добавить экспорт отчётов в CSV/ });
  await expect(found).toBeVisible();
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'searchWorkspace').length).toBeGreaterThan(0);
  await found.click();
  // Экран run с F4 — AI Mission Control (старый путь run перенаправляется туда с тем же run).
  await expect(page).toHaveURL(new RegExp(`/w/${ws.id}/mission-control\\?run=${active.id}`));
  // Ленивый экран переводит фокус на свой h1, когда смонтируется, — дождаться, прежде чем фокусировать кнопку.
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeFocused();

  // Esc закрывает и возвращает фокус туда, где он был.
  const trigger = page.getByRole('button', { name: 'Командная палитра (Ctrl+K)' });
  await trigger.focus();
  await page.keyboard.press('Enter');
  await expect(palette).toBeVisible();
  await expect(input).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(palette).toHaveCount(0);
  await expect(trigger).toBeFocused();
  const search = api.requests.find((r) => r.operationId === 'searchWorkspace');
  expect(search?.headers['x-workspace-id']).toBe(ws.id);
});

test('global search: min length, grouped results, ↑/↓ + Enter opens the result, rate limit and skipped types are explained', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws, p, active } = seed(api);
  let limited = false;
  api.route('GET', '/api/v1/search', 'searchWorkspace', ({ url }) => {
    if (limited) return { ...problem(429, 'rate_limited'), headers: { 'Retry-After': '30' } };
    const q = url.searchParams.get('q') ?? '';
    const items: S['SearchResult'][] = [
      { type: 'project', id: p.id, title: p.name, snippet: null, score: 1, created_at: p.created_at, project_id: p.id, run_id: null, task_id: null, api_path: `/api/v1/projects/${p.id}` },
      {
        type: 'run',
        id: active.id,
        title: active.objective,
        snippet: `…${q}…`,
        score: 0.5,
        created_at: active.created_at,
        project_id: p.id,
        run_id: active.id,
        task_id: null,
        api_path: `/api/v1/runs/${active.id}`,
      },
    ];
    return { status: 200, body: { query: q, types: ['project', 'run'], skipped_types: ['finding'], truncated: true, items } };
  });
  await login(page, api);
  const box = page.getByRole('combobox', { name: 'Поиск по рабочему пространству' });
  await box.click();
  await box.fill('э');
  await expect(page.getByTestId('search-results')).toContainText('Введите не менее 2 символов.');
  expect(api.requests.some((r) => r.operationId === 'searchWorkspace')).toBe(false);

  await box.fill('экспорт');
  const list = page.getByRole('listbox', { name: 'Результаты поиска' });
  await expect(list.getByRole('group', { name: 'Проекты' }).getByRole('option')).toHaveCount(1);
  await expect(list.getByRole('group', { name: 'Запуски' }).getByRole('option')).toHaveCount(1);
  await expect(box).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByTestId('search-results')).toContainText('Не искали (нет права): Находки.');
  await expect(page.getByTestId('search-results')).toContainText('Показаны лучшие совпадения');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `global search (${scheme})`);
  }
  await expect(list.getByRole('option').first()).toHaveAttribute('aria-selected', 'true');
  await page.keyboard.press('ArrowDown');
  const second = list.getByRole('option', { name: /Добавить экспорт отчётов в CSV/ });
  await expect(second).toHaveAttribute('aria-selected', 'true');
  await expect(box).toHaveAttribute('aria-activedescendant', (await second.getAttribute('id')) ?? '');
  await page.keyboard.press('Enter');
  // Результат «запуск» ведёт на старый путь run, который сразу перенаправляется в Mission Control:
  // промежуточный URL под нагрузкой может не попасть в опрос — проверяется итоговый экран.
  await expect(page).toHaveURL(new RegExp(`/w/${ws.id}/mission-control\\?run=${active.id}`));
  await expect(box).toHaveValue('');
  // Новый экран переводит фокус на свой h1 (после загрузки данных) — дождаться, прежде чем снова искать.
  await expect(page.getByRole('heading', { level: 1 })).toBeFocused();
  const sent = api.requests.filter((r) => r.operationId === 'searchWorkspace').at(-1);
  expect(sent?.headers['x-workspace-id']).toBe(ws.id);

  // Esc закрывает список; 429 — понятное сообщение.
  limited = true;
  await box.fill('миграция');
  await expect(page.getByTestId('search-results')).toContainText('Слишком много запросов поиска');
  await page.keyboard.press('Escape');
  await expect(box).toHaveAttribute('aria-expanded', 'false');
});

test('CLI status chips: real provider data (ready / AUTH_REQUIRED / unknown / not detected), tooltip with version and last check, link to Settings', async ({ page, api }) => {
  await page.setViewportSize({ width: 1920, height: 900 });
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  let items = providers({ auth_status: 'AUTH_REQUIRED', last_error_category: 'AUTH_REQUIRED' }, { auth_status: 'UNKNOWN', checked_at: null, detected_version: null });
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({ status: 200, body: { items } }));
  await login(page, api);
  const chips = page.getByRole('list', { name: 'Состояние CLI-провайдеров' });
  const codex = chips.getByRole('link', { name: /^Codex CLI/ });
  const claude = chips.getByRole('link', { name: /^Claude Code CLI/ });
  await expect(codex).toHaveText('Codex CLI · требуется вход');
  await expect(codex).toHaveAttribute('data-state', 'auth_required');
  await expect(claude).toHaveText('Claude Code CLI · неизвестно');
  await expect(claude).toHaveAttribute('data-state', 'unknown');
  await codex.focus();
  const tip = page.getByRole('tooltip');
  await expect(tip).toContainText('worker-exec-1: версия 0.42.0, Поддерживается');
  await expect(tip).toContainText('последняя ошибка AUTH_REQUIRED');
  await expect(tip).toContainText('Проверено');
  await expect(tip).toContainText('Квоты и остаток подписки CLI не сообщают');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `cli chips (${scheme})`);
  }
  await page.keyboard.press('Escape');

  // Готов / не обнаружен (нет установок) — после обновления данных (опрос/перезагрузка).
  items = providers({}, null);
  await page.reload();
  await expect(codex).toHaveText('Codex CLI · готов');
  await expect(claude).toHaveText('Claude Code CLI · не обнаружен');
  // < 1536 px — компактный чип (код + иконка); полное состояние — в доступном имени и подсказке.
  await page.setViewportSize({ width: 1366, height: 900 });
  await expect(codex).toHaveAccessibleName('Codex CLI · готов');
  await expect(codex.locator('[aria-hidden="true"]', { hasText: 'CX' })).toBeVisible();
  await expectNoHorizontalScroll(page);
  await codex.click();
  await expect(page).toHaveURL(`/w/${ws.id}/settings/providers`);
  await expect(page.getByRole('heading', { level: 1, name: 'CLI-провайдеры' })).toBeFocused();
});

test('notifications (B8): server list and unread badge, live arrival via workspace SSE, mark one / all read (Idempotency-Key), survives reload; pending approvals link', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws, p, active, done } = seed(api);
  const approvalId = '01900000-0000-7000-8000-0000000a0001';
  api.route('GET', '/api/v1/approvals', 'listApprovals', ({ url }) => {
    const mine = url.searchParams.get('decidable_by_me') === 'true';
    const items: S['Approval'][] = mine
      ? [
          {
            id: approvalId,
            workspace_id: ws.id,
            run_id: active.id,
            action_type: 'MERGE',
            status: 'PENDING',
            risk_level: 'medium',
            summary: 'Слияние ветки feature/export',
            requested_by: { actor_type: 'agent', user: null, role: 'developer' },
            can_decide: true,
            created_at: '2026-11-08T09:00:00Z',
            version: 0,
          },
        ]
      : [];
    return { status: 200, body: { items, next_cursor: null } };
  });
  // История до входа: одно прочитанное и одно непрочитанное уведомление (без кадров потока).
  api.addNotification(
    ws.id,
    {
      kind: 'run.completed', severity: 'success', title: 'Запуск завершён', body: 'Обновить зависимости',
      project_id: p.id, run_id: done.id, resource_type: 'run', resource_id: done.id, data: { final_status: 'COMPLETED' },
      read: true, read_at: '2026-11-08T09:05:00Z',
    },
    false,
  ); // fmt: skip
  const approvalNote = api.addNotification(
    ws.id,
    {
      kind: 'approval.requested', severity: 'warning', title: 'Требуется ваше решение: MERGE', body: 'Слияние ветки feature/export',
      project_id: p.id, run_id: active.id, resource_type: 'approval', resource_id: approvalId, data: { action_type: 'MERGE' },
    },
    false,
  ); // fmt: skip
  // Устаревший курсор F2 в localStorage игнорируется.
  await page.addInitScript((key) => window.localStorage.setItem(key, '2030-01-01T00:00:00Z'), `aicd.notifications.read.${ws.id}`);
  await login(page, api);
  const bell = page.getByTestId('notifications-trigger');
  await expect(bell).toHaveAccessibleName('Уведомления: 1 непрочитанное');
  await expect(page.getByTestId('notifications-count')).toHaveText('1');
  await expect(page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: /Одобрения/ })).toContainText('1');

  // Новое уведомление приходит live: кадр notification.created потока workspace.
  const failed = api.addNotification(ws.id, {
    kind: 'run.failed', severity: 'error', title: 'Запуск завершился с ошибкой', body: 'Добавить экспорт отчётов в CSV',
    project_id: p.id, run_id: active.id, resource_type: 'run', resource_id: active.id, data: { final_status: 'FAILED' },
  }); // fmt: skip
  await expect(bell).toHaveAccessibleName('Уведомления: 2 непрочитанных');
  const connections = api.workspaceStreamConnections.filter((c) => c.workspaceId === ws.id);
  expect(connections.length).toBeGreaterThan(0);

  await bell.click();
  const panel = page.getByRole('dialog', { name: 'Уведомления' });
  const items = panel.getByTestId('notification-item');
  await expect(items).toHaveCount(3);
  await expect(items.first()).toContainText('Запуск завершился с ошибкой');
  await expect(items.first()).toHaveAttribute('data-read', 'false');
  await expect(items.nth(2)).toHaveAttribute('data-read', 'true');
  await expect(panel.getByTestId('notifications-approvals')).toContainText('Ожидают вашего решения: 1');
  await expect(panel).not.toContainText('только в этом браузере');
  await expect(panel.getByRole('link', { name: /Требуется ваше решение/ })).toHaveAttribute('href', `/w/${ws.id}/approvals/${approvalId}`);
  await expect(panel.getByRole('link', { name: /Запуск завершился с ошибкой/ })).toHaveAttribute('href', `/w/${ws.id}/projects/${p.id}/runs/${active.id}`);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `notifications (${scheme})`);
  }

  // Отметить одно: POST /notifications/mark-read по id с Idempotency-Key.
  await panel.getByRole('button', { name: `Отметить прочитанным: ${approvalNote.title}` }).click();
  await expect(bell).toHaveAccessibleName('Уведомления: 1 непрочитанное');
  await expect(items.nth(1)).toHaveAttribute('data-read', 'true');
  const marks = () => api.requests.filter((r) => r.operationId === 'markNotificationsRead');
  expect(marks()[0]?.body).toEqual({ notification_ids: [approvalNote.id] });
  expect(marks()[0]?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);

  // Отметить все: «до самого нового показанного» (up_to).
  await panel.getByRole('button', { name: 'Отметить все прочитанными' }).click();
  await expect(bell).toHaveAccessibleName('Уведомления');
  await expect(page.getByTestId('notifications-count')).toHaveCount(0);
  expect(marks()[1]?.body).toEqual({ up_to: failed.id });
  expect(marks()[1]?.headers['idempotency-key']).not.toBe(marks()[0]?.headers['idempotency-key']);
  await expect(panel.getByRole('button', { name: 'Отметить все прочитанными' })).toBeDisabled();
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(bell).toBeFocused();

  // Отметка «прочитано» хранится на сервере: после перезагрузки непрочитанных нет.
  await page.reload();
  await expect(page.getByRole('navigation', { name: 'Основная навигация' })).toBeVisible();
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'getNotificationUnreadCount').length).toBeGreaterThan(1);
  await expect(bell).toHaveAccessibleName('Уведомления');
  for (const r of api.requests.filter((x) => x.operationId === 'listNotifications' || x.operationId === 'getNotificationUnreadCount')) {
    expect(r.headers['x-workspace-id']).toBe(ws.id);
  }
});

test('context panel on Mission Control: default "no context", selected run summary, toggle + keyboard resize, persisted after reload; split view', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws, p, active } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'AI Mission Control' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeFocused();
  // Только активные runs (фильтр статусов в запросе), без завершённого.
  const runs = page.getByRole('list', { name: 'Активные запуски' });
  await expect(runs.getByRole('listitem')).toHaveCount(1);

  const panel = page.getByTestId('context-panel');
  await expect(panel).toHaveAttribute('data-mode', 'push');
  await expect(panel.getByTestId('context-empty')).toContainText('Нет выбранного контекста');
  const toggle = page.getByRole('button', { name: 'Скрыть контекстную панель' });
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');

  await runs.getByRole('button', { name: /Добавить экспорт отчётов в CSV/ }).click();
  // F4: выбранный run открывается в трёх панелях; сводка run — в контекстной панели (Agent Inspector).
  await expect(page).toHaveURL(`/w/${ws.id}/mission-control?run=${active.id}`);
  await expect(panel.getByTestId('mc-run-inspector')).toContainText('Добавить экспорт отчётов в CSV');
  await expect(panel.getByRole('link', { name: 'Качество' })).toHaveAttribute('href', `/w/${ws.id}/quality/runs/${active.id}`);
  await expect(panel.getByRole('link', { name: 'К списку запусков' })).toHaveAttribute('href', `/w/${ws.id}/projects/${p.id}/runs`);
  await expect(page.getByTestId('run-graph-canvas').locator('[data-task-id]')).toHaveCount(2);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `mission control + context panel (${scheme})`);
  }

  // Ширина — с клавиатуры (WCAG 2.1.1, 2.5.7), сохраняется после перезагрузки; кнопка сброса.
  const sep = page.getByRole('separator', { name: 'Изменить ширину контекстной панели' });
  await expect(sep).toHaveAttribute('aria-valuenow', '340');
  await sep.focus();
  for (let i = 0; i < 3; i++) await page.keyboard.press('ArrowLeft');
  await expect(sep).toHaveAttribute('aria-valuenow', '388');
  expect(Math.round(await panel.evaluate((el) => el.getBoundingClientRect().width))).toBe(388);
  await page.keyboard.press('End');
  await expect(sep).toHaveAttribute('aria-valuenow', '560');
  await page.reload();
  await expect(sep).toHaveAttribute('aria-valuenow', '560');
  // Без перетаскивания и без клавиатуры (2.5.7): меню ширины — «Уже», затем сброс.
  await panel.getByRole('button', { name: 'Ширина контекстной панели' }).click();
  await page.getByRole('menuitem', { name: 'Уже' }).click();
  await expect(sep).toHaveAttribute('aria-valuenow', '496');
  await panel.getByRole('button', { name: 'Ширина контекстной панели' }).click();
  await page.getByRole('menuitem', { name: 'Сбросить ширину панели' }).click();
  await expect(sep).toHaveAttribute('aria-valuenow', '340');

  // Скрыть — сохраняется по экрану; кнопка возвращает панель.
  await toggle.click();
  await expect(panel).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeVisible();
  await expect(panel).toHaveCount(0);
  await page.getByRole('button', { name: 'Показать контекстную панель' }).click();
  await expect(panel).toBeVisible();
  await expect(page.getByRole('heading', { level: 2, name: 'Запуск', exact: true })).toBeFocused();

  // Split view экрана (Resizable): разделитель с клавиатуры, размер сохраняется.
  const split = page.getByRole('separator', { name: 'Изменить размер: Навигатор процесса / Live Agent Chat' });
  const before = Number(await split.getAttribute('aria-valuenow'));
  await split.focus();
  for (let i = 0; i < 3; i++) await page.keyboard.press('ArrowRight');
  await expect.poll(async () => Number(await split.getAttribute('aria-valuenow'))).toBeGreaterThan(before + 5);
  const after = Math.round(Number(await split.getAttribute('aria-valuenow')));
  await page.reload();
  await expect.poll(async () => Math.round(Number(await split.getAttribute('aria-valuenow')))).toBe(after);

  // Экран без контекстной панели — нет ни панели, ни кнопки.
  await page.goto(`/w/${ws.id}/projects`);
  await expect(page.getByRole('heading', { level: 1, name: /^Проекты/ })).toBeVisible();
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole('button', { name: /контекстную панель/ })).toHaveCount(0);
});

test('context panel overlays on narrow screens: Escape closes and returns focus to the toggle', async ({ page, api }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  const { ws } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/mission-control`);
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeVisible();
  const panel = page.getByTestId('context-panel');
  await expect(panel).toHaveCount(0); // наложение по умолчанию закрыто
  const show = page.getByRole('button', { name: 'Показать контекстную панель' });
  await show.click();
  await expect(panel).toHaveAttribute('data-mode', 'overlay');
  await expect(page.getByRole('heading', { level: 2, name: 'Запуск', exact: true })).toBeFocused();
  await expectNoHorizontalScroll(page);
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Показать контекстную панель' })).toBeFocused();
});

test('old URLs keep working: providers/audit → Settings sections (query preserved), mission-control?run= resolves a run by id; placeholders link to existing screens', async ({
  page,
  api,
}) => {
  const { ws, p, active } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/providers?probe=1#codex`);
  await expect(page).toHaveURL(`/w/${ws.id}/settings/providers?probe=1#codex`);
  await expect(page.getByRole('heading', { level: 1, name: 'CLI-провайдеры' })).toBeVisible();

  // F4: `?run=` — сам экран run в Mission Control; прежний путь run ведёт сюда же.
  await page.goto(`/w/${ws.id}/projects/${p.id}/runs/${active.id}`);
  await expect(page).toHaveURL(`/w/${ws.id}/mission-control?run=${active.id}&view=system`);
  await expect(page.getByTestId('run-status')).toHaveText('Выполняется');
  await page.goto(`/w/${ws.id}/mission-control?run=01900000-0000-7000-8000-00000000dead`);
  await expect(page.getByTestId('error-state')).toContainText('Запуск не найден');

  // F8: Code Intelligence и F7: Центр безопасности — экраны (не заглушки).
  await page.goto(`/w/${ws.id}/code`);
  await expect(page.getByRole('heading', { level: 1, name: 'Code Intelligence' })).toBeVisible();
  await expect(page.getByTestId('area-placeholder')).toHaveCount(0);
  await page.goto(`/w/${ws.id}/security`);
  await expect(page.getByRole('heading', { level: 1, name: 'Центр безопасности' })).toBeVisible();
  await expect(page.getByTestId('area-placeholder')).toHaveCount(0);
});

test('no horizontal page overflow at 375/768/1366/1920 (shell + Mission Control + settings), both themes; mobile drawer at 375', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа с очень длинным названием рабочего пространства', 'alpha', 'owner');
  const p = api.addProject(ws.id, 'Проект с очень длинным названием, которое должно переноситься в переключателе', 'Описание');
  const run = api.addRun(p, { objective: 'Очень длинная цель запуска '.repeat(6), status: 'RUNNING' });
  api.addTask(run, 'Задача с длинным названием '.repeat(4), { status: 'RUNNING' });
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({
    status: 200,
    body: { items: providers({ auth_status: 'AUTH_REQUIRED' }, { availability: 'RATE_LIMITED' }) },
  }));
  await login(page, api);
  for (const width of [375, 768, 1366, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ['dashboard', 'mission-control', `projects/${p.id}/runs/${run.id}`]) {
      await page.goto(`/w/${ws.id}/${path}`);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      // Run открывается в Mission Control: проверяем после того, как панели получили данные.
      if (path.startsWith('projects/')) await expect(page.getByTestId('mc-layout')).toBeVisible();
      for (const scheme of ['dark', 'light'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoHorizontalScroll(page);
      }
    }
  }
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto(`/w/${ws.id}/dashboard`);
  await expect(page.getByTestId('sidebar')).toHaveCount(0);
  await page.getByRole('button', { name: 'Открыть меню навигации' }).click();
  const drawer = page.getByRole('dialog', { name: 'Навигация' });
  await expect(drawer.getByRole('link', { name: /Codex CLI · требуется вход/ })).toBeVisible();
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'mobile drawer 375');
  const mc = drawer.getByRole('link', { name: 'AI Mission Control' });
  await tabTo(page, mc, { max: 40 });
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(`/w/${ws.id}/mission-control`);
  await expect(drawer).toHaveCount(0);
  // Поиск на узком экране — через палитру (кнопка-иконка).
  await page.getByRole('button', { name: 'Поиск и команды' }).click();
  await expect(page.getByRole('dialog', { name: 'Командная палитра' })).toBeVisible();
  await expectNoHorizontalScroll(page);
});

test('reduced motion: page transition and sidebar collapse run without animation; shell axe in light and dark', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const { ws } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Проекты', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: /^Проекты/ })).toBeVisible();
  const running = () => page.evaluate(() => document.getAnimations().filter((a) => Number(a.effect?.getComputedTiming().duration ?? 0) > 1).length);
  expect(await running()).toBe(0);
  const transition = await page.getByTestId('sidebar').evaluate((el) => getComputedStyle(el).transitionDuration);
  expect(parseFloat(transition)).toBeLessThanOrEqual(0.00001);

  // Без reduced motion переход экрана анимируется (только opacity/transform, ≤ 250 мс).
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto(`/w/${ws.id}/dashboard`);
  await expect(page.getByRole('heading', { level: 1, name: /^Обзор/ })).toBeVisible();
  await page.evaluate(() => {
    const log: number[] = [];
    (window as unknown as { __anim: number[] }).__anim = log;
    const original = Element.prototype.animate;
    Element.prototype.animate = function (this: Element, ...args: Parameters<Element['animate']>) {
      const a = original.apply(this, args);
      log.push(Number(a.effect?.getComputedTiming().duration ?? 0));
      return a;
    };
  });
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Проекты', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __anim: number[] }).__anim)).toContainEqual(180);

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `shell (${scheme})`);
  }
});
