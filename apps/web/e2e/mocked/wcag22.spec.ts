/**
 * F9 — WCAG 2.2 AA (D-1) по всем основным экранам и выпускные замеры (план §5.9, §7.1 F9):
 * - 2.5.8 Target Size (Minimum): автоматическая проверка всех видимых целей указателя на каждом экране
 *   (≥ 24×24 CSS px либо исключение «отступ» — круг Ø 24 px вокруг центра цели не задевает другие
 *   цели; исключение «в строке текста» — ссылка внутри предложения), 1366 светлая и 375 тёмная;
 * - 2.4.11 Focus Not Obscured: обход Shift+Tab снизу вверх длинной страницы — элемент с фокусом не
 *   скрыт целиком под sticky верхней панелью; наложение контекстной панели закрывается, когда фокус
 *   уходит в основную область;
 * - 3.2.6 Consistent Help: справка (сочетания клавиш) и меню пользователя — в одном месте верхней
 *   панели на всех экранах;
 * - 3.3.8 Accessible Authentication: вход без когнитивного теста, вставка и автозаполнение разрешены;
 * - 2.5.7 Dragging Movements: размеры панелей Mission Control меняются из меню, без перетаскивания.
 * Замер (AICD_E2E_PERF=1): Mission Control › Вывод CLI с 10 000 строк — кадры при прокрутке.
 * Аудит (AICD_E2E_AUDIT_DIR=<dir>): скриншоты всех основных экранов 1920/1366/768/375 × тёмная/светлая.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Page } from '@playwright/test';
import type { components } from '../../src/api/schema';
import { settleAnimations } from '../support/a11y';
import { PASSWORD, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

function seed(api: MockApi) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const project = api.addProject(ws.id, 'Платёжный шлюз', 'Сервис приёма и сверки платежей');
  api.addProject(ws.id, 'Мобильное приложение');
  api.addProject(ws.id, 'Внутренний портал');
  api.addRun(project, { objective: 'Обновить зависимости', status: 'COMPLETED', finished_at: '2026-11-08T08:30:00Z' });
  const run = api.addRun(project, { objective: 'Добавить экспорт отчётов в CSV', status: 'RUNNING', version: 3 });
  const a = api.addTask(run, 'Спроектировать API', { agent_role: 'architect', status: 'SUCCEEDED', attempt_count: 1, current_provider: 'claude' });
  const b = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING', depends_on: [a.id], attempt_count: 1, current_provider: 'codex', version: 2 });
  api.addTask(run, 'Проверить экспорт', { agent_role: 'qa', status: 'PENDING', depends_on: [b.id] });
  api.addEvent(run, 'task.state_changed', {
    task_id: b.id,
    title: b.title,
    agent_role: 'developer',
    from_status: 'READY',
    to_status: 'RUNNING',
    task_version: 2,
    attempt_no: 1,
    provider: 'codex',
  });
  mockScreens(api, ws.id);
  return { ws, project, run, b };
}

const AT = '2026-11-08T09:00:00Z';
const DISCLAIMER = 'Автоматические проверки не являются официальной сертификацией (ТЗ §13).';
const TECH_VERSION = '01900000-0000-7000-a200-000000000002';

/** Операции экранов вне SHELL_DEFAULTS: небольшие, валидные по контракту ответы (данные только мока). */
function mockScreens(api: MockApi, workspaceId: string) {
  const ok = (body: unknown) => ({ status: 200, body });
  api.route('GET', '/api/v1/runs/{id}/findings', 'listRunFindings', () => ok({ items: [], next_cursor: null }));
  api.route('GET', '/api/v1/projects/{id}/worktrees', 'listProjectWorktrees', () => ok({ items: [], next_cursor: null }));
  api.route('GET', '/api/v1/projects/{id}/controls', 'listProjectControls', () =>
    ok({ items: [], next_cursor: null, quality_profile: 'strict', disclaimer: DISCLAIMER }),
  );
  api.route('GET', '/api/v1/projects/{id}/technologies', 'listTechnologies', () => ok({ items: [], next_cursor: null }));
  const installation: S['GitHubInstallation'] = {
    id: '01900000-0000-7000-e000-000000000001',
    installation_id: 4242,
    account_login: 'alpha-org',
    account_type: 'organization',
    repository_selection: 'selected',
    permissions: { contents: 'write', pull_requests: 'write' },
    status: 'ACTIVE',
    created_at: AT,
    updated_at: AT,
  };
  api.route('GET', '/api/v1/integrations/github/installations', 'listGitHubInstallations', () => ok({ items: [installation] }));
  const source: S['DocumentationSource'] = {
    id: '01900000-0000-7000-d000-000000000001',
    workspace_id: workspaceId,
    technology_version_id: TECH_VERSION,
    source_type: 'url',
    uri_or_file_ref: 'https://docs.python.org/3.12/',
    trust_level: 'official',
    license_info: 'PSF',
    refresh_policy: 'manual',
    enabled: true,
    latest_snapshot: null,
    created_at: '2026-11-01T00:00:00Z',
  };
  api.route('GET', '/api/v1/documentation/sources', 'listDocumentationSources', () => ok({ items: [source], next_cursor: null }));
  api.route('GET', '/api/v1/technologies', 'listWorkspaceTechnologies', () =>
    ok({
      items: [
        {
          technology_id: '01900000-0000-7000-a200-000000000001',
          name: 'Python',
          ecosystem: 'pypi',
          technology_version_id: TECH_VERSION,
          version_label: '3.12',
          support_status: 'supported',
          project_count: 1,
          source_count: 1,
        },
      ],
      next_cursor: null,
    }),
  );
  const members: S['Member'][] = [
    { user: { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна' }, email: 'anna@example.com', role: 'owner', joined_at: AT, version: 0 },
    { user: { id: '01900000-0000-7000-8000-0000000000bb', display_name: 'Борис Петров' }, email: 'boris@example.com', role: 'developer', joined_at: AT, version: 0 },
  ];
  api.route('GET', '/api/v1/workspaces/{id}/members', 'listMembers', () => ok({ items: members, next_cursor: null }));
  const audit: S['AuditEvent'][] = [
    {
      id: '01900000-0000-7000-c000-000000000001',
      workspace_id: workspaceId,
      actor_type: 'user',
      actor_id: '01900000-0000-7000-8000-0000000000aa',
      action: 'project.create',
      resource_type: 'project',
      resource_id: '01900000-0000-7000-9000-000000000001',
      metadata: { outcome: 'success', request_id: 'req-1' } as unknown as S['AuditEvent']['metadata'],
      created_at: AT,
    },
  ];
  api.route('GET', '/api/v1/audit-events', 'listAuditEvents', () => ok({ items: audit, next_cursor: null }));
}

interface Screen {
  name: string;
  path: (ids: ReturnType<typeof seed>) => string;
}

/** Основные экраны (IA D-3) + подразделы Настроек. */
const SCREENS: Screen[] = [
  { name: 'dashboard', path: ({ ws }) => `/w/${ws.id}/dashboard` },
  { name: 'projects', path: ({ ws }) => `/w/${ws.id}/projects` },
  { name: 'project-workspace', path: ({ ws, project }) => `/w/${ws.id}/projects/${project.id}` },
  { name: 'code-review', path: ({ ws, project, run }) => `/w/${ws.id}/projects/${project.id}/review/${run.id}` },
  { name: 'mission-control', path: ({ ws, run }) => `/w/${ws.id}/mission-control?run=${run.id}` },
  { name: 'agents', path: ({ ws }) => `/w/${ws.id}/agents` },
  { name: 'workflows', path: ({ ws }) => `/w/${ws.id}/runs` },
  { name: 'code-intelligence', path: ({ ws }) => `/w/${ws.id}/code` },
  { name: 'security-center', path: ({ ws }) => `/w/${ws.id}/security` },
  { name: 'quality-gates', path: ({ ws }) => `/w/${ws.id}/quality` },
  { name: 'standards', path: ({ ws }) => `/w/${ws.id}/compliance` },
  { name: 'github-integration', path: ({ ws }) => `/w/${ws.id}/integrations` },
  { name: 'analytics', path: ({ ws }) => `/w/${ws.id}/analytics` },
  { name: 'documentation', path: ({ ws }) => `/w/${ws.id}/knowledge` },
  { name: 'approvals', path: ({ ws }) => `/w/${ws.id}/approvals` },
  { name: 'settings', path: ({ ws }) => `/w/${ws.id}/settings` },
  { name: 'settings-providers', path: ({ ws }) => `/w/${ws.id}/settings/providers` },
  { name: 'settings-audit', path: ({ ws }) => `/w/${ws.id}/settings/audit` },
  { name: 'settings-appearance', path: ({ ws }) => `/w/${ws.id}/settings/appearance` },
];

/** Экран открыт: h1 виден, индикаторы загрузки (role=status со спиннером, aria-busy) исчезли. */
async function openScreen(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.locator('main [aria-busy="true"], main .motion-safe\\:animate-spin')).toHaveCount(0);
  await page.evaluate(() => document.fonts.ready);
  await settleAnimations(page);
}

interface TargetIssue {
  target: string;
  width: number;
  height: number;
}

/**
 * WCAG 2.5.8: цели указателя < 24×24 без исключения «отступ» (круг Ø 24 px вокруг центра не
 * пересекает другие цели и круги других малых целей) и не в строке текста. Сторонние области без
 * собственной разметки (редактор Monaco) не проверяются — см. ACCESSIBILITY.md.
 */
async function targetSizeIssues(page: Page): Promise<TargetIssue[]> {
  return page.evaluate(() => {
    const SELECTOR = [
      'a[href]',
      'button',
      'input:not([type="hidden"])',
      'select',
      'textarea',
      'summary',
      '[role="button"]',
      '[role="link"]',
      '[role="tab"]',
      '[role="menuitem"]',
      '[role="option"]',
      '[role="checkbox"]',
      '[role="radio"]',
      '[role="switch"]',
      '[role="separator"][tabindex]',
      '[tabindex="0"]',
    ].join(',');
    const MIN = 24;
    const EPS = 0.5;
    const all = Array.from(document.querySelectorAll<HTMLElement>(SELECTOR)).filter((el) => {
      if (el.closest('.monaco-editor, [inert], [aria-hidden="true"]')) return false;
      if ((el as HTMLButtonElement).disabled) return false;
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none' || style.pointerEvents === 'none') return false;
      // Визуально скрытые (sr-only: clip) — не цели указателя, пока не получили фокус (skip link).
      if (style.clip === 'rect(0px, 0px, 0px, 0px)' || style.clipPath === 'inset(50%)') return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    });
    // Вложенные цели (кнопка внутри ссылки-карточки и т. п.) — отдельные цели; родитель-обёртка
    // с потомком-целью того же размера не считается дважды.
    const targets = all.filter((el) => !all.some((other) => other !== el && el.contains(other) && sameRect(el, other)));
    function sameRect(a: Element, b: Element): boolean {
      const ra = a.getBoundingClientRect();
      const rb = b.getBoundingClientRect();
      return Math.abs(ra.width - rb.width) < 1 && Math.abs(ra.height - rb.height) < 1;
    }
    /** Ссылка в строке текста (2.5.8, исключение Inline): inline-элемент, рядом — текст того же блока. */
    function inline(el: HTMLElement): boolean {
      if (el.tagName !== 'A' || getComputedStyle(el).display !== 'inline') return false;
      const block = el.parentElement;
      if (!block) return false;
      const text = (block.textContent ?? '').replace(el.textContent ?? '', '').trim();
      return text.length > 0;
    }
    /**
     * Цель-«метка»: нативный checkbox/radio, у которого есть связанная label ≥ 24 px по высоте —
     * эквивалентная цель (клик по метке переключает тот же элемент; исключение Equivalent).
     */
    function equivalentLabel(el: HTMLElement): boolean {
      if (!(el instanceof HTMLInputElement) || !['checkbox', 'radio'].includes(el.type)) return false;
      return Array.from(el.labels ?? []).some((l) => {
        const r = l.getBoundingClientRect();
        return r.height >= MIN - EPS && r.width >= MIN - EPS;
      });
    }
    const rects = targets.map((el) => el.getBoundingClientRect());
    const small = targets.map((el, i) => {
      const r = rects[i]!;
      return (r.width < MIN - EPS || r.height < MIN - EPS) && !inline(el) && !equivalentLabel(el);
    });
    const issues: { target: string; width: number; height: number }[] = [];
    targets.forEach((el, i) => {
      if (!small[i]) return;
      const r = rects[i]!;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const clash = targets.some((other, j) => {
        if (j === i || other.contains(el) || el.contains(other)) return false;
        const o = rects[j]!;
        if (small[j]) {
          const ox = o.left + o.width / 2;
          const oy = o.top + o.height / 2;
          return Math.hypot(cx - ox, cy - oy) < MIN - EPS;
        }
        const dx = Math.max(o.left - cx, 0, cx - o.right);
        const dy = Math.max(o.top - cy, 0, cy - o.bottom);
        return Math.hypot(dx, dy) < MIN / 2 - EPS;
      });
      if (!clash) return;
      const name = el.getAttribute('aria-label') ?? el.textContent?.trim().slice(0, 40) ?? '';
      const id = el.getAttribute('data-testid') ?? el.id;
      issues.push({ target: `${el.tagName.toLowerCase()}${id ? `#${id}` : ''} «${name}»`, width: Math.round(r.width), height: Math.round(r.height) });
    });
    return issues;
  });
}

for (const [width, scheme] of [
  [1366, 'light'],
  [375, 'dark'],
] as const) {
  test(`2.5.8 target size ≥ 24×24 (or spacing / inline exception) on every main screen — ${width} px, ${scheme}`, async ({ page, api }) => {
    test.slow();
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: scheme });
    const ids = seed(api);
    await login(page, api);
    const report: Record<string, TargetIssue[]> = {};
    for (const screen of SCREENS) {
      await openScreen(page, screen.path(ids));
      const issues = await targetSizeIssues(page);
      if (issues.length > 0) report[screen.name] = issues;
    }
    expect(report).toEqual({});
  });
}

test('2.4.11 focus not obscured: Shift+Tab from the end of a long screen never leaves the focused element under the sticky top bar', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 640 });
  const ids = seed(api);
  await login(page, api);
  for (const path of [`/w/${ids.ws.id}/dashboard`, `/w/${ids.ws.id}/settings/appearance`, `/w/${ids.ws.id}/agents`]) {
    await openScreen(page, path);
    // Последний фокусируемый элемент страницы, затем назад: браузер прокручивает к элементу у верхнего края.
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.locator('main').evaluate((main) => {
      const all = Array.from(main.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), input, select, textarea, [tabindex="0"]'));
      all.at(-1)?.focus();
    });
    const hidden: string[] = [];
    for (let i = 0; i < 40; i++) {
      await page.keyboard.press('Shift+Tab');
      const state = await page.evaluate(() => {
        const el = document.activeElement as HTMLElement | null;
        const bar = document.querySelector('[data-testid="topbar"]');
        if (!el || !bar || bar.contains(el) || !el.closest('main')) return null;
        const r = el.getBoundingClientRect();
        const barBottom = bar.getBoundingClientRect().bottom;
        // Скрыт целиком: нижний край не ниже нижнего края панели (или ниже окна).
        return r.bottom <= barBottom + 0.5 || r.top >= window.innerHeight ? `${el.tagName} «${(el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 40)}»` : '';
      });
      if (state) hidden.push(state);
    }
    expect(hidden, path).toEqual([]);
  }
});

test('2.4.11: the context panel overlay (< 1280 px) closes when focus moves into the main area, so it never covers the focused element', async ({ page, api }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  const { ws } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/mission-control`);
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeVisible();
  await page.getByRole('button', { name: 'Показать контекстную панель' }).click();
  const panel = page.getByTestId('context-panel');
  await expect(panel).toHaveAttribute('data-mode', 'overlay');
  await page.getByRole('link', { name: 'Все процессы' }).focus();
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Все процессы' })).toBeFocused();
});

test('3.2.6 consistent help: keyboard help and user menu sit at the same place of the top bar on every screen', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const ids = seed(api);
  await login(page, api);
  const positions = new Set<string>();
  for (const screen of SCREENS) {
    await openScreen(page, screen.path(ids));
    const bar = page.getByTestId('topbar');
    const help = bar.getByRole('button', { name: 'Сочетания клавиш' });
    await expect(help).toBeVisible();
    const order = await bar.evaluate((el) => {
      const buttons = Array.from(el.querySelectorAll('button')).filter((b) => b.getBoundingClientRect().width > 0);
      const helpIndex = buttons.findIndex((b) => b.getAttribute('aria-label') === 'Сочетания клавиш');
      // Справа от справки — только уведомления и профиль: одинаковый порядок и место на каждом экране.
      return { after: buttons.slice(helpIndex + 1).map((b) => b.getAttribute('aria-haspopup') ?? b.getAttribute('aria-label')), x: Math.round(buttons[helpIndex]!.getBoundingClientRect().right - window.innerWidth) };
    });
    positions.add(JSON.stringify(order));
  }
  expect(positions.size).toBe(1);
});

test('3.3.8 accessible authentication: no cognitive test; paste and password managers allowed (autocomplete, no paste blocking)', async ({ page, api }) => {
  api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  await page.goto('/login');
  const email = page.getByLabel('Электронная почта');
  const password = page.getByLabel('Пароль');
  await expect(email).toHaveAttribute('autocomplete', 'username');
  await expect(password).toHaveAttribute('autocomplete', 'current-password');
  // Вставка из буфера (как менеджер паролей / копирование): событие paste не отменяется, значение принимается.
  for (const [field, value] of [
    [email, api.user.email],
    [password, PASSWORD],
  ] as const) {
    const prevented = await field.evaluate((el, text) => {
      const data = new DataTransfer();
      data.setData('text/plain', text);
      const event = new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true });
      const ok = el.dispatchEvent(event);
      return !ok;
    }, value);
    expect(prevented).toBe(false);
    await field.fill(value);
  }
  await expect(page.locator('form').getByRole('img')).toHaveCount(0); // нет CAPTCHA/головоломок
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).not.toHaveURL(/\/login/);
});

test('2.5.7 dragging: Mission Control panel sizes change from a menu (single pointer, no drag); reset restores the default', async ({ page, api }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  const { ws, run } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/mission-control?run=${run.id}`);
  await expect(page.getByTestId('mc-layout')).toHaveAttribute('data-layout', 'three');
  const nav = page.locator('[data-panel]#mc-nav');
  const widthOf = () => nav.evaluate((el) => Math.round(el.getBoundingClientRect().width));
  const before = await widthOf();
  await page.getByRole('button', { name: 'Размеры панелей' }).click();
  await page.getByRole('menuitem', { name: 'Навигатор шире' }).click();
  await expect.poll(widthOf).toBeGreaterThan(before + 20);
  await page.getByRole('button', { name: 'Размеры панелей' }).click();
  await page.getByRole('menuitem', { name: 'Сбросить размеры панелей' }).click();
  await expect.poll(widthOf).toBe(before);
});

test('performance: CLI Output with 10 000 lines stays virtualized and scrolls smoothly (AICD_E2E_PERF=1)', async ({ page, api }) => {
  test.skip(!process.env.AICD_E2E_PERF, 'замер выполняется только при AICD_E2E_PERF=1');
  test.slow();
  await page.setViewportSize({ width: 1920, height: 1080 });
  const { ws, run, b } = seed(api);
  const LINES = 10_000;
  for (let i = 1; i <= LINES; i++) {
    api.addEvent(run, 'cli.output', {
      execution_id: '01900000-0000-7000-b000-000000000003',
      task_id: b.id,
      provider: 'codex',
      agent_role: 'developer',
      cli_seq: i,
      cli_event_type: 'output',
      stream: i % 10 === 0 ? 'stderr' : 'stdout',
      text: `[${String(i).padStart(5, '0')}] pytest tests/test_export.py::test_row_${i} PASSED — ${'x'.repeat(i % 60)}`,
    });
  }
  await login(page, api);
  const started = Date.now();
  await page.goto(`/w/${ws.id}/mission-control?run=${run.id}&view=cli`);
  const feed = page.getByRole('feed', { name: 'Вывод CLI' });
  await expect(feed.locator(`[aria-setsize="${LINES}"]`).first()).toBeAttached({ timeout: 60_000 });
  const loadMs = Date.now() - started;
  const domLines = await feed.getByTestId('cli-line').count();
  expect(domLines).toBeLessThan(120);
  // Прокрутка вверх-вниз по всей ленте в течение ~4 с; кадры считаются requestAnimationFrame.
  const metrics = await feed.evaluate(async (root) => {
    const el = root as HTMLElement;
    let target: HTMLElement = el;
    for (let node: HTMLElement | null = el; node; node = node.parentElement) {
      if (node.scrollHeight > node.clientHeight + 10 && getComputedStyle(node).overflowY !== 'visible') {
        target = node;
        break;
      }
    }
    if (target.scrollHeight <= target.clientHeight + 10) {
      for (const child of Array.from(el.querySelectorAll<HTMLElement>('*'))) {
        if (child.scrollHeight > child.clientHeight + 10 && getComputedStyle(child).overflowY !== 'visible') {
          target = child;
          break;
        }
      }
    }
    const max = target.scrollHeight - target.clientHeight;
    /** Кадры requestAnimationFrame, пока позиция прокрутки задаётся функцией времени. */
    async function measure(duration: number, position: (ms: number) => number) {
      const frames: number[] = [];
      const start = performance.now();
      await new Promise<void>((resolve) => {
        const step = (now: number) => {
          frames.push(now);
          target.scrollTop = position(now - start);
          if (now - start < duration) requestAnimationFrame(step);
          else resolve();
        };
        requestAnimationFrame(step);
      });
      const deltas = frames.slice(1).map((f, i) => f - frames[i]!);
      deltas.sort((x, y) => x - y);
      const total = frames.at(-1)! - frames[0]!;
      return {
        fps: Math.round(((frames.length - 1) / total) * 1000 * 10) / 10,
        p95FrameMs: Math.round(deltas[Math.floor(deltas.length * 0.95)]! * 10) / 10,
        maxFrameMs: Math.round(deltas.at(-1)! * 10) / 10,
      };
    }
    // 1) Равномерная быстрая прокрутка (≈ колесо/тачпад): 3000 px/с вверх от конца, 4 с.
    target.scrollTop = max;
    const steady = await measure(4000, (ms) => Math.max(0, max - ms * 3));
    // 2) Худший случай: перетаскивание ползунка по всей ленте (конец → начало → конец за 4 с) —
    //    каждый кадр — новое окно строк.
    const sweep = await measure(4000, (ms) => {
      const phase = ((ms / 4000) * 2) % 2;
      return max * (phase < 1 ? 1 - phase : phase - 1);
    });
    return { scrollHeight: target.scrollHeight, steady, sweep, domNodes: document.getElementsByTagName('*').length };
  });
  const result = { lines: LINES, loadMs, domLines, ...metrics };
  console.log(`CLI 10k perf: ${JSON.stringify(result)}`);
  // Критерий плана F4: прокрутка 10 000 строк без деградации — ≥ 50 fps при равномерной прокрутке.
  // Протяжка ползунка по всей ленте — худший случай: порог ниже (≥ 30 fps), фиксирует отсутствие «залипаний».
  expect(metrics.steady.fps, 'CLI 10k: равномерная прокрутка ≥ 50 fps').toBeGreaterThanOrEqual(50);
  expect(metrics.sweep.fps, 'CLI 10k: протяжка по всей ленте ≥ 30 fps').toBeGreaterThanOrEqual(30);
  const out = process.env.AICD_E2E_AUDIT_DIR;
  if (out) {
    mkdirSync(out, { recursive: true });
    writeFileSync(join(out, 'cli-10k-perf.json'), JSON.stringify(result, null, 2));
  }
  expect(metrics.scrollHeight).toBeGreaterThan(10_000 * 10);
  // Критерий F4/F9: ≥ 50 fps при прокрутке (замер на машине прогона, без параллельной нагрузки).
  expect(metrics.steady.fps).toBeGreaterThanOrEqual(50);
});

test('screen audit: every main screen at 1920/1366/768/375 in dark and light (AICD_E2E_AUDIT_DIR)', async ({ page, api }) => {
  const dir = process.env.AICD_E2E_AUDIT_DIR;
  test.skip(!dir, 'скриншоты аудита — только при AICD_E2E_AUDIT_DIR');
  test.setTimeout(15 * 60_000);
  const ids = seed(api);
  await login(page, api);
  mkdirSync(dir!, { recursive: true });
  const overflow: string[] = [];
  for (const scheme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    for (const width of [1920, 1366, 768, 375] as const) {
      await page.setViewportSize({ width, height: width === 375 ? 812 : width === 768 ? 1024 : 1080 });
      for (const screen of SCREENS) {
        await openScreen(page, screen.path(ids));
        const extra = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        if (extra > 0) overflow.push(`${screen.name} ${width} ${scheme}: +${extra}px`);
        await page.screenshot({ path: join(dir!, `${screen.name}-${width}-${scheme}.png`), fullPage: true });
      }
    }
  }
  expect(overflow).toEqual([]);
});

