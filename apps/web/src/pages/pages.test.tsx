import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../test/utils';

const projectsPath = `/w/${WS_ID}/projects`;

describe('auth', () => {
  it('redirects an anonymous user to the login page', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => problem(401, 'unauthenticated') });
    renderApp(projectsPath);
    expect(await screen.findByRole('heading', { level: 1, name: 'Вход' })).toHaveFocus();
  });

  it('shows a generic error for wrong credentials and keeps field errors accessible', async () => {
    const user = userEvent.setup();
    mockFetch({
      'GET /api/v1/auth/session': () => problem(401, 'unauthenticated'),
      'POST /api/v1/auth/login': () => problem(401, 'unauthenticated'),
    });
    renderApp('/login');
    await user.click(await screen.findByRole('button', { name: 'Войти' }));
    expect(screen.getByLabelText('Электронная почта')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Электронная почта')).toHaveAccessibleDescription('Обязательное поле.');

    await user.type(screen.getByLabelText('Электронная почта'), 'anna@example.com');
    await user.type(screen.getByLabelText('Пароль'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Войти' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Неверная электронная почта или пароль.');
  });

  it('logs in and lands on the dashboard of the first workspace', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => problem(401, 'unauthenticated'),
      'POST /api/v1/auth/login': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [project(1)], next_cursor: null }),
      'GET /api/v1/runs': () => json(200, { items: [], next_cursor: null }),
    });
    renderApp('/login');
    await user.type(await screen.findByLabelText('Электронная почта'), 'anna@example.com');
    await user.type(screen.getByLabelText('Пароль'), 'secret{Enter}');
    expect(await screen.findByRole('heading', { level: 1, name: 'Обзор · Команда 1' })).toBeInTheDocument();
    expect(await screen.findByText('Последние запуски', { selector: 'h2' })).toBeInTheDocument();
    const list = calls.find((c) => c.url.pathname === '/api/v1/projects');
    expect(list?.headers.get('X-Workspace-Id')).toBe(WS_ID);
  });
});

describe('projects list states', () => {
  it('shows loading, then the empty state with a create action for owners', async () => {
    let release: (r: Response) => void = () => undefined;
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => new Promise<Response>((r) => (release = r)),
    });
    renderApp(projectsPath);
    expect(await screen.findByText('Загрузка проектов…')).toBeInTheDocument();
    expect(screen.getByText('Загрузка проектов…').closest('[role="status"]')).not.toBeNull();
    release(json(200, { items: [], next_cursor: null }));
    const empty = await screen.findByTestId('empty-state');
    expect(within(empty).getByRole('heading', { name: 'Проектов пока нет' })).toBeInTheDocument();
    expect(within(empty).getByRole('link', { name: 'Создать проект' })).toHaveAttribute('href', `${projectsPath}/new`);
  });

  it('shows the error state and recovers on retry', async () => {
    const user = userEvent.setup();
    let fail = true;
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () =>
        fail ? problem(500, 'internal_error', { request_id: 'trace-42' }) : json(200, { items: [project(1)], next_cursor: null }),
    });
    renderApp(projectsPath);
    const alert = await screen.findByTestId('error-state');
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert).toHaveTextContent('Внутренняя ошибка сервера');
    expect(alert).toHaveTextContent('trace-42');
    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('Проект 1')).toBeInTheDocument();
  });

  it('loads the next page with the cursor ("Загрузить ещё")', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': (req) =>
        req.url.searchParams.get('cursor') === 'c2'
          ? json(200, { items: [project(3)], next_cursor: null })
          : json(200, { items: [project(1), project(2)], next_cursor: 'c2' }),
    });
    renderApp(projectsPath);
    await user.click(await screen.findByRole('button', { name: 'Загрузить ещё' }));
    expect(await screen.findByText('Проект 3')).toBeInTheDocument();
    expect(screen.getAllByRole('listitem').filter((li) => li.closest('[aria-label="Список проектов"]'))).toHaveLength(3);
    expect(screen.queryByRole('button', { name: 'Загрузить ещё' })).not.toBeInTheDocument();
    expect(calls.filter((c) => c.url.pathname === '/api/v1/projects').map((c) => c.url.searchParams.get('cursor'))).toEqual([
      null,
      'c2',
    ]);
  });

  it.each(['developer', 'reviewer', 'viewer'] as const)('hides "Создать проект" for role %s', async (role) => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session(role)),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
    });
    renderApp(projectsPath);
    expect(await screen.findByTestId('empty-state')).toHaveTextContent('Создавать проекты могут владелец');
    expect(screen.queryByRole('link', { name: 'Создать проект' })).not.toBeInTheDocument();
  });

  it('shows "workspace not found" for a workspace without membership', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('owner')) });
    renderApp('/w/01900000-0000-7000-8000-0000000000ff/projects');
    expect(await screen.findByRole('heading', { level: 1, name: 'Рабочее пространство не найдено' })).toBeInTheDocument();
  });
});

describe('workspace create form', () => {
  it('validates the slug against the contract pattern without calling the API', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('viewer')) });
    renderApp('/workspaces/new');
    await user.type(await screen.findByLabelText('Название'), 'Команда');
    await user.type(screen.getByLabelText('Slug'), 'Bad_Slug');
    await user.click(screen.getByRole('button', { name: 'Создать' }));
    const slug = screen.getByLabelText('Slug');
    expect(slug).toHaveAttribute('aria-invalid', 'true');
    await waitFor(() => expect(slug).toHaveFocus());
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });

  it('shows server field errors by JSON pointer and slug conflicts on the slug field', async () => {
    const user = userEvent.setup();
    let attempt = 0;
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'POST /api/v1/workspaces': () => {
        attempt += 1;
        return attempt === 1
          ? problem(422, 'validation_error', { errors: [{ pointer: '/name', code: 'reserved', message: 'Имя зарезервировано' }] })
          : problem(409, 'conflict');
      },
    });
    renderApp('/workspaces/new');
    await user.type(await screen.findByLabelText('Название'), 'admin');
    await user.type(screen.getByLabelText('Slug'), 'admin');
    await user.click(screen.getByRole('button', { name: 'Создать' }));
    expect(await screen.findByText('Имя зарезервировано')).toBeInTheDocument();
    expect(screen.getByLabelText('Название')).toHaveAccessibleDescription('Имя зарезервировано');

    await user.click(screen.getByRole('button', { name: 'Создать' }));
    expect(await screen.findByText('Этот slug уже занят.')).toBeInTheDocument();
    const posts = calls.filter((c) => c.method === 'POST');
    // Тот же ввод — то же намерение: ключ переиспользуется.
    expect(posts[0]?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(posts[1]?.headers.get('Idempotency-Key')).toBe(posts[0]?.headers.get('Idempotency-Key'));
    expect(posts[0]?.headers.get('X-CSRF-Token')).toBe('csrf-from-session');
  });
});

describe('project create form', () => {
  it('sends contract defaults SMART/strict and returns to the list', async () => {
    const user = userEvent.setup();
    const created = project(7, { name: 'Новый' });
    let listed = false;
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'POST /api/v1/projects': () => {
        listed = true;
        return json(201, created);
      },
      'GET /api/v1/projects': () => json(200, { items: listed ? [created] : [], next_cursor: null }),
    });
    renderApp(`${projectsPath}/new`);
    expect(await screen.findByRole('radio', { name: 'Умный (SMART)' })).toBeChecked();
    expect(screen.getByRole('radio', { name: 'Строгий (strict)' })).toBeChecked();
    await user.type(screen.getByLabelText('Название'), '  Новый  ');
    await user.click(screen.getByRole('button', { name: 'Создать' }));
    expect(await screen.findByText('Проект «Новый» создан.')).toBeInTheDocument();
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.body).toEqual({ name: 'Новый', documentation_mode: 'SMART', quality_profile: 'strict' });
    expect(post?.headers.get('X-Workspace-Id')).toBe(WS_ID);
    expect(post?.headers.get('Idempotency-Key')).toMatch(/^[A-Za-z0-9._:-]{8,255}$/);
  });

  it('maps problem errors[].pointer to fields and shows forbidden as a form error', async () => {
    const user = userEvent.setup();
    let n = 0;
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'POST /api/v1/projects': () => {
        n += 1;
        return n === 1
          ? problem(422, 'validation_error', {
              errors: [
                { pointer: '/quality_profile', code: 'enum', message: 'Недопустимый профиль' },
                { pointer: '/unknown', code: 'x', message: 'Прочая ошибка' },
              ],
            })
          : problem(403, 'forbidden');
      },
    });
    renderApp(`${projectsPath}/new`);
    await user.type(await screen.findByLabelText('Название'), 'P');
    await user.click(screen.getByRole('radio', { name: 'Критичный (critical)' }));
    await user.click(screen.getByRole('button', { name: 'Создать' }));
    expect(await screen.findByText('Недопустимый профиль')).toBeInTheDocument();
    expect(screen.getByTestId('form-alert')).toHaveTextContent('Прочая ошибка');
    await user.click(screen.getByRole('button', { name: 'Создать' }));
    expect(await screen.findByText('Недостаточно прав для этого действия.')).toBeInTheDocument();
  });

  it('does not render the form for roles without project:create', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('developer')) });
    renderApp(`${projectsPath}/new`);
    expect(await screen.findByText(/нет права создавать проекты/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Создать' })).not.toBeInTheDocument();
  });
});

describe('layout', () => {
  it('has a skip link, grouped sidebar nav for all Mission Control sections, workspace switcher and user menu', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner', 2)),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'POST /api/v1/auth/logout': () => new Response(null, { status: 204 }),
    });
    renderApp(projectsPath);
    await screen.findByTestId('empty-state');
    expect(screen.getByRole('link', { name: 'Перейти к основному содержимому' })).toHaveAttribute('href', '#main');
    const nav = screen.getByRole('navigation', { name: 'Основная навигация' });
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual([
      'Обзор',
      'Проекты',
      'Запуски',
      'Одобрения',
      'Evidence и качество',
      'Стандарты и соответствие',
      'Знания и документация',
      'Аналитика',
      'CLI-провайдеры',
      'Интеграции',
      'Настройки',
      'Аудит',
      'Рабочие пространства',
    ]);
    expect(within(nav).getByRole('list', { name: 'Администрирование' })).toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Проекты' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Аудит' })).toHaveAttribute('href', `/w/${WS_ID}/audit`);

    const switcher = screen.getByRole('button', { name: /Рабочее пространство/ });
    await user.click(switcher);
    expect(switcher).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('link', { name: /Команда 2/ })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(switcher).toHaveAttribute('aria-expanded', 'false');
    expect(switcher).toHaveFocus();

    await user.click(screen.getByRole('button', { name: /Меню пользователя/ }));
    await user.click(await screen.findByRole('menuitem', { name: 'Выйти' }));
    expect(await screen.findByText('Вы вышли из системы.')).toBeInTheDocument();
    expect(calls.find((c) => c.url.pathname === '/api/v1/auth/logout')?.headers.get('X-CSRF-Token')).toBe('csrf-from-session');
  });

  it('hides sections the role cannot access and shows a forbidden page on direct navigation', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('viewer')) });
    renderApp(`/w/${WS_ID}/audit`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Недостаточно прав' })).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Основная навигация' });
    expect(within(nav).queryByRole('link', { name: 'Аудит' })).not.toBeInTheDocument();
    expect(within(nav).getByRole('link', { name: 'Одобрения' })).toBeInTheDocument();
  });

  // Разделы approvals/quality/compliance реализованы (P7-10/P7-11): их экраны — в своих тестах и e2e.
  it('renders 404 for unknown paths inside a workspace', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('developer')) });
    renderApp(`/w/${WS_ID}/no-such-screen`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Страница не найдена' })).toBeInTheDocument();
    expect(screen.queryByTestId('area-placeholder')).not.toBeInTheDocument();
  });

  it('opens the keyboard shortcuts dialog with "?" and navigates with "g" sequences', async () => {
    const user = userEvent.setup();
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
    });
    renderApp(projectsPath);
    await screen.findByTestId('empty-state');
    await user.keyboard('?');
    const dialog = await screen.findByRole('dialog', { name: 'Сочетания клавиш' });
    expect(within(dialog).getByRole('table', { name: 'Сочетания клавиш Mission Control' })).toBeInTheDocument();
    expect(within(dialog).getByText('Перейти: Одобрения')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await user.keyboard('ga');
    expect(await screen.findByRole('heading', { level: 1, name: 'Одобрения' })).toBeInTheDocument();

    // Отключение односимвольных сочетаний (WCAG 2.1.4).
    await user.click(screen.getByRole('button', { name: 'Сочетания клавиш' }));
    await user.click(await screen.findByRole('checkbox', { name: 'Использовать односимвольные сочетания клавиш' }));
    await user.keyboard('{Escape}');
    await user.keyboard('gp');
    expect(screen.getByRole('heading', { level: 1, name: 'Одобрения' })).toBeInTheDocument();
  });

  it('renders the 404 page for unknown paths inside a workspace', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('owner')) });
    renderApp(`/w/${WS_ID}/no-such-screen`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Страница не найдена' })).toBeInTheDocument();
  });
});
