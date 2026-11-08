# apps/web — Mission Control (P1-13, P3-15, P7-01, P7-02)

SPA AiCodingDev: вход, каркас Mission Control (боковая навигация по всем разделам Phase 7, переключатель workspace, клавиатурные сочетания, светлая/тёмная тема), рабочие пространства, проекты, запуски и экран run с лентой событий SSE. Экраны Phase 7, ещё не реализованные, открываются как заглушки «Раздел в разработке». Архитектура — ADR-0014; правила API — `docs/api/API_DESIGN.md`; контракт — `packages/contracts/openapi.yaml` (единственный источник, ADR-0013).

## Команды (pnpm, Node ≥ 22.12)

| Команда | Что делает |
|---|---|
| `pnpm dev` | Vite dev server `127.0.0.1:5173`; `/api` проксируется на `AICD_API_URL` (по умолчанию `http://127.0.0.1:8000`) |
| `pnpm build` / `pnpm preview` | Production-сборка в `dist/` / просмотр на `127.0.0.1:4173` (тот же прокси) |
| `pnpm typecheck` | `tsc --noEmit` (strict, `noUncheckedIndexedAccess`) для приложения и конфигов/E2E |
| `pnpm lint` | ESLint flat config: typescript-eslint + jsx-a11y (strict) + react-hooks |
| `pnpm test` | Vitest + Testing Library (клиент API, формы, состояния) |
| `pnpm e2e` | Playwright против `vite preview` с моками `page.route`, валидируемыми по OpenAPI |
| `pnpm e2e:live` | Live E2E без моков backend: smoke против `AICD_E2E_LIVE_URL`; с `AICD_E2E_LIVE_STACK=1` — поднимает стенд (API + воркеры + PostgreSQL + Redis + S3 + Fake CLI) и прогоняет AC-05/AC-06/AC-08 (раздел «Live E2E») |
| `pnpm gen:api` | `openapi-typescript` → `src/api/schema.d.ts` из `../../packages/contracts/openapi.yaml` |
| `pnpm check:api` | Drift check для CI: генерирует во временный файл и падает, если `schema.d.ts` устарел |

## Структура

```
src/
  App.tsx                 дерево маршрутов из реестра областей + провайдеры (Query, SSE, Tooltip, Toast)
  routes/<area>.tsx       область = маршруты + пункт навигации (AreaModule); routes/index.ts — список областей
  layout/                 AppLayout (skip link, sidebar/drawer, шапка), Sidebar, WorkspaceSwitcher (Popover),
                          UserMenu (DropdownMenu: тема, выход), ShortcutsDialog, WorkspaceGuard (/w/:workspaceId)
  pages/                  страницы; AreaPlaceholderPage (заглушка), NotFoundPage (404), ForbiddenPage (403)
  components/ui/          дизайн-система (импорт из components/ui): Button/ButtonLink/IconButton, Badge/Callout,
                          Card, Table (сортируемые заголовки), CodeBlock, Timestamp/RelativeTime, Toolbar, Kbd, Icon,
                          Dialog, Menu (DropdownMenu), Tabs, Tooltip, Popover, Toast (useToast)
  components/states.tsx   LoadingState/Skeleton, EmptyState, ErrorState, ForbiddenState, UnderConstructionState
  components/             LoadMore (cursor-пагинация), StatusBadge/statusTone, PageHeading, form
  styles/tokens.css       дизайн-токены (CSS-переменные; светлая/тёмная тема)
  api/client.ts           ядро транспорта: fetch, CSRF, Idempotency-Key, If-Match, problem+json, openEventStream;
                          requestWithMeta (тело + ETag ответа → If-Match: `ifMatch` приоритетнее `ifMatchVersion`),
                          requestText (не-JSON представление по Accept, например отчёт run text/markdown)
  api/<area>.ts           операции области: auth, workspaces, projects, runs (+ ключи TanStack Query *Keys)
  api/sse.ts              парсер text/event-stream, дедуп, EventStream (одно соединение с возобновлением)
  api/streamManager.ts    StreamManager: общие потоки, ref-counting, лимит 5, offline/online, скрытая вкладка
  api/useEventStream.tsx  хук + провайдер менеджера; api/useRunEvents.ts / runEvents.ts — поток run;
                          api/workspaceEvents.ts — поток уведомлений workspace (resume 'cursor': Last-Event-ID = id:,
                          дедуп по event_id; инвалидация экранов — useWorkspaceInvalidation в features/quality/live.tsx)
  i18n/<area>.json        строки UI (namespace = имя файла), i18n/index.ts подхватывает файлы сам
  lib/                    permissions (полный каталог прав §5.1), theme, shortcuts, format, problem, storage
```

### Как добавить экран (параллельная работа без правки общих файлов)

1. **Маршрут и меню.** Заменить содержимое своего `src/routes/<area>.tsx` (файлы есть для всех областей Phase 7: dashboard, projects, runs, chat, tasks, approvals, quality, compliance, knowledge, providers, settings, audit): `workspaceRoutes` — пути относительно `/w/:workspaceId/` (членство уже проверено `WorkspaceGuard`), `nav` — пункт меню (группа, порядок, буква `g`-сочетания, `permission`). Право по роли — `<RequirePermission permission="…">`. Тяжёлые экраны (React Flow, Monaco) — `lazy(() => import(...))`: `Suspense` уже есть в `AppRoutes`. Экран «домой» после входа — `home: { path, priority }` (сейчас projects с priority 0; Dashboard ставит больше). Новая область (которой нет в списке) — новый файл + одна строка в `routes/index.ts`.
2. **Страницы** — `src/pages/<area>/…` (или `src/features/<area>/`), компоненты — из `components/ui` и `components/states.tsx`; заголовок — `PageHeading` (title документа + фокус на h1).
3. **API** — новый файл `src/api/<area>.ts` по образцу `runs.ts`: типы из `Schemas[...]`/`JsonBody<'op'>`/`JsonResponse<'op', 200>`/`Query<'op'>`, вызовы через `request()`, ключи кэша `<area>Keys`. `client.ts` не меняется.
4. **Строки** — только в своём `src/i18n/<area>.json`, под группой с именем области (`{ "approvals": { … } }`): ключ `approvals.title` работает через `t()` из любого компонента. Свои перечисления — `<area>.enums.*` (общие `enums.*` в common.json не трогать).
5. **События run** — `useRunEvents(runId, { filters, onEvents, onResync })`: экраны одного run (граф, чат, лента) делят одно соединение; фильтр `types`/`task_id` — отдельный поток.
6. **Тесты** — unit рядом с кодом; E2E — свой `e2e/mocked/<area>.spec.ts`: операции своей области регистрируются в мок прямо из spec (`api.route(method, path, operationId, handler)` — ответы валидируются по контракту, `mockApi.ts` не меняется), вход — `login()` из `e2e/support/session.ts`, a11y — `expectNoSeriousA11yViolations`.

### Дизайн-система (P7-01)

- Только семантические цвета Tailwind (`bg-surface`, `text-fg`, `text-fg-muted`, `border-border`, `bg-accent`, `text-link`, `{neutral|info|success|warning|danger}-{soft,border,fg}`, `text-danger-text`) — значения в `styles/tokens.css`. Тема: `prefers-color-scheme`, принудительно — `<html data-theme>` (меню пользователя, `lib/theme.ts`). Контраст AA для всех пар текст/фон и ≥ 3:1 для границ полей и кольца фокуса проверяет `styles/tokens.test.ts`.
- Фокус: видимое кольцо 3px (`--color-focus`) на `:focus-visible`; после перехода на другой экран (смена pathname) фокус на h1, смена query (вкладка, фильтр) фокус не трогает; диалоги возвращают фокус открывшему элементу; цели касания ≥ 44px (`min-h-touch`).
- Клавиатура: `?` — справка, `g` + буква — раздел, Esc — закрыть слой; односимвольные сочетания отключаются в справке (WCAG 2.1.4). Мобильная навигация (< 768px) — Dialog-drawer.
- Radix-обёртки задают доступные имена и классы; меню немодальные (модальный режим Radix скрывает `#root` через aria-hidden → axe `aria-hidden-focus`).

### SSE (P7-02)

`StreamManager` (один на вкладку) держит по одному `EventStream` на (путь + фильтры). Подписчики делят соединение (ref-counting, закрытие через 1,5 с после последней отписки), поздний подписчик получает буфер принятых событий (до 1000). Не более 5 одновременных потоков (API_DESIGN §13: 6-й ⇒ 429) — лишние ждут в очереди (`queued`). `EventStream`: `Last-Event-ID` = последний принятый seq при любом переподключении, дедуп по seq/event_id, `retry:` сервера, экспоненциальный backoff с джиттером при ошибках подряд (429/503 — по Retry-After), watchdog 45 с без байтов (heartbeat 15 с), `410 replay_window_exceeded` → `onResync` (перечитать REST) и продолжение с `after_seq = last_seq` (OQ-35, вариант a), 401/403/404 — `failed`. `offline` разрывает потоки (без попыток), `online` переподключает сразу; вкладка скрыта > 60 с — потоки на паузе (`paused`), при возврате — возобновление.

### Тесты

- `src/api/client.test.ts`, `runEvents.test.ts`, `streamManager.test.ts`, `useRunEvents.test.tsx` — транспорт, парсер, дедуп, реконнект, ref-counting, лимит, offline/online, видимость, 410, restart.
- `src/components/ui/ui.test.tsx` — Table (сортировка, aria-sort), Toolbar (roving tabindex), Dialog/Menu/Tabs/Toast, CodeBlock, время; `src/i18n/i18n.test.ts`, `src/routes/routes.test.ts`, `src/styles/tokens.test.ts`.
- `e2e/mocked/layout.spec.ts` — axe (serious/critical) в светлой и тёмной теме для layout, всех заглушек, 404/403, диалога справки, меню и переключателя; клавиатура через sidebar и диалоги; мобильный drawer; тема.
- `e2e/mocked/sse.spec.ts` — обрыв сети (`context.setOffline`): поток `offline` без попыток подключения, после `online` — `Last-Event-ID`, сервер повторяет часть событий, в ленте каждое событие ровно один раз и без пропусков; обрыв посреди кадра.
- `e2e/support/contract.ts` — ajv (JSON Schema 2020-12) по схемам самого контракта: тела запросов UI, обязательные заголовки, CSRF и тела ответов моков; SSE-кадры мока — по `packages/contracts/schemas/events/1.0/sse-event.schema.json`; нарушения валят тест.

Права в UI (скрытие пунктов меню и действий) — только UX; авторизацию выполняет сервер.

## Зависимости (точные версии; обоснование)

Runtime:
- `react`, `react-dom` 18.3.1 — ADR-0014/ТЗ §3.
- `react-router` 7.18.4 — маршрутизация (ADR-0014); v8 требует React 19.
- `@tanstack/react-query` 5.104.5 — кэш/загрузка данных, infinite queries для cursor-пагинации (ADR-0014).
- `i18next` 26.5.7, `react-i18next` 17.1.5 — i18n, ключи вместо литералов (ADR-0014).
- Radix UI primitives (ADR-0014 п.5; React 16.8–19; только используемые пакеты):
  - `@radix-ui/react-dialog` 1.3.1 — модальные диалоги (справка по клавишам, мобильная навигация; далее — подтверждения директив/одобрений): ловушка фокуса, aria-modal, возврат фокуса;
  - `@radix-ui/react-dropdown-menu` 2.2.1 — меню пользователя (APG Menu Button, radio-пункты темы); далее — меню действий строк;
  - `@radix-ui/react-popover` 1.3.1 — переключатель workspace (список ссылок), далее — фильтры чата/таблиц;
  - `@radix-ui/react-tooltip` 1.4.1 — подсказки кнопок-иконок (видны при фокусе, Escape скрывает — WCAG 1.4.13);
  - `@radix-ui/react-tabs` 1.2.1 — вкладки внутри экрана (Task Detail, Evidence, Compliance);
  - `@radix-ui/react-toast` 1.3.1 — уведомления (live region, F8, пауза таймера) для статусов команд/директив.

Dev:
- `vite` 7.3.7, `@vitejs/plugin-react` 5.2.0 — сборка/dev server (ТЗ §3).
- `typescript` 5.9.3 — strict TS (5.x: требование `openapi-typescript` и `typescript-eslint`).
- `tailwindcss` 3.4.19, `postcss` 8.5.31, `autoprefixer` 10.6.1 — Tailwind 3 (ТЗ §3).
- `openapi-typescript` 7.13.0 — генерация типов из контракта (ADR-0013).
- `vitest` 4.1.12, `jsdom` 27.4.0, `@testing-library/react` 16.3.3, `@testing-library/dom` 10.4.2, `@testing-library/user-event` 14.6.8, `@testing-library/jest-dom` 6.10.0 — unit-тесты (ADR-0014).
- `@playwright/test` 1.63.0, `@axe-core/playwright` 4.14.0 — E2E и проверка a11y (ADR-0014).
- `ajv` 8.20.0, `ajv-formats` 3.0.1, `yaml` 2.9.1 — валидация моков E2E по OpenAPI-схемам.
- `eslint` 9.39.5, `@eslint/js` 9.39.5, `typescript-eslint` 8.72.0, `eslint-plugin-jsx-a11y` 6.10.2, `eslint-plugin-react-hooks` 7.1.1, `globals` 16.5.0 — линтинг (eslint 9: jsx-a11y не поддерживает 10).
- `@types/react` 18.3.31, `@types/react-dom` 18.3.7, `@types/node` 22.20.5 — типы.

Добавлены экранами P7-05..P7-08 (точные версии, выпущены ≥ 2 недель назад, React 18; загружаются лениво — в начальный бандл не входят):
- `@xyflow/react` 12.12.0 — Run Graph (ADR-0014): DAG задач; холст — отдельный чанк `TaskGraphCanvas` (~187 kB / 62 kB gzip) вместе со стилями. Анимированные рёбра не используются (prefers-reduced-motion, axe ждёт завершения анимаций).
- `@monaco-editor/react` 4.7.0 + `monaco-editor` 0.57.0 — read-only diff viewer Task Detail (ADR-0014). Monaco берётся из локального пакета (`loader.config({ monaco })` с `monaco-editor/editor/editor.api`), без CDN; editor worker — отдельный файл того же origin (`?worker`, совместимо с CSP `script-src 'self'`). Чанк `MonacoDiff` (~2,7 MB / 713 kB gzip + worker 276 kB) грузится только на вкладке «Изменения».

Не добавлены: `openapi-fetch` — свой клиент закрывает CSRF/idempotency/problem; иконки — свой набор SVG (`components/ui/Icon.tsx`) без зависимости; Radix Toolbar — `Toolbar` на roving tabindex в ~60 строк.

## Ошибки по контракту

С P1-18 (OQ-31) каждая операция объявляет `500 internal_error` и `503 service_unavailable`; `Problem.code` — открытая строка (известные коды — `x-known-codes`), клиент обрабатывает неизвестный код по HTTP-статусу. `FieldError.pointer`: JSON Pointer относительно корня тела (`/name`) либо `query:<имя>` / `header:<имя>`. E2E-мок ошибки списка проектов отвечает `500 internal_error`.

## Live E2E

Без моков backend; SPA и API на одном origin (cookie `SameSite=Strict`, без CORS).

**Стенд из коробки (P7-14, AC-05/AC-06/AC-08).** Конфиг собирает SPA в `dist-live/` и запускает `tests/support/web_live_stack.py`: отдельная временная база PostgreSQL 17 (миграции Alembic, удаляется по завершении), API (`create_app`) и SPA на `127.0.0.1:4190` (как nginx: `/api/` → API, остальное — статика с fallback на `index.html`, те же заголовки и CSP), воркеры `aicd-worker outbox | orchestrator | exec` (Dramatiq, Redis БД 12), Fake CLI (сценарий `agent`: developer → REVIEW_REQUEST → независимый reviewer), gates — локальный runner без gates «только в контейнере», артефакты — S3 (MinIO, отдельный bucket) при `AICD_E2E_S3_ENDPOINT`, иначе FS. Управляющий HTTP стенда (`127.0.0.1:4191`) меняет только темп и инфраструктуру: остановка/запуск воркера исполнения, SIGKILL/запуск процесса API.

```bash
# из apps/web; нужны uv, Redis, PostgreSQL 17 (суперпользователь) и, по желанию, MinIO
export UV_SYSTEM_CERTS=1 NO_PROXY=127.0.0.1,localhost
AICD_E2E_LIVE_STACK=1 \
AICD_TEST_DATABASE_ADMIN_URL=postgresql://aicd_admin:…@127.0.0.1:55432/postgres \
AICD_E2E_S3_ENDPOINT=http://127.0.0.1:59000 AICD_E2E_S3_ACCESS_KEY=… AICD_E2E_S3_SECRET_KEY=… \
pnpm e2e:live
```

Порты — `AICD_E2E_LIVE_PORT` / `AICD_E2E_LIVE_CONTROL_PORT`, Redis — `AICD_E2E_REDIS_URL`. Стенд можно поднять отдельно (`uv run python -m tests.support.web_live_stack --keep` из корня; логи процессов — в выводе) и прогонять против него: `AICD_E2E_LIVE_URL=http://127.0.0.1:4190 AICD_E2E_LIVE_CONTROL_URL=http://127.0.0.1:4191 AICD_E2E_EMAIL=e2e-operator@example.test AICD_E2E_PASSWORD=Correct-Horse-Battery-9 pnpm e2e:live`.

`e2e/live/mission-control.spec.ts`:
- **AC-05** — DEVELOP run; чат и граф открыты до начала исполнения: handoff developer → reviewer (`Запрос ревью`) и итог ревью появляются в ленте без перезагрузки, статусы узлов графа меняются live до SUCCEEDED, axe.
- **AC-06** — поручения из чата: PAUSE/RESUME (доставлено → подтверждено → выполнено, run → PAUSED → RUNNING); CHANGE_REQUEST: impact preview (анализ на воркере) → подтверждение → checkpoint → «План v2» → DISPATCHED, задачи замены исполняются до COMPLETED.
- **AC-08** — процесс API убит посреди run, run продолжается; после подъёма API лента содержит каждое событие ровно один раз, seq 1..N без пропусков, переподключение — с `Last-Event-ID` ≥ последнего принятого seq.

**Внешний стенд (smoke).** `AICD_E2E_LIVE_URL=https://aicd.local AICD_E2E_EMAIL=... AICD_E2E_PASSWORD=... pnpm e2e:live` — `smoke.spec.ts` создаёт workspace `e2e-<suffix>` и проект; сценарии P7-14 без `AICD_E2E_LIVE_CONTROL_URL` пропускаются.
