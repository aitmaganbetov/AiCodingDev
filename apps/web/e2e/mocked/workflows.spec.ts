import type { Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import type { MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/**
 * F5 «Процессы» (Workflows, план §3.4): запуски как процессы — режим, этап FSM, прогресс только по
 * подтверждённым задачам (getRun.task_counts: SUCCEEDED), длительность, причина блокировки, ожидающие
 * одобрения, переход в AI Mission Control, фильтр режима (`mode` listRuns) и справка конвейеров.
 * Прежнее поведение списка (фильтры статуса/проекта, пагинация, ссылки run) — runs.spec.ts.
 */
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

function seed(api: MockApi) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'developer');
  const gateway = api.addProject(ws.id, 'Платёжный шлюз');
  const audit = api.addRun(gateway, {
    mode: 'AUDIT',
    status: 'COMPLETED',
    objective: 'Аудит безопасности платёжного шлюза',
    created_at: '2026-11-07T08:00:00Z',
    started_at: '2026-11-07T08:00:00Z',
    finished_at: '2026-11-07T08:02:05Z',
  });
  api.addTask(audit, 'Анализ угроз', { agent_role: 'security', status: 'SUCCEEDED' });
  const blocked = api.addRun(gateway, {
    status: 'BLOCKED',
    blocked_reason: 'PROVIDER_UNAVAILABLE',
    objective: 'Обновить зависимости шлюза',
    created_at: '2026-11-08T07:00:00Z',
    started_at: '2026-11-08T07:00:05Z',
    pending_approvals: 0,
  });
  api.addTask(blocked, 'Обновление', { status: 'BLOCKED', blocked_reason: 'PROVIDER_UNAVAILABLE' });
  const running = api.addRun(gateway, { status: 'RUNNING', objective: 'Добавить экспорт отчётов в CSV', created_at: '2026-11-08T08:00:00Z' });
  api.addTask(running, 'Схема', { agent_role: 'database', status: 'SUCCEEDED' });
  api.addTask(running, 'Экспорт', { status: 'RUNNING' });
  api.addTask(running, 'Тесты', { agent_role: 'qa', status: 'PENDING' });
  return { ws, audit, blocked, running };
}

test('workflows: mode, FSM stage, verified progress, duration, blocked reason, Mission Control links, mode filter (light/dark axe)', async ({ page, api }) => {
  const { ws, audit, blocked, running } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Процессы', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Процессы' })).toBeFocused();
  const list = page.getByRole('list', { name: 'Список запусков' });
  await expect(list.locator(':scope > li')).toHaveCount(3);

  const run = list.locator(`[data-run-id="${running.id}"]`);
  await expect(run).toContainText('Разработка (DEVELOP)');
  await expect(run.getByTestId('workflow-stage')).toContainText('Этап 4 из 6: Выполняется');
  // Прогресс — только подтверждённые (SUCCEEDED) задачи: 1 из 3.
  await expect(run.getByTestId('workflow-progress')).toContainText('1 из 3');
  await expect(run.getByTestId('workflow-progress')).toHaveAttribute('data-ratio', String(1 / 3));
  await expect(run).toContainText('В работе: 1 · с ошибкой: 0 · заблокировано: 0');
  await expect(run.getByTestId('workflow-duration')).toContainText('(идёт)');
  await expect(run.getByRole('link', { name: /^AI Mission Control/ })).toHaveAttribute('href', `/w/${ws.id}/mission-control?run=${running.id}`);

  const stuck = list.locator(`[data-run-id="${blocked.id}"]`);
  await expect(stuck.getByTestId('workflow-blocked')).toContainText('Причина блокировки: Провайдер CLI недоступен');
  await expect(stuck.getByTestId('workflow-stage')).toContainText('Остановлен на этапе 4 из 6');

  const done = list.locator(`[data-run-id="${audit.id}"]`);
  await expect(done.getByTestId('workflow-duration')).toHaveText('Длительность: 2 мин 5 с');
  await expect(done.getByTestId('workflow-progress')).toContainText('1 из 1');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `workflows (${scheme})`);
  }

  // Фильтр режима: `mode` уходит на сервер, справка показывает конвейер выбранного режима.
  const mode = page.getByLabel('Режим');
  await mode.focus();
  await mode.selectOption('AUDIT');
  await expect(page).toHaveURL(`/w/${ws.id}/runs?mode=AUDIT`);
  await expect(mode).toBeFocused();
  await expect(list.locator(':scope > li')).toHaveCount(1);
  await expect(list.locator(`[data-run-id="${audit.id}"]`)).toBeVisible();
  const reference = page.getByTestId('pipelines-reference');
  await reference.getByText('Конвейер ролей режима «Аудит (AUDIT)» (справка)').click();
  await expect(reference).toContainText('planner → architect ‖ security ‖ qa ‖ performance');
  await expect(reference).not.toContainText('Разработка (DEVELOP)');
  await expectNoSeriousA11yViolations(page, 'workflows filtered');
  await page.reload();
  await expect(page.getByLabel('Режим')).toHaveValue('AUDIT');
});

test('workflows: 375/768/1366/1920 without horizontal overflow in both themes', async ({ page, api }) => {
  const { ws } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/runs`);
  await expect(page.getByRole('list', { name: 'Список запусков' }).locator(':scope > li')).toHaveCount(3);
  await expect(page.getByTestId('workflow-progress')).toHaveCount(3);
  for (const width of [375, 768, 1366, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const scheme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
      if (width === 1366 || width === 375) await shot(page, `workflows-${scheme}-${width}`);
    }
  }
});
