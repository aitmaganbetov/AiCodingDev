/**
 * P7-06 Live Agent Chat (AC-05) и P7-07 поручения (AC-06): моки операций chat/directives
 * регистрируются здесь (api.route) и валидируются по контракту; SSE — мок сервера (mockApi).
 * С F4 лента — вид «Беседа агентов» AI Mission Control (прежний путь `…/chat` перенаправляется с
 * фильтрами), список поручений — вид «Поручения»; три панели — с 1366 px.
 */
import type { components } from '../../src/api/schema';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type MockRequest } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

test.use({ viewport: { width: 1366, height: 900 } });

/** Фильтры «Беседы» свёрнуты под кнопкой «Фильтры» (экономия высоты центральной панели). */
async function openFilters(page: import('@playwright/test').Page) {
  const toggle = page.getByRole('button', { name: 'Фильтры', exact: true });
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
}

async function openDirectives(page: import('@playwright/test').Page) {
  await page.getByRole('tab', { name: 'Поручения' }).click();
}

let n = 0;
const uid = (group: string) => `01900000-0000-7000-${group}-${String(++n).padStart(12, '0')}`;
const AT = (s: number) => `2026-11-08T09:00:${String(s).padStart(2, '0')}Z`;

class ChatBackend {
  items: S['ChatItem'][] = [];
  threads: S['ChatThread'][] = [];
  directives: S['Directive'][] = [];
  /** Сколько раз отдавали ленту (для проверки live-обновления REST). */
  feedCalls = 0;

  constructor(
    private readonly api: MockApi,
    private readonly run: S['Run'],
  ) {}

  install(): void {
    const { api, run } = this;
    const pageOf = <T>(items: T[], cursor: string | null, limit: number) => {
      const start = cursor ? Number(Buffer.from(cursor, 'base64url').toString('utf8')) : 0;
      const end = start + limit;
      return { items: items.slice(start, end), next_cursor: end < items.length ? Buffer.from(String(end)).toString('base64url') : null };
    };
    api.route('GET', '/api/v1/runs/{id}/chat', 'getRunChat', ({ url, id }) => {
      if (id !== run.id) return problem(404, 'not_found');
      this.feedCalls += 1;
      const q = url.searchParams;
      const stream = q.get('stream') ?? 'all_agents';
      const role = q.get('role');
      const task = q.get('task_id');
      const types = q.getAll('item_type');
      const text = q.get('q')?.toLowerCase();
      if (stream === 'role' && !role) return problem(422, 'validation_error', { errors: [{ pointer: '/role', code: 'required' }] });
      let items = this.items.filter((i) => {
        if ((stream === 'codex' || stream === 'claude') && i.sender?.provider !== stream) return false;
        if (stream === 'role' && !(i.sender?.role === role || i.recipient_role === role || (i.mentions ?? []).includes(role as S['AgentRole'])))
          return false;
        if (stream === 'task' && i.task_id !== task) return false;
        if (stream === 'approvals' && i.item_type !== 'approval') return false;
        if (q.get('thread_id') && i.thread_id !== q.get('thread_id')) return false;
        if (types.length > 0 && !types.includes(i.item_type)) return false;
        if (text && !i.body.toLowerCase().includes(text)) return false;
        return true;
      });
      items = [...items].sort((a, b) => a.created_at.localeCompare(b.created_at));
      if ((q.get('order') ?? 'desc') === 'desc') items.reverse();
      return { status: 200, body: { ...pageOf(items, q.get('cursor'), Number(q.get('limit') ?? 50)), threads: this.threads } };
    });
    api.route('POST', '/api/v1/runs/{id}/chat/messages', 'postChatMessage', ({ body }) => {
      const b = body as S['ChatMessageCreateRequest'];
      const msg: S['ChatMessage'] = {
        id: uid('c000'),
        thread_id: b.thread_id ?? this.threads[0]!.id,
        sender_type: 'user',
        sender_id: '01900000-0000-7000-8000-0000000000aa',
        body: b.body,
        message_kind: 'text',
        created_at: AT(50),
      };
      this.items.push({
        id: msg.id,
        item_type: 'chat_message',
        thread_id: msg.thread_id,
        sender_type: 'user',
        sender: { user: { id: '01900000-0000-7000-8000-0000000000aa', display_name: api.user.display_name }, role: null, provider: null },
        message_kind: 'text',
        body: msg.body,
        mentions: b.mentions ?? [],
        created_at: msg.created_at,
      });
      return { status: 201, body: msg, headers: { Location: `/api/v1/runs/${run.id}/chat/messages/${msg.id}`, ETag: '"v0"' } };
    });
    api.route('GET', '/api/v1/runs/{id}/directives', 'listDirectives', ({ url }) => ({
      status: 200,
      body: pageOf([...this.directives].reverse(), url.searchParams.get('cursor'), Number(url.searchParams.get('limit') ?? 50)),
    }));
    api.route('POST', '/api/v1/runs/{id}/directives', 'createDirective', ({ body }) => {
      const b = body as S['DirectiveCreateRequest'];
      if (b.expected_run_version !== run.version) return problem(409, 'version_mismatch', { current_version: run.version } as Partial<S['Problem']>);
      const d: S['Directive'] = {
        id: uid('d000'),
        run_id: run.id,
        chat_message_id: uid('c000'),
        target: b.target,
        directive_type: b.directive_type,
        message: b.message,
        status: 'IMPACT_ANALYSIS_PENDING',
        impact_status: 'PENDING',
        impact_summary: null,
        impacts: [],
        requires_confirmation: false,
        approval_id: null,
        delivery: { state: 'NOT_DISPATCHED', acknowledgements: [] },
        completion: { state: 'NOT_STARTED', resulting_task_ids: [] },
        submitted_by: { id: '01900000-0000-7000-8000-0000000000aa', display_name: api.user.display_name },
        created_at: AT(30),
        updated_at: AT(30),
        version: 0,
      };
      this.directives.push(d);
      api.addEvent(run, 'directive.state_changed', { directive_id: d.id, directive_type: d.directive_type, from_status: 'RECEIVED', to_status: d.status });
      return {
        status: 202,
        body: { directive_id: d.id, status: d.status, chat_message_id: d.chat_message_id, status_url: `/api/v1/runs/${run.id}/directives/${d.id}` },
      };
    });
    const find = (req: MockRequest) => this.directives.find((d) => req.url.pathname.split('/')[6] === d.id);
    api.route('GET', '/api/v1/runs/{id}/directives/[^/]+', 'getDirective', (req) => {
      const d = find(req);
      return d ? { status: 200, body: d, headers: { ETag: `"v${d.version}"` } } : problem(404, 'not_found');
    });
    const command = (kind: 'confirm' | 'cancel') => (req: MockRequest) => {
      const d = find(req);
      if (!d) return problem(404, 'not_found');
      if (req.headers['if-match'] !== `"v${d.version}"`) return problem(412, 'precondition_failed');
      const allowed = kind === 'confirm' ? ['AWAITING_CONFIRMATION'] : ['RECEIVED', 'IMPACT_ANALYSIS_PENDING', 'IMPACT_READY', 'AWAITING_CONFIRMATION', 'CONFIRMED'];
      if (!allowed.includes(d.status)) return problem(409, 'invalid_state_transition', { current_status: d.status } as Partial<S['Problem']>);
      this.update(d.id, { status: kind === 'confirm' ? 'CONFIRMED' : 'CANCELLED' });
      return { status: 202, body: d };
    };
    api.route('POST', '/api/v1/runs/{id}/directives/[^/]+/confirm', 'confirmDirective', command('confirm'));
    api.route('POST', '/api/v1/runs/{id}/directives/[^/]+/cancel', 'cancelDirective', command('cancel'));
  }

  /** Переход поручения на «сервере»: состояние + событие directive.state_changed в журнал run. */
  update(id: string, patch: Partial<S['Directive']>, recipients?: { role: S['AgentRole']; ack_status: string }[]): S['Directive'] {
    const d = this.directives.find((x) => x.id === id)!;
    const from = d.status;
    Object.assign(d, patch, { version: d.version + 1 });
    if (patch.status && patch.status !== from) {
      this.api.addEvent(this.run, 'directive.state_changed', {
        directive_id: d.id,
        directive_type: d.directive_type,
        from_status: from,
        to_status: d.status,
        ...(recipients ? { recipients } : {}),
      });
    }
    return d;
  }
}

function seed(api: MockApi, role: S['WorkspaceRole'] = 'developer') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'RUNNING', version: 3 });
  const a = api.addTask(run, 'Спроектировать API', { agent_role: 'architect', status: 'SUCCEEDED', attempt_count: 1 });
  const b = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING', depends_on: [a.id], attempt_count: 1, current_provider: 'codex' });
  const chat = new ChatBackend(api, run);
  const threadRun = uid('e000');
  chat.threads = [
    { id: threadRun, run_id: run.id, scope: 'run', created_at: AT(0) },
    { id: uid('e000'), run_id: run.id, scope: 'role', target_role: 'developer', created_at: AT(0) },
  ];
  chat.items = [
    {
      id: uid('c000'),
      item_type: 'agent_message',
      thread_id: threadRun,
      task_id: a.id,
      sender_type: 'agent',
      sender: { user: null, role: 'architect', provider: 'claude' },
      recipient_role: 'developer',
      message_kind: 'agent_relay',
      acp_type: 'HANDOFF',
      body: 'Контракт API готов, передаю реализацию',
      mentions: [],
      refs: [{ type: 'diff', id: uid('f000') }],
      created_at: AT(1),
    },
    {
      id: uid('c000'),
      item_type: 'chat_message',
      thread_id: threadRun,
      sender_type: 'user',
      sender: { user: { id: '01900000-0000-7000-8000-0000000000bb', display_name: 'Иван Смирнов' }, role: null, provider: null },
      message_kind: 'text',
      body: 'Проверьте формат CSV <b>срочно</b><script>window.pwned=1</script>',
      mentions: ['developer'],
      created_at: AT(2),
    },
  ];
  chat.install();
  const url = `/w/${ws.id}/projects/${project.id}/runs/${run.id}/chat`;
  return { ws, project, run, a, b, chat, url };
}

test('AC-05: live chat shows inter-agent messages and handoffs streaming in, with streams, filters, search and mentions', async ({ page, api }) => {
  const { run, a, b, chat, url } = seed(api);
  await login(page, api);
  await page.goto(url);

  await expect(page).toHaveURL(/\/mission-control\?run=.*view=conversation/);
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeVisible();
  await expect(page.getByRole('heading', { level: 2, name: 'Live Agent Chat' })).toBeVisible();
  const timeline = page.getByTestId('chat-timeline');
  const items = timeline.getByTestId('chat-item');
  await expect(items).toHaveCount(2);
  const handoff = items.nth(0);
  await expect(handoff.getByRole('heading')).toHaveText('Архитектор · Claude → Разработчик');
  await expect(handoff).toContainText('Передача задачи');
  await expect(handoff).toContainText('Контракт API готов, передаю реализацию');
  await expect(handoff.getByRole('link', { name: `Задача: ${a.title}` })).toBeVisible();
  await expect(handoff.locator('time')).toHaveAttribute('datetime', AT(1));
  // Текст пользователя/агента — недоверенные данные: только текст, без HTML.
  await expect(items.nth(1).getByTestId('chat-item-body')).toHaveText('Проверьте формат CSV <b>срочно</b><script>window.pwned=1</script>');
  await expect(timeline.locator('b, script')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { pwned?: number }).pwned)).toBeUndefined();
  await expectNoSeriousA11yViolations(page, 'live chat');

  // Live: developer (Codex) передаёт работу на ревью — сообщение приходит по SSE и появляется без перезагрузки.
  const messageId = uid('c000');
  api.addEvent(run, 'agent.message', {
    message_id: uid('b000'),
    message_type: 'REVIEW_REQUEST',
    task_id: b.id,
    from: { role: 'developer', provider: 'codex' },
    to: { role: 'reviewer' },
    correlation_id: uid('b000'),
    summary: 'Экспорт готов, прошу ревью',
  });
  api.addEvent(run, 'chat.message', {
    chat_message_id: messageId,
    thread_id: chat.threads[0]!.id,
    scope: 'RUN',
    message_kind: 'AGENT_MESSAGE',
    sender_type: 'agent',
    sender_role: 'developer',
    provider: 'codex',
    task_id: b.id,
    target_role: 'reviewer',
    body: 'Экспорт готов, прошу ревью',
    created_at: AT(10),
  });
  const live = items.filter({ hasText: 'Экспорт готов, прошу ревью' });
  await expect(live).toHaveCount(1);
  await expect(live.getByRole('heading')).toHaveText('Разработчик · Codex → Ревьюер');
  // REST-проекция (с типом ACP) заменяет live-элемент — без дублей.
  chat.items.push({
    id: messageId,
    item_type: 'agent_message',
    thread_id: chat.threads[0]!.id,
    task_id: b.id,
    sender_type: 'agent',
    sender: { user: null, role: 'developer', provider: 'codex' },
    recipient_role: 'reviewer',
    message_kind: 'agent_relay',
    acp_type: 'REVIEW_REQUEST',
    body: 'Экспорт готов, прошу ревью',
    mentions: [],
    created_at: AT(10),
  });
  api.addEvent(run, 'chat.message', {
    chat_message_id: uid('c000'),
    thread_id: chat.threads[0]!.id,
    scope: 'RUN',
    message_kind: 'STATUS',
    sender_type: 'system',
    body: 'Задача «Реализовать экспорт» ожидает ревью',
    created_at: AT(11),
  });
  await expect(live).toContainText('Запрос ревью');
  await expect(items).toHaveCount(4);
  await expect(live).not.toHaveAttribute('data-live', 'true');

  // Потоки: Codex → только сообщения Codex.
  await openFilters(page);
  await page.getByRole('combobox', { name: 'Поток', exact: true }).selectOption({ label: 'Codex' });
  await expect(page).toHaveURL(/stream=codex/);
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText('Экспорт готов');
  // Роль: без выбора роли — подсказка, с ролью — поток роли.
  await page.getByRole('combobox', { name: 'Поток', exact: true }).selectOption({ label: 'Роль' });
  await expect(page.getByTestId('chat-incomplete')).toBeVisible();
  await page.getByRole('combobox', { name: 'Роль', exact: true }).selectOption({ label: 'Ревьюер' });
  await expect(page).toHaveURL(/stream=role&role=reviewer/);
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText('Экспорт готов');
  // Задача.
  await page.getByRole('combobox', { name: 'Поток', exact: true }).selectOption({ label: 'Задача' });
  await page.getByRole('combobox', { name: 'Задача', exact: true }).selectOption({ label: a.title });
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText('Передача задачи');
  await expectNoSeriousA11yViolations(page, 'live chat task stream');

  // Поиск (FR-CHAT-06): полнотекстовый запрос уходит на сервер.
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
  await expect(items).toHaveCount(4);
  await page.getByRole('searchbox', { name: 'Поиск по ленте' }).fill('CSV');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/q=CSV/);
  await expect(items).toHaveCount(1);

  // Упоминание роли — переход в поток роли.
  await items.first().getByRole('button', { name: 'Показать поток роли «Разработчик»' }).click();
  await expect(page).toHaveURL(/stream=role&role=developer/);
  await expect(items).toHaveCount(3); // получатель, упоминание и отправитель — роль developer

  // Тип элементов.
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
  await page.getByRole('checkbox', { name: 'Событие' }).check();
  await expect(items).toHaveCount(1);
  await expect(items.first()).toContainText('ожидает ревью');
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();

  // Комментарий с упоминаниями ролей (не доставляется агентам).
  await page.getByText('Комментарий в ленту (агентам не доставляется)').click();
  await page.getByLabel('Текст комментария').fill('Команда, проверьте @qa');
  await page.getByRole('button', { name: 'Упомянуть роль' }).click();
  await page.getByRole('dialog', { name: 'Упомянуть роли' }).getByRole('checkbox', { name: /Ревьюер/ }).check();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('composer-mentions')).toHaveText('Упомянуты: Ревьюер, QA');
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByText('Комментарий отправлен.')).toBeVisible();
  const post = api.requests.find((r) => r.operationId === 'postChatMessage');
  expect(post?.body).toEqual({ body: 'Команда, проверьте @qa', mentions: ['reviewer', 'qa'] });
  await expect(items.filter({ hasText: 'Команда, проверьте @qa' })).toHaveCount(1);
  expect(chat.feedCalls).toBeGreaterThan(3);
});

test('AC-06: CHANGE_REQUEST with impact preview → confirmation → delivered / acknowledged / completed', async ({ page, api }) => {
  const { run, b, chat, url } = seed(api);
  await login(page, api);
  await page.goto(url);
  await openDirectives(page);
  const panel = page.getByTestId('directives-panel');
  await expect(panel.getByRole('heading', { name: 'Поручений пока нет' })).toBeVisible();

  await panel.getByRole('button', { name: 'Новое поручение' }).click();
  const dialog = page.getByRole('dialog', { name: 'Новое поручение' });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('Команда').selectOption('CHANGE_REQUEST');
  await dialog.getByRole('radio', { name: 'Задача' }).check();
  // Валидация: без задачи и текста — ошибки у полей, фокус на первом.
  await dialog.getByRole('button', { name: 'Отправить поручение' }).click();
  await expect(dialog.getByText('Введите текст поручения.')).toBeVisible();
  await expect(dialog.getByRole('combobox', { name: 'Задача' })).toHaveAttribute('aria-invalid', 'true');
  await expectNoSeriousA11yViolations(page, 'directive form');
  await dialog.getByRole('combobox', { name: 'Задача' }).selectOption({ label: b.title });
  await dialog.getByLabel('Текст поручения').fill('Добавьте также экспорт в XLSX');
  await dialog.getByRole('button', { name: 'Отправить поручение' }).click();

  await expect.poll(() => api.requests.some((r) => r.operationId === 'createDirective')).toBe(true);
  const create = api.requests.find((r) => r.operationId === 'createDirective');
  expect(create?.body).toEqual({
    target: { type: 'task', task_id: b.id },
    directive_type: 'CHANGE_REQUEST',
    message: 'Добавьте также экспорт в XLSX',
    expected_run_version: 3,
    requires_impact_preview: true,
  });
  const track = page.getByRole('dialog', { name: 'Поручение «Изменение (CHANGE_REQUEST)»' });
  await expect(track).toBeVisible();
  await expect(track.getByText('Идёт анализ влияния…')).toBeVisible();
  const step = (id: string) => track.locator(`[data-step="${id}"]`);
  await expect(step('accepted')).toHaveAttribute('data-state', 'done');
  await expect(step('impact')).toHaveAttribute('data-state', 'current');

  // Анализ влияния готов: impact preview требует подтверждения.
  const d = chat.directives[0]!;
  const impacts: S['DirectiveImpact'][] = [{ task_id: b.id, impact_type: 'REPLAN_REQUIRED', resolution: 'Перепланирование в safe point' }];
  api.addEvent(run, 'directive.impact_ready', {
    directive_id: d.id,
    summary: 'Затронута 1 задача: требуется перепланирование',
    impacts,
    requires_confirmation: true,
    expected_run_version: 3,
  });
  // Сводка анализа приходит в REST-представлении (Directive.impact_summary), событие лишь инициирует перечитывание.
  chat.update(d.id, {
    status: 'AWAITING_CONFIRMATION',
    impact_status: 'READY',
    impact_summary: 'Затронута 1 задача: требуется перепланирование',
    impacts,
    requires_confirmation: true,
  });
  await expect(track.getByText('Затронута 1 задача: требуется перепланирование')).toBeVisible();
  const impactList = track.getByTestId('directive-impacts');
  await expect(impactList).toContainText('Перепланирование');
  await expect(impactList.getByRole('link', { name: b.title })).toBeVisible();
  await expect(step('confirmation')).toHaveAttribute('data-state', 'current');
  await expect(step('delivered')).toHaveAttribute('data-state', 'pending');
  await expectNoSeriousA11yViolations(page, 'directive impact preview');

  // Подтверждение (If-Match версии поручения).
  await track.getByRole('button', { name: 'Подтвердить и применить' }).click();
  await expect(track.getByText('Поручение подтверждено.')).toBeVisible();
  const confirm = api.requests.find((r) => r.operationId === 'confirmDirective');
  expect(confirm?.headers['if-match']).toBe('"v1"');
  expect(confirm?.headers['idempotency-key']).toBeTruthy();
  await expect(step('confirmation')).toHaveAttribute('data-state', 'done');

  // Доставлено агентам (DISPATCHED) — ещё не подтверждено агентами и не выполнено.
  chat.update(
    d.id,
    { status: 'DISPATCHED', delivery: { state: 'DISPATCHED', acknowledgements: [{ recipient_role: 'developer', status: 'DELIVERED', acknowledged_at: null }] } },
    [{ role: 'developer', ack_status: 'DELIVERED' }],
  );
  await expect(step('delivered')).toHaveAttribute('data-state', 'done');
  await expect(step('acknowledged')).toHaveAttribute('data-state', 'current');
  await expect(step('completed')).toHaveAttribute('data-state', 'pending');
  await expect(track.getByTestId('directive-delivery')).toHaveText('доставлено, ждём подтверждения агентов');
  await expect(track.getByTestId('directive-completion')).toHaveText('не начато');

  // Агенты подтвердили (ACKNOWLEDGED), выполнение идёт.
  chat.update(
    d.id,
    {
      status: 'ACKNOWLEDGED',
      delivery: { state: 'ACKNOWLEDGED', acknowledgements: [{ recipient_role: 'developer', status: 'ACKNOWLEDGED', acknowledged_at: AT(40) }] },
      completion: { state: 'IN_PROGRESS', resulting_task_ids: [b.id] },
    },
    [{ role: 'developer', ack_status: 'ACKNOWLEDGED' }],
  );
  await expect(step('acknowledged')).toHaveAttribute('data-state', 'done');
  await expect(step('acknowledged')).toContainText('1 из 1');
  await expect(step('completed')).toHaveAttribute('data-state', 'current');
  await expect(track.getByTestId('directive-completion')).toHaveText('выполняется');

  // Выполнено.
  chat.update(d.id, { status: 'COMPLETED', completion: { state: 'COMPLETED', resulting_task_ids: [b.id], completed_at: AT(50) } });
  await expect(step('completed')).toHaveAttribute('data-state', 'done');
  await expect(track.getByTestId('directive-completion')).toHaveText('выполнено');
  await expectNoSeriousA11yViolations(page, 'directive completed');

  await track.getByRole('button', { name: 'Закрыть' }).first().click();
  await expect(track).toBeHidden();
  await expect(panel.getByRole('button', { name: 'Новое поручение' })).toBeFocused();
  const card = panel.getByTestId('directive-card');
  await expect(card).toHaveCount(1);
  await expect(card.getByTestId('directive-details')).toHaveAttribute('data-status', 'COMPLETED');
  await expect(card).toContainText('Добавьте также экспорт в XLSX');
});

test('directives: stale run version (409) is reported and retried with the fresh version; cancel before dispatch', async ({ page, api }) => {
  const { run, chat, url } = seed(api);
  await login(page, api);
  await page.goto(url);
  await openDirectives(page);
  const panel = page.getByTestId('directives-panel');
  await panel.getByRole('button', { name: 'Новое поручение' }).click();
  const dialog = page.getByRole('dialog', { name: 'Новое поручение' });
  await expect(dialog.getByText('Поручение относится к версии run v3')).toBeVisible();
  run.version = 4; // run ушёл вперёд, пока пользователь писал поручение
  await dialog.getByLabel('Текст поручения').fill('Что осталось сделать?');
  await dialog.getByRole('button', { name: 'Отправить поручение' }).click();
  await expect(dialog.getByTestId('form-alert')).toHaveText('Данные изменились. Обновите страницу.');
  await expect(dialog.getByText('Поручение относится к версии run v4')).toBeVisible();
  await dialog.getByRole('button', { name: 'Отправить поручение' }).click();
  const track = page.getByRole('dialog', { name: 'Поручение «Вопрос (ASK)»' });
  await expect(track).toBeVisible();
  const creates = api.requests.filter((r) => r.operationId === 'createDirective');
  expect(creates.map((r) => (r.body as S['DirectiveCreateRequest']).expected_run_version)).toEqual([3, 4]);
  expect(creates[0]!.headers['idempotency-key']).not.toBe(creates[1]!.headers['idempotency-key']);

  await track.getByRole('button', { name: 'Отозвать поручение' }).click();
  await track.getByRole('button', { name: 'Да, отозвать' }).click();
  await expect(track.getByText('Поручение отозвано.')).toBeVisible();
  await expect(track.locator('[data-step="impact"]')).toHaveAttribute('data-state', 'failed');
  expect(chat.directives[0]!.status).toBe('CANCELLED');
});

test('chat permissions: viewer reads only; reviewer gets the restricted command set', async ({ page, api }) => {
  const { url } = seed(api, 'viewer');
  await login(page, api);
  await page.goto(url);
  await expect(page.getByTestId('chat-item')).toHaveCount(2);
  await expect(page.getByText('Ваша роль позволяет только читать ленту.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Новое поручение' })).toHaveCount(0);
  await expect(page.getByText('Ваша роль не позволяет создавать поручения.')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'chat viewer');
});

test('chat: reviewer command set and APPROVE target from pending approvals', async ({ page, api }) => {
  const { ws, run, url } = seed(api, 'reviewer');
  const approvalId = uid('a000');
  api.route('GET', '/api/v1/approvals', 'listApprovals', ({ url: u }) => {
    // Счётчик оболочки (без run_id) — пустой список; композер запрашивает approvals своего run.
    if (u.searchParams.get('run_id') !== run.id) return { status: 200, body: { items: [], next_cursor: null } };
    return {
      status: 200,
      body: {
        items: [
          {
            id: approvalId,
            workspace_id: ws.id,
            run_id: run.id,
            action_type: 'MERGE',
            status: 'PENDING',
            risk_level: 'medium',
            summary: 'Слияние ветки feature/export',
            requested_by: { actor_type: 'agent', user: null, role: 'developer' },
            can_decide: true,
            created_at: AT(5),
            version: 0,
          },
        ],
        next_cursor: null,
      },
    };
  });
  await login(page, api);
  await page.goto(url);
  await openDirectives(page);
  await page.getByRole('button', { name: 'Новое поручение' }).click();
  const dialog = page.getByRole('dialog', { name: 'Новое поручение' });
  const type = dialog.getByLabel('Команда');
  await expect(type.locator('option')).toHaveText(['Вопрос (ASK)', 'Ревью (REVIEW)', 'Одобрить (APPROVE)', 'Отклонить (REJECT)']);
  await type.selectOption('APPROVE');
  await dialog.getByLabel('Запрос одобрения').selectOption({ label: 'Слияние ветки feature/export' });
  await dialog.getByLabel('Текст поручения').fill('Одобряю слияние');
  await dialog.getByRole('button', { name: 'Отправить поручение' }).click();
  await expect(page.getByRole('dialog', { name: 'Поручение «Одобрить (APPROVE)»' })).toBeVisible();
  const create = api.requests.find((r) => r.operationId === 'createDirective');
  expect((create?.body as S['DirectiveCreateRequest']).target).toEqual({ type: 'approval', approval_id: approvalId });
});
