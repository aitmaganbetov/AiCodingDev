import { expect, test, type Page, type Request } from '@playwright/test';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import { loginLive, missionControl, requireStack, runBase, stack, startDevelopRun, type LiveFixture, type LiveProject } from './support';

/**
 * P7-14: сквозные сценарии Mission Control против настоящего стенда (ТЗ §18 этап 5).
 * AC-05 — межагентные сообщения и handoff появляются в Live Agent Chat в реальном времени, граф — live;
 * AC-06 — поручения: команда PAUSE/RESUME (доставлено → подтверждено → выполнено, run реагирует) и
 *         CHANGE_REQUEST с impact preview → подтверждение → checkpoint → новая plan_version → доставлено →
 *         подтверждено агентами (directive_acks итога) → выполнено (задачи замены прошли проверки);
 * AC-08 — разрыв SSE на стороне сервера посреди run: переподключение с Last-Event-ID, без дублей и пропусков.
 * Исполнитель задач — Fake CLI (сценарий `agent`: developer → REVIEW_REQUEST → независимый reviewer).
 * С F4 все сценарии идут на одном экране — AI Mission Control: лента «Беседа агентов», граф навигатора,
 * поручения из User Command Center, журнал «Системные события»; Task Detail — прежний экран.
 */
requireStack();
test.describe.configure({ mode: 'serial' });
test.setTimeout(180_000);

let fx: LiveFixture;

test.beforeAll(async () => {
  fx = await stack.fixture();
});

test.afterEach(async () => {
  // Следующий сценарий начинает со штатного стенда (API и воркер исполнения запущены).
  await stack.startApi();
  await stack.startExec();
});

function runStatus(page: Page) {
  return page.getByTestId('run-status');
}

/** Наблюдатель за data-status узлов графа: последовательность статусов каждой задачи (без перезагрузки). */
async function watchGraphStatuses(page: Page): Promise<void> {
  await page.evaluate(() => {
    const seen: Record<string, string[]> = {};
    const record = (el: Element) => {
      const id = el.getAttribute('data-task-id');
      const status = el.getAttribute('data-status');
      if (!id || !status) return;
      const list = (seen[id] ??= []);
      if (list.at(-1) !== status) list.push(status);
    };
    document.querySelectorAll('[data-testid="run-graph-canvas"] [data-task-id]').forEach(record);
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.type === 'attributes' && m.target instanceof Element) record(m.target);
        m.addedNodes.forEach((n) => {
          if (n instanceof Element) {
            if (n.matches('[data-task-id]')) record(n);
            n.querySelectorAll('[data-task-id]').forEach(record);
          }
        });
      }
    }).observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ['data-status'] });
    (window as unknown as { __graphStatuses: typeof seen }).__graphStatuses = seen;
  });
}

async function graphStatuses(page: Page): Promise<Record<string, string[]>> {
  return page.evaluate(() => (window as unknown as { __graphStatuses: Record<string, string[]> }).__graphStatuses);
}

test('AC-05: DEVELOP run on Fake CLI — agent messages and handoffs stream into Live Agent Chat; graph statuses progress live', async ({
  page,
  context,
}) => {
  const project: LiveProject = await stack.project('AC-05');
  await stack.stopExec(); // задачи станут READY и подождут, пока открыты чат и граф
  await loginLive(page, fx);
  const runId = await startDevelopRun(page, project, 'Добавить экспорт отчётов в CSV');
  const base = runBase(project, runId);
  await expect(runStatus(page)).toHaveText('Выполняется', { timeout: 60_000 });

  // Лента и граф одного run открыты одновременно (две вкладки Mission Control), до начала исполнения.
  await page.getByRole('tab', { name: 'Беседа агентов' }).click();
  await expect(page).toHaveURL(new RegExp(`/mission-control\\?run=${runId}`));
  await expect(page.getByRole('heading', { level: 2, name: 'Live Agent Chat' })).toBeVisible();
  const items = page.getByTestId('chat-timeline').getByTestId('chat-item');
  await expect(items.filter({ hasText: 'Запрос ревью' })).toHaveCount(0);
  await expect(page.getByTestId('stream-state')).toHaveAttribute('data-state', 'open', { timeout: 30_000 });

  const graph = await context.newPage();
  await graph.goto(missionControl(project, runId));
  const canvas = graph.getByTestId('run-graph-canvas');
  await expect(canvas.locator('[data-task-id]')).toHaveCount(2, { timeout: 30_000 });
  await expect(canvas.locator('[data-task-id][data-status="SUCCEEDED"]')).toHaveCount(0);
  await watchGraphStatuses(graph);

  // Task Detail → «События и логи» (только вывод CLI) открыт до исполнения: живой вывод Fake CLI приходит
  // по SSE (worker-exec → outbox `cli.output` → run_event_log → SSE) без перезагрузки.
  const taskId = await canvas.locator('[data-task-id]').first().getAttribute('data-task-id');
  const logs = await context.newPage();
  await logs.goto(`${base}/tasks/${taskId}?tab=events`);
  await logs.getByRole('checkbox', { name: 'Только вывод CLI' }).check();
  const cliItems = logs.getByTestId('task-events').getByRole('listitem');
  await logs.evaluate(() => ((window as unknown as { __noReload: boolean }).__noReload = true));

  // Маркер окна: лента обновляется без перезагрузки страницы.
  await page.evaluate(() => ((window as unknown as { __noReload: boolean }).__noReload = true));
  await stack.startExec();

  // Handoff developer → reviewer (ACP REVIEW_REQUEST) появляется в ленте всех агентов.
  const review = items.filter({ hasText: 'Запрос ревью' });
  await expect(review.first()).toBeVisible({ timeout: 90_000 });
  await expect(review.first().getByRole('heading')).toHaveText(/^Разработчик · (Codex|Claude) → Ревьюер$/);
  await expect(review.first().locator('time')).toHaveAttribute('datetime', /^\d{4}-\d{2}-\d{2}T/);
  // Итог независимого ревью (reviewer → оркестратор) — тот же поток, после handoff.
  await expect(items.filter({ hasText: 'Задача завершена' }).filter({ has: page.getByRole('heading', { name: /^Ревьюер/ }) }).first()).toBeVisible({
    timeout: 90_000,
  });
  expect(await page.evaluate(() => (window as unknown as { __noReload?: boolean }).__noReload)).toBe(true);
  const shots = process.env.AICD_E2E_SCREENSHOTS_DIR;
  if (shots) {
    for (const [width, scheme] of [
      [1920, 'dark'],
      [1366, 'light'],
    ] as const) {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme: scheme });
      await page.screenshot({ path: `${shots}/live-mc-${width}-${scheme}.png` });
    }
    await page.setViewportSize({ width: 1280, height: 720 });
  }
  // AC-05: только ACP-сообщения/события — без скрытых рассуждений; каждое сообщение один раз.
  const ids = await items.evaluateAll((els) => els.map((el) => el.getAttribute('data-item-id')));
  expect(new Set(ids).size).toBe(ids.length);
  await expectNoSeriousA11yViolations(page, 'live chat (real stack)');

  // Отправитель и получатель: в потоке роли «Ревьюер» есть входящий запрос ревью.
  await page.getByRole('button', { name: 'Фильтры', exact: true }).click();
  await page.getByRole('combobox', { name: 'Поток', exact: true }).selectOption({ label: 'Роль' });
  await page.getByRole('combobox', { name: 'Роль', exact: true }).selectOption({ label: 'Ревьюер' });
  await expect(page).toHaveURL(/stream=role&role=reviewer/);
  await expect(items.filter({ hasText: 'Запрос ревью' }).first()).toBeVisible();

  // Граф: статусы задач менялись на глазах (live), итог — обе задачи SUCCEEDED, run завершён.
  await expect(canvas.locator('[data-task-id][data-status="SUCCEEDED"]')).toHaveCount(2, { timeout: 90_000 });
  const history = await graphStatuses(graph);
  const sequences = Object.values(history);
  expect(sequences).toHaveLength(2);
  for (const seq of sequences) {
    expect(seq.at(-1)).toBe('SUCCEEDED');
    expect(seq.length, `live transitions: ${seq.join(' → ')}`).toBeGreaterThan(1);
  }
  await expect(runStatus(graph)).toHaveText('Завершён', { timeout: 90_000 });
  await graph.close();

  // Живой вывод CLI задачи: первая строка Fake CLI, событие завершения (`exit 0`), порядок по seq, без перезагрузки.
  await expect(cliItems.filter({ hasText: 'Получена задача' }).first()).toBeVisible({ timeout: 30_000 });
  await expect(cliItems.filter({ hasText: 'exit 0' }).first()).toBeVisible({ timeout: 30_000 });
  const types = await cliItems.evaluateAll((els) => els.map((el) => el.getAttribute('data-type')));
  expect(new Set(types)).toEqual(new Set(['cli.output']));
  const seqs = await cliItems.evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-seq'))));
  expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
  expect(await logs.evaluate(() => (window as unknown as { __noReload?: boolean }).__noReload)).toBe(true);
  await expectNoSeriousA11yViolations(logs, 'task events: live CLI output (real stack)');
  await logs.close();
});

test('AC-06: PAUSE/RESUME directives are delivered, acknowledged and completed; CHANGE_REQUEST impact preview → confirm → replan (new plan version) → acknowledged by agents → completed', async ({
  page,
}) => {
  const project = await stack.project('AC-06');
  await stack.stopExec(); // run RUNNING, задачи ждут исполнителя — поручения применяются к живому run
  await loginLive(page, fx);
  const runId = await startDevelopRun(page, project, 'Сервис экспорта отчётов');
  const base = missionControl(project, runId);
  await expect(runStatus(page)).toHaveText('Выполняется', { timeout: 60_000 });

  // Поручения — из User Command Center Mission Control (критические PAUSE/STOP — подтверждение до отправки).
  const center = page.getByTestId('command-center');
  const sendDirective = async (type: string, message: string, preview?: boolean) => {
    await center.getByLabel('Команда').selectOption(type);
    await center.getByLabel('Текст поручения').fill(message);
    if (preview !== undefined) await center.getByLabel('Показать влияние до применения').setChecked(preview);
    await center.getByRole('button', { name: 'Отправить поручение' }).click();
    const confirm = page.getByRole('dialog', { name: /^Отправить «/ });
    if (type === 'PAUSE' || type === 'STOP') await confirm.getByRole('button', { name: 'Подтвердить и отправить' }).click();
  };

  // PAUSE: команда исполняется синхронно — доставлено → подтверждено → выполнено; run → PAUSED.
  await sendDirective('PAUSE', 'Пауза на время проверки требований');
  let track = page.getByRole('dialog', { name: 'Поручение «Пауза (PAUSE)»' });
  await expect(track).toBeVisible();
  const step = (id: string) => track.locator(`[data-step="${id}"]`);
  for (const s of ['accepted', 'delivered', 'acknowledged', 'completed']) await expect(step(s)).toHaveAttribute('data-state', 'done');
  await expect(track.getByTestId('directive-completion')).toHaveText('выполнено');
  await expectNoSeriousA11yViolations(page, 'directive PAUSE completed (real stack)');
  await track.getByRole('button', { name: 'Закрыть' }).first().click();
  await page.goto(base);
  await expect(runStatus(page)).toHaveText('Приостановлен', { timeout: 30_000 });

  // RESUME — run продолжает работу.
  await sendDirective('RESUME', 'Продолжаем');
  track = page.getByRole('dialog', { name: 'Поручение «Продолжить (RESUME)»' });
  await expect(step('completed')).toHaveAttribute('data-state', 'done');
  await track.getByRole('button', { name: 'Закрыть' }).first().click();
  await page.goto(base);
  await expect(runStatus(page)).toHaveText('Выполняется', { timeout: 30_000 });

  // CHANGE_REQUEST: impact preview (детерминированный анализ на воркере) → подтверждение → replan.
  await expect(page.getByTestId('mc-run-bar').getByText('План v1')).toBeVisible({ timeout: 30_000 });
  await sendDirective('CHANGE_REQUEST', 'Добавьте также экспорт в XLSX', true);
  track = page.getByRole('dialog', { name: 'Поручение «Изменение (CHANGE_REQUEST)»' });
  await expect(track).toBeVisible();
  await expect(step('accepted')).toHaveAttribute('data-state', 'done');
  await expect(step('confirmation')).toHaveAttribute('data-state', 'current', { timeout: 60_000 });
  const impacts = track.getByTestId('directive-impacts');
  await expect(impacts).toContainText('Перепланирование');
  await expect(step('delivered')).toHaveAttribute('data-state', 'pending');
  await expectNoSeriousA11yViolations(page, 'directive impact preview (real stack)');

  await track.getByRole('button', { name: 'Подтвердить и применить' }).click();
  await expect(track.getByText('Поручение подтверждено.')).toBeVisible();
  // Checkpoint → новая plan_version → DISPATCHED: «доставлено» ≠ «выполнено».
  await expect(step('confirmation')).toHaveAttribute('data-state', 'done');
  await expect(step('delivered')).toHaveAttribute('data-state', 'done', { timeout: 60_000 });
  await expect(track.getByTestId('directive-delivery')).toHaveText('доставлено, ждём подтверждения агентов');
  await expect(track.getByTestId('directive-completion')).toHaveText('выполняется');
  await expectNoSeriousA11yViolations(page, 'directive dispatched (real stack)');
  await track.getByRole('button', { name: 'Закрыть' }).first().click();

  // Run реагирует: граф перестроен по новой версии плана (тот же экран, без перезагрузки).
  await expect(page.getByTestId('mc-run-bar').getByText('План v2')).toBeVisible({ timeout: 30_000 });

  // Исполнитель запущен: попытки задач замены получают поручение (доставлено), агенты подтверждают его в итоге
  // (directive_acks), задачи проходят обязательные проверки → «выполнено». Карточка обновляется по SSE, без перезагрузки.
  await page.getByRole('tab', { name: 'Поручения' }).click();
  const card = page.getByTestId('directive-card').filter({ hasText: 'Добавьте также экспорт в XLSX' });
  const cardStep = (id: string) => card.locator(`[data-step="${id}"]`);
  await expect(card.getByTestId('directive-delivery')).toHaveText('доставлено, ждём подтверждения агентов');
  await expect(card.getByTestId('directive-ack')).toHaveCount(2);
  await stack.startExec();
  await expect(cardStep('acknowledged')).toHaveAttribute('data-state', 'done', { timeout: 120_000 });
  await expect(card.getByTestId('directive-delivery')).toHaveText('подтверждено агентами');
  await expect(card.locator('[data-testid="directive-ack"][data-status="ACKNOWLEDGED"]')).toHaveCount(2);
  await expect(cardStep('completed')).toHaveAttribute('data-state', 'done', { timeout: 120_000 });
  await expect(card.getByTestId('directive-completion')).toHaveText('выполнено');
  await expectNoSeriousA11yViolations(page, 'directive acknowledged and completed (real stack)');

  const canvas = page.getByTestId('run-graph-canvas');
  await expect(canvas.locator('[data-task-id][data-status="SUCCEEDED"]')).toHaveCount(2, { timeout: 120_000 });
  await expect(runStatus(page)).toHaveText('Завершён', { timeout: 90_000 });
});

test('AC-08: server-side SSE drop mid-run — the UI resumes with Last-Event-ID, every event exactly once and without gaps', async ({ page }) => {
  const project = await stack.project('AC-08');
  await stack.stopExec();
  await loginLive(page, fx);
  const streamRequests: { lastEventId: string | undefined; url: string }[] = [];
  page.on('request', (r: Request) => {
    if (/\/api\/v1\/runs\/[^/]+\/events/.test(r.url())) streamRequests.push({ lastEventId: r.headers()['last-event-id'], url: r.url() });
  });
  await startDevelopRun(page, project, 'Отчёт о продажах');
  await expect(runStatus(page)).toHaveText('Выполняется', { timeout: 60_000 });

  const feed = page.getByRole('list', { name: 'События run' });
  const state = page.getByTestId('stream-state');
  await expect(state).toHaveAttribute('data-state', 'open', { timeout: 30_000 });
  const seqs = () => feed.getByRole('listitem').evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-seq'))));
  await expect.poll(async () => (await seqs()).length, { timeout: 30_000 }).toBeGreaterThan(2);
  const before = await seqs();
  const lastBefore = Math.max(...before);
  const requestsBefore = streamRequests.length;

  // Процесс API убит посреди run: поток рвётся на стороне сервера. Run тем временем идёт дальше
  // (outbox пишет run_event_log), клиент пытается переподключиться.
  await stack.killApi();
  await expect(state).toHaveAttribute('data-state', /reconnecting|connecting/, { timeout: 30_000 });
  await stack.startExec();
  await page.waitForTimeout(8_000);
  await stack.startApi();

  // После восстановления — run.completed и все промежуточные события.
  await expect(feed.getByRole('listitem').filter({ hasText: 'Run завершён' }).first()).toBeVisible({ timeout: 120_000 });
  const after = await seqs();
  expect(after.length).toBeGreaterThan(before.length);
  const max = Math.max(...after);
  expect(after, 'каждое событие ровно один раз, по порядку seq, без пропусков').toEqual(Array.from({ length: max }, (_, i) => i + 1));
  const ids = await feed.getByRole('listitem').evaluateAll((els) => els.map((el) => el.getAttribute('data-event-id')));
  expect(new Set(ids).size).toBe(ids.length);
  // Переподключение продолжило поток с последнего принятого seq (Last-Event-ID).
  const resumed = streamRequests.slice(requestsBefore).filter((r) => r.lastEventId !== undefined);
  expect(resumed.length).toBeGreaterThan(0);
  expect(Number(resumed[0]?.lastEventId)).toBeGreaterThanOrEqual(lastBefore);
  expect(resumed.every((r) => Number(r.lastEventId) >= lastBefore)).toBe(true);
  await expect(runStatus(page)).toHaveText('Завершён', { timeout: 60_000 });
  await expect(state).not.toHaveAttribute('data-state', 'failed');
});
