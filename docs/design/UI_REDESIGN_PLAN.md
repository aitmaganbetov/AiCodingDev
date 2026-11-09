# UI Redesign Plan — «AiCodingDev — Neural Engineering Command Center»

**Статус:** утверждён владельцем 2026-11-09 (решения D-1…D-11 — [OPEN_QUESTIONS.md](../planning/OPEN_QUESTIONS.md)). Реализация: **F1, F2 — выполнены** (2026-11-09, см. «Статус фаз» в 7.1); F3–F9 — не начаты.
**Дата:** 2026-11-08
**Основания:** ТЗ §1, §3, §4, §6, §8, §11–§14, §19; [ADR-0014](../architecture/adr/ADR-0014-frontend-architecture.md); [AGENT_ROLES.md](../agents/AGENT_ROLES.md); [QUALITY_GATES.md](../agents/QUALITY_GATES.md); [API_DESIGN.md](../api/API_DESIGN.md); `packages/contracts/openapi.yaml` (ADR-0013 — единственный источник контракта); `apps/web/README.md`; `infrastructure/docker/web/nginx.conf`; [OPEN_QUESTIONS.md](../planning/OPEN_QUESTIONS.md) (OQ-44, OQ-45, OQ-50).

**Принципы, не подлежащие изменению редизайном:**
1. API-контракты, бизнес-логика, FSM, RBAC и транспорт (`api/client.ts`, `StreamManager`) сохраняются; редизайн — слой представления.
2. **Нет фиктивных данных под видом реальных.** Виджет без источника данных либо не показывается, либо показывает явное состояние «Нет источника данных (требуется API …)». Моки — только в `e2e/mocked` и Storybook-подобных фикстурах с пометкой `MOCK — только для разработки UI`.
3. Evidence-first: UI никогда не показывает «успех» без проверенного результата (ТЗ §14, AC-11). «Доставлено» ≠ «выполнено» (ТЗ §11).
4. Изменения backend (новые операции контракта) — только после утверждения владельцем (раздел 6, фазы B*).
5. Не копировать UI других продуктов; собственная визуальная система на основе палитры владельца и логотипа.

---

## Содержание

1. [Краткие выводы](#1-краткие-выводы)
2. [Аудит текущего frontend](#2-аудит-текущего-frontend)
3. [Матрица «требование → источник данных»](#3-матрица-требование--источник-данных)
4. [Конфликты и риски](#4-конфликты-и-риски)
5. [Спецификация дизайн-системы](#5-спецификация-дизайн-системы)
6. [Карта экранов, IA и ключевые сценарии](#6-карта-экранов-ia-и-ключевые-сценарии)
7. [Поэтапный план реализации](#7-поэтапный-план-реализации)
8. [Решения, требуемые от владельца](#8-решения-требуемые-от-владельца)

---

## 1. Краткие выводы

| # | Вывод | Следствие |
|---|---|---|
| 1 | Frontend уже зрелый: 15 областей, ~29,7 тыс. строк TS/TSX, SSE-менеджер с дедупом/реконнектом, RBAC-скрытие, i18n, 19 mocked E2E-спеков с axe (светлая/тёмная), live E2E AC-05/06/08. | Редизайн = **реструктуризация оболочки + новая визуальная система + новые композиции экранов** поверх существующих API-модулей и фич; не переписывание. |
| 2 | Агентские роли в репозитории однозначны: 12 ролей `AgentRole` (`requirements, architect, planner, developer, reviewer, security, qa, database, performance, devops, maintenance, documentation`) — ТЗ §4 = AGENT_ROLES.md = контракт. В запросе на редизайн имена ролей **не перечислены**. | Экран Agents строится строго по `AgentRole`. Любые «персонажи» вне списка (Orchestrator, Router, Verifier и т. п.) — это компоненты платформы, а не агенты; показывать их как агентов нельзя (см. 4.6). |
| 3 | Палитра владельца в тёмной теме **частично не проходит WCAG AA**: `--text-muted` 3,60–4,44:1, `--secondary` как текст 3,41–4,21:1, `--accent` на `surface-elevated` 4,17:1, `--border` 1,38:1 (как граница поля), белый текст на `--primary` 1,85:1. | Нужны производные текстовые токены и токен границы поля (раздел 5.1); градиент — только декоративно. |
| 4 | ~40 % запрошенных виджетов имеют прямой источник в API, ~30 % — частичный, ~30 % — **GAP** (System Health, Running AI Agents, Security Findings по workspace, Quality Score, глобальный поиск, inline-комментарии ревью, symbol search, dependency graph, context packs/«какие файлы использовал агент», worktrees, история исполнений CLI, профили агентов). | Данные для большинства GAP **уже есть в БД** (`worker_hosts`, `worktrees`, `code_symbols`, `code_dependencies`, `context_packs`, `indexing_jobs`, `cli_executions`) — нужны только read-операции API (без смены архитектуры). Inline-комментарии и глобальный поиск — новая модель/функция. |
| 5 | Безопасность: CSP `default-src 'self'`, нет `font-src` → шрифты **только self-hosted** (npm-пакеты `@fontsource-variable/*` в бандле), никаких CDN/Google Fonts. `/health/ready` вне `/api/` — nginx его **не проксирует**, SPA прочитать не может. | Inter + JetBrains Mono через npm; System Health — новая операция под `/api/v1`. |
| 6 | Логотип — растровый PNG 1536×1024, 1,5 МБ, **непрозрачный фон** `#000418`, со слоганом. | В интерфейс не подходит как есть: нужен SVG-знак (A + `</>`) и прозрачные варианты; решение владельца (4.8). |
| 7 | Из предложенного стека: Recharts, Motion, Zustand — **отклонить** (вес/избыточность); shadcn/ui — только как паттерны (текущий shadcn ориентирован на Tailwind v4/React 19, у нас Tailwind 3.4/React 18). Принять: `lucide-react`, шрифты, `@tanstack/react-virtual`, `react-resizable-panels`, `cmdk` (или свой), отдельные Radix-примитивы. | Раздел 4.4. |
| 8 | Попутно найден дефект контракта: `AnalyticsSummary.providers[].tokens.total.description` в flow-YAML содержит запятую → парсер создаёт ложное свойство `"сообщённых CLI; null — CLI не сообщали."`. | Отдельная правка контракта (вне редизайна, `openapi.yaml:3529`), сообщить владельцу. |

---

## 2. Аудит текущего frontend

### 2.1 Архитектура и зависимости (факт, `apps/web/package.json`)

| Слой | Сейчас | Оценка |
|---|---|---|
| Сборка | Vite 7.3.7, TS 5.9.3 strict + `noUncheckedIndexedAccess`, Tailwind 3.4.19 | ✅ сохранить |
| Данные | TanStack Query 5.104.5, типы из OpenAPI (`openapi-typescript` 7.13.0, `check:api`) | ✅ сохранить |
| Маршрутизация | react-router 7.18.4, реестр областей `routes/<area>.tsx` (`AreaModule`: маршруты + nav + home) | ✅ сохранить механизм; перестроить группы/порядок навигации |
| SSE | `api/sse.ts` + `StreamManager` (ref-counting, лимит 5, Last-Event-ID, 410→resync, offline/visibility) | ✅ эталонно; не трогать |
| A11y-примитивы | Radix: dialog 1.3.1, dropdown-menu 2.2.1, popover 1.3.1, tabs 1.2.1, toast 1.3.1, tooltip 1.4.1 | ✅ расширить (select, switch, scroll-area, collapsible, toggle-group, checkbox) |
| Тяжёлые виджеты | `@xyflow/react` 12.12.0 (чанк 187 kB / 61 kB gz), Monaco 0.57.0 + `@monaco-editor/react` 4.7.0 (2,7 MB / 713 kB gz, только вкладка «Изменения»), оба lazy, без CDN | ✅ уже лениво |
| i18n | i18next 26.5.7, `ru` по умолчанию, namespace на область | ✅ |
| Иконки | собственный `Icon.tsx`: 21 SVG (dashboard, folder, play, approval, terminal, quality, compliance, book, audit, layers, menu, close, keyboard, check, copy, alert, info, lock, construction, user) | ⚠️ мало для нового IA; заменить на Lucide за тем же API `Icon` |
| Графики | собственные SVG/CSS (`features/analytics/charts.tsx`) с таблицей-альтернативой | ✅ доступно; расширить (sparkline, donut, stacked bar) без библиотеки |
| Тесты | Vitest + Testing Library; Playwright mocked (контрактная валидация моков ajv), live (AC-05/06/08); axe serious/critical в двух темах; `tokens.test.ts` проверяет контраст | ✅ сохранить, расширить |
| Бандл | initial JS 762 kB / **222 kB gz**, CSS 33 kB / 6,6 kB gz | бюджет на редизайн: initial ≤ 260 kB gz |
| Темизация | `tokens.css`: RGB-тройки, `prefers-color-scheme` + `<html data-theme>`, `lib/theme.ts` (system/light/dark) | ✅ механизм сохранить; значения заменить |
| Шрифты | системные (`ui-sans-serif`, `ui-monospace`) | ⚠️ заменить на Inter / JetBrains Mono self-hosted |
| Motion | только `fade-in 120ms`; глобальный `prefers-reduced-motion` сброс | ✅ основа есть; добавить токены |
| Виртуализация | **нет** (чат — infinite pages по 50, лента run — до 1000 событий в буфере) | ❌ нужна для CLI Output/логов/чата/аудита |
| Layout | `AppLayout`: sidebar 16rem (≥ md) / drawer (< md), header, `main max-w-6xl` | ❌ нет resizable panels, правой контекстной панели, command palette, глобального поиска, статусов CLI в шапке; `max-w-6xl` не подходит для рабочего пространства 1920 |

### 2.2 Экраны: что есть → что меняется

| Требование редизайна | Существующая реализация | Качество / переиспользование | Что изменить |
|---|---|---|---|
| Оболочка: sidebar 13 пунктов | `Sidebar.tsx`, группы `work/quality/admin`, `g`+буква | ✅ доступно, RBAC | Новый состав/порядок (6.1), сворачивание в rail, иконки Lucide, бейджи счётчиков (approvals) |
| Top bar | `AppLayout` header: WorkspaceSwitcher, справка, UserMenu | ✅ | + логотип-знак, переключатель проекта, поиск, Command Palette, статусы Codex/Claude, уведомления |
| Command Palette Ctrl/Cmd+K | нет (есть `?` справка, `g`-навигация в `lib/shortcuts.ts`) | — | Новая (`cmdk` или своя на Radix Dialog + listbox) |
| Resizable panels / tabs / split / persisted layout | нет | — | `react-resizable-panels` + `lib/layoutPrefs.ts` (localStorage, как `theme.ts`) |
| Правая контекстная панель | нет | — | Новый `ContextPanel` (Inspector выбранной сущности) |
| Dashboard (bento) | `DashboardPage`: 4 StatTile + «требуют внимания» + «идут» + таблица последних runs; считает по последним 50 runs (OQ-44, честная подпись выборки) | ✅ честность выборки — сохранить | Bento-сетка 13 плиток на `getAnalyticsSummary` + `listRuns` + `listApprovals` + `listProviders`; GAP-плитки — по разделу 3 |
| AI Mission Control | Разнесено по 4 маршрутам: `RunPage` (сводка, задачи, лента SSE), `RunGraphPage` (React Flow), `ChatPage` (потоки, фильтры, поиск, timeline, композер, `DirectivesPanel`), `TaskDetailPage` (секции, DiffViewer) | ✅ логика и модели (`chatModel`, `directiveModel`, `graphLayout`) готовы | Объединить в один экран с панелями: Navigator (DAG) / Live Chat (5 видов) / Command Center / Agent Inspector; старые URL — редиректы с сохранением deep-link |
| Agents (12 ролей) | нет экрана; роль — фильтр чата, поле задачи | — | Новый экран-каталог ролей + измеренные метрики `tasks.by_role` |
| Workflows | `WorkspaceRunsPage` (`runs`), `ProjectRunsPage` | ✅ | Переименование в навигации, режимы → конвейеры ролей (справочно из AGENT_ROLES §4) |
| Projects & Developer Workspace | `ProjectsPage`, `ProjectCreatePage`, `ProjectDetailPage` (обзор, `RepositoryExplorer`: ветки, commits, дерево, файл ≤ 512 KiB, стек), `AttachRepositoryForm` (local_path/remote/GitHub), `PullRequestsPanel` | ✅ | Вкладки проекта: Обзор / Репозиторий / Запуски / Ветки / PR / Политики; Code Review Workspace — новый композитный экран |
| Code Review Workspace | `DiffViewer` (Monaco diff read-only по `git_diff` artifact), `TaskSections` (evidence, gates, findings), approvals | ✅ | Explorer файлов diff + side-by-side + маркеры findings (file/line) + gates/tests + approve/request changes через approvals/directives; inline-комментарии — GAP |
| Code Intelligence | `RepositoryExplorer` + индекс-снимок (`latest_index_snapshot`), `KnowledgePage` (источники документации, политика, технологии) | ⚠️ частично | Новый раздел; symbol search / graph / context packs — GAP (B3) |
| Security & Quality Center | `QualityIndexPage` + `RunQualityPage` (gates, verdict, findings), `FindingPage` (переходы), `EvidencePage` | ✅ модель SKIPPED≠PASS уже соблюдена | Разделить на Security Center и Quality Gates; ввести 5 состояний верификации (5.6) |
| Standards & Compliance | `ComplianceIndexPage`, `RunCompliancePage` (матрица, ревью человеком), `ProjectControlsPage` (применимость, утверждение) | ✅ | Визуальная матрица, «нет evidence» как отдельный фильтр, дисклеймер «не сертификация» на каждом экране |
| CLI Providers | `ProvidersPage`: установки, версия, совместимость, auth, availability, capabilities, probe (poll 2 с), настройки (step-up) | ✅ | Перенос в Settings › CLI Providers + статус-чипы в top bar; история исполнений — GAP (B1) |
| GitHub Integration | `GitHubPage`, `VerificationStatus`, `PullRequestsPanel` | ✅ | Только визуальный рестайл |
| Analytics | `AnalyticsPage` + SVG-графики | ✅ | Рестайл; переиспользовать графики в Dashboard |
| Documentation | `KnowledgePage` | ✅ | Переименование пункта меню, рестайл |
| Settings | `WorkspacePanel`, `MembersPanel` (step-up); `AuditPage` отдельно | ✅ | Подразделы: Workspace / Участники / CLI Providers / Аудит / Внешний вид (тема, плотность, сброс раскладки) |
| Approvals | `ApprovalsPages` (список + деталь, four-eyes, step-up) | ✅ | Не входит в 13 пунктов — решение владельца (8, D-3) |

### 2.3 Переиспользуемые части (без изменений логики)

`api/*` (все модули и ключи кэша), `api/sse.ts`, `api/streamManager.ts`, `useRunEvents`, `workspaceEvents` + `useWorkspaceInvalidation`, `auth/*` (RequireAuth, RequirePermission, StepUp), `lib/permissions.ts`, `lib/problem.ts`, `lib/format.ts`, `features/chat/chatModel.ts`, `features/directives/directiveModel.ts`, `features/runs/graphLayout.ts`, `features/tasks/diffModel.ts`, `features/quality/shared.tsx` (логика статусов), `components/states.tsx` (паттерн), `components/ui/Table.tsx` (aria-sort), `Toolbar` (roving tabindex), `focus.ts`, i18n-файлы (ключи сохраняются, добавляются новые).

---

## 3. Матрица «требование → источник данных»

Обозначения: ✅ — есть операция/событие; ◐ — частично (агрегация на клиенте, ограниченная выборка или неполные поля); ❌ **GAP** — нет данных в API. «B*» — предлагаемая фаза backend (раздел 7.2). Столбец «Без API» — что UI может показать честно до появления backend.

### 3.1 Оболочка (top bar, sidebar, панели)

| Элемент | Источник | Статус | Предложение для GAP / «Без API» |
|---|---|---|---|
| Workspace switcher | `getSession.workspaces`, `listWorkspaces` | ✅ | — |
| Project switcher | `listProjects` (cursor) | ✅ | — |
| Global search | — | ❌ GAP | **Без API:** Command Palette ищет по навигации, проектам (`listProjects`), последним runs (`listRuns` первая страница; у `listRuns` нет `q` — фильтр по `objective` на клиенте с подписью «среди последних N»). **B6:** `GET /search?q=&types=project,run,task,finding,file` (FTS по существующим таблицам, RLS). |
| Command Palette | локальные действия + навигация; команды → существующие операции (createRun, createDirective…) через те же диалоги | ✅ (UI) | Никаких shell-команд; только действия API с теми же подтверждениями/step-up |
| Codex CLI / Claude Code CLI status | `listProviders` → `installations[].auth_status/availability/compatibility/detected_version/checked_at/last_error_category` | ◐ | Нет SSE-события о провайдере → опрос (60 с, пауза при скрытой вкладке) + `probeProvider` по клику (право). Всегда показывать «проверено N мин назад». |
| Notifications | SSE `GET /workspaces/{id}/events` (`run.state_changed`, `approval.requested`, `approval.decided`, `run.completed`) + `listApprovals?status=PENDING&decidable_by_me` | ◐ | Нет хранения «прочитано» → UI-only: курсор прочитанного в localStorage (персонально, на устройстве), подпись «уведомления этого устройства». Approvals без run (`DOCS_DOWNLOAD`) в поток не попадают (OQ-45 c) → опрос approvals 60 с. B8 (опционально): серверные уведомления. |
| Profile | `getSession.user`, `logout`, тема (`lib/theme.ts`) | ✅ | — |
| Persisted layout (панели, свёрнутость, вкладки) | — | UI-only | localStorage per user+workspace, ключ с версией схемы; сброс в Settings › Внешний вид. Не синхронизируется между устройствами (честно указать). |

### 3.2 Bento Dashboard

| Плитка | Источник | Статус | Предложение / «Без API» |
|---|---|---|---|
| Active Projects | `listProjects?status=` (+ `listRuns` для «есть активный run») | ✅/◐ | Число проектов — точно (при `next_cursor` — «N+»). «Активный» = есть run в активном статусе среди выборки — подписать выборку. |
| Running AI Agents | — (нет workspace-списка задач/исполнений) | ❌ GAP | **Без API:** для runs со статусом `RUNNING|VERIFYING` (`listRuns?status=`, ≤ 10) — `getRunTasks?status=RUNNING` (N+1, ограничено, подпись выборки); показывать «роль × провайдер × задача», а не «агент онлайн». Альтернатива: `analytics.providers[].in_progress` (только количество). **B1:** `GET /tasks?status=RUNNING,LEASED&workspace` (TaskSummary + run_id + current_provider). |
| Completed Tasks | `getAnalyticsSummary.tasks` (`succeeded`, `verified_completion_rate`, окно `from/to`) | ✅ | Подпись окна времени; показывать «подтверждённо завершено» (verified), а не «выполнено агентом». |
| Quality Score | — (нет скалярной оценки) | ❌ GAP (концептуальный) | **Не вводить выдуманную формулу.** Показывать измеренные: `analytics.gates[].pass_rate` (SKIPPED не в знаменателе), `tasks.verified_completion_rate`, доля runs с verdict PASS. Композитный «score» — только по утверждённой владельцем формуле (D-6), с раскрытием состава. |
| Security Findings | `listRunFindings` / `getRunQuality.findings_summary` — **только per run** | ❌ GAP (workspace) | **Без API:** сумма `findings_summary` по последним N завершённым runs (N+1, подпись). **B1:** `GET /findings?severity=&status=&project_id=` (workspace) или блок `findings` в `AnalyticsSummary`. |
| Pending Approvals | `listApprovals?status=PENDING` (+ `decidable_by_me`), SSE workspace | ✅ | — |
| CLI Provider Health | `listProviders` + `analytics.providers` (executions, errors, rate_limited, auth_required, success_rate, failures_by_category) | ✅ | Только измеренное; **никаких квот/лимитов подписки** (CLI их не сообщают). Токены — только если `tokens.total ≠ null`, с источником. |
| Workflow Success Rate | `analytics.runs.success_rate` (+ `by_mode`) | ✅ | `null` → «нет завершённых runs в окне», не 0 %. |
| AI Activity Timeline | SSE workspace (4 типа) + `listRuns` (created/started/finished) | ◐ | Без API — лента из этих событий («события runs и approvals»). `listAuditEvents` — только admin (`audit:read`). **B1 (опц.):** `GET /activity` workspace (агрегат `run_event_log` по workspace; индекс `(workspace_id, created_at)` отсутствует — OQ-45 c). |
| Active Workflows | `listRuns?status=RUNNING,…` + `getRun.task_counts`, `pending_approvals` | ✅ | — |
| Agent Performance | `analytics.tasks.by_role[]` (attempts, succeeded, failed, retries, verified_completion_rate), `review_loops` | ✅ | Только измеренные доли с размером выборки; без «рейтингов» агентов. |
| Quality Overview | `analytics.gates[]` (total/passed/passed_verified/passed_unverified/failed/errored/skipped/pass_rate; D-8: «Пройдено» = `passed_verified`, миграция 0021) | ✅ | — |
| System Health | `/health/ready` (postgres, redis, object_storage, migrations) — **вне `/api/`, nginx не проксирует**; workers/queues — нет | ❌ GAP | **B2:** `GET /api/v1/system/health` (право admin): компоненты + `worker_hosts` (kind, status, `last_heartbeat_at`) + глубина очередей Redis/outbox lag. **Без API:** плитка не показывается (не выводить «OK» по умолчанию). |

### 3.3 AI Mission Control

| Элемент | Источник | Статус | Примечание |
|---|---|---|---|
| Workflow Navigator (DAG) | `getRunTasks` (tasks + edges, `plan_version`) + SSE `task.state_changed`, `run.state_changed` | ✅ | React Flow + свой `layoutDag`; текстовый список-альтернатива (уже есть) |
| View: Agent Conversation | `getRunChat?stream=all_agents&item_type=agent_message` + SSE `agent.message`, `chat.message` | ✅ | Потоки ТЗ §11 (All Agents/Codex/Claude/Role/Task/Approvals) сохраняются как фильтры |
| View: CLI Output | SSE `cli.output` (санитизировано, `truncated`, `dropped_chunks`, `redactions`); артефакты `cli_output`/`raw_cli_log` (`downloadArtifact`); `getRunActivity?event_type=` | ✅ | Виртуализация обязательна; моноширинный; показывать маркеры редакции/усечения |
| View: System Events | `getRunActivity` + SSE `run.state_changed`, `task.state_changed`, `gate.completed`, `run.completed`; чат `message_kind=system_notice` | ✅ | — |
| View: User Directives | `listDirectives`, `getDirective` + SSE `directive.impact_ready`, `directive.state_changed` | ✅ | Этапы RECEIVED→…→ACKNOWLEDGED→COMPLETED; «доставлено ≠ выполнено» |
| View: Evidence | `listRunEvidence?verification_status=&kind=&task_id=`, `getEvidence` | ✅ | — |
| Фильтры | `getRunChat` (`stream, role, task_id, thread_id, q, item_type, order`) | ✅ | — |
| User Command Center | `createDirective` (ASK, NEW_TASK, CHANGE_REQUEST, STOP, PAUSE, RESUME, REVIEW, APPROVE, REJECT) → impact preview → `confirmDirective`/`cancelDirective`; `postChatMessage` (комментарий, агентам не доставляется); `pauseRun/resumeRun/cancelRun` | ✅ | `expected_run_version` (409 → перечитать); права по типу (API_DESIGN §5.4); step-up; критические — подтверждение в диалоге |
| Agent Inspector | `getTask` (instructions, acceptance_criteria, review_loop_count, timeout), `listTaskAttempts` (provider, routing_reason, executions: status/exit_code/failure_category), `listTaskMessages` (ACP), `listTaskEvidence`, gates из `getRunQuality` | ✅ | — |
| «Какие файлы/символы использовал агент» (Context Pack) | таблица `context_packs.manifest` есть; API нет; artifact kind `context_pack` в контракте есть, но связь с попыткой не отдаётся | ❌ GAP | **B3:** `GET /tasks/{task_id}/attempts/{attempt_id}/context-pack` → manifest (пути, символы, commit, stale-флаги, omitted). Без API — раздел не показывается. |

### 3.4 Agents и Workflows

| Элемент | Источник | Статус | Предложение |
|---|---|---|---|
| Каталог 12 ролей (миссия, выход, права, ACP, гейты) | `AgentRole` enum; описания — AGENT_ROLES.md (документация, не live) | ◐ | Статические i18n-тексты с пометкой «описание профиля по документации v1.0». **B7 (опц.):** `GET /agent-profiles` (write_scope, network, allowed_tools, template_sha256 — поля из AGENT_ROLES §5). |
| Текущая активность роли | `getRunTasks?agent_role=` по активным runs | ◐ | См. Running AI Agents (B1). Формулировка: «назначено задач: N», не «агент работает». |
| Метрики роли | `analytics.tasks.by_role` | ✅ | — |
| Workflows (= runs) | `listRuns`, `getRun`, `createRun` | ✅ | — |
| Шаблоны конвейеров по режимам | AGENT_ROLES §4 (документация) | ◐ | Статическая справка; фактический план — только из `getRunTasks`. |

### 3.5 Projects & Developer Workspace / Code Review

| Элемент | Источник | Статус | Предложение |
|---|---|---|---|
| Создать проект | `createProject` (name, description, documentation_mode, quality_profile) | ✅ | — |
| Привязать репозиторий / локальный путь | `attachRepository` (provider, remote_url, local_path, default_branch) + `getGitHubVerification` | ✅ | — |
| Режим | `RunCreateRequest.mode` (11 режимов) | ✅ | — |
| Исполнители (executors) | `RunCreateRequest.routing.allowed_providers`; `ProviderSettings.allowed_roles` | ✅ | — |
| Политики качества | `ProjectSettings.quality_profile`, `security_profile`, `routing_policy_id`; `listProjectControls` | ✅ | — |
| История запусков | `listRuns?project_id=` | ✅ | — |
| Ветки | `listRepositoryBranches` | ✅ | — |
| Worktrees | таблица `worktrees` есть; API нет → **B4 `listProjectWorktrees` (выполнено), вкладка проекта F6** | ❌ GAP → ✅ | **B4:** `GET /projects/{id}/repositories/{rid}/worktrees` (только чтение: путь-идентификатор без абсолютного пути хоста, task/attempt, статус, commit). Без API — не показывать. |
| PR | `listPullRequests`, `requestPullRequest`, `requestPullRequestMerge` (→ approval MERGE) | ✅ | — |
| Файлы / изменения | `listRepositoryTree`, `getRepositoryFile` (≤ 512 KiB), `listRepositoryCommits`; diff задачи — artifact `git_diff` | ◐ | Diff произвольного commit — GAP (низкий приоритет); изменения run/task — через `git_diff`. |
| Code Review: file explorer | файлы из `git_diff` artifact (`diffModel.ts`) | ✅ | — |
| Monaco side-by-side diff | `git_diff` artifact (`downloadArtifact`) | ✅ | lazy; текстовый fallback сохраняется |
| Inline comments | — → **B5 (ADR-0015, выполнено), Code Review F6** | ❌ GAP → ✅ | **B5:** новая модель `review_comments` (task/run, file, line, commit, body, author, thread) + операции; значимое изменение backend → **требует отдельного ADR**. **Без API:** findings (`file_path`, `line`, severity, rule) как read-only маркеры в Monaco; комментарий человека — `CHANGE_REQUEST`-директива с указанием `файл:строка` в тексте (реально доставляется через impact analysis) — честно подписать «поручение», а не «комментарий». |
| Findings / tests / security | `getRunQuality` (gate_executions), `listRunFindings?task_id=`, `listTaskEvidence` | ✅ | — |
| Approve / Request changes | `decideApproval` (MANUAL_REVIEW / PLAN_APPROVAL / MERGE…), `createDirective` (REVIEW/APPROVE/REJECT/CHANGE_REQUEST), `transitionFinding` | ✅ | Кнопки появляются только при существующем approval/праве; без «approve PR в GitHub» напрямую |
| Shell из браузера | — | ⛔ запрещено | Ни терминала, ни «run command» в UI |

### 3.6 Code Intelligence

| Элемент | Источник | Статус | Предложение |
|---|---|---|---|
| Code explorer | `listRepositoryTree`, `getRepositoryFile` (по commit/ветке/снимку индекса) | ✅ | — |
| Symbol search | `code_symbols` + `fts_search`/`trigram_search` (пакет `aicd_code_intelligence`) — API нет | ❌ GAP | **B3:** `GET /repositories/{rid}/symbols?q=&kind=&snapshot_id=` |
| Dependency graph | `code_dependencies` (+ `graph.build_edges`) — API нет | ❌ GAP | **B3:** `GET /repositories/{rid}/dependencies?path=&depth=` (ограничение узлов); React Flow (уже в бандле) |
| Context packs | `context_packs` — API нет; `analytics.context` (packs, sizes, stale ratio, usefulness) | ◐ | Агрегаты — ✅ сейчас; конкретный pack — B3 |
| Indexing status | `Repository.latest_index_snapshot{status, commit_sha}` | ◐ | Очередь/ошибки `indexing_jobs` — B3 |
| Knowledge explorer | `listDocumentationSources`, `listWorkspaceTechnologies`, `listTechnologies`, политика OFF/SMART/STRICT | ✅ | — |
| Git-aware history | `listRepositoryCommits`, `listRepositoryBranches` | ✅ | — |
| Файлы/символы, использованные агентом в задаче | `context_packs.manifest` | ❌ GAP | B3 (см. 3.3) |

### 3.7 Security Center и Quality Gates

| Элемент | Источник | Статус | Предложение |
|---|---|---|---|
| Findings (severity, статус, файл/строка, правило, источник) | `listRunFindings`, `getFinding`, `transitionFinding` | ✅ per run / ❌ workspace | B1 для сводного списка |
| CWE | `Finding.cwe_id` | ✅ | — |
| OWASP / контроли | `Finding.control_refs` (+ `listProjectControls`) | ◐ | Показывать только присутствующие ссылки; не выводить OWASP-категорию, если её нет в данных |
| Dependency risks (SCA) | findings с `source` SCA (osv/trivy) + artifacts `scan_report`, `sbom` | ◐ | Фильтр по `source`; отдельной модели пакетов нет |
| Secrets | findings источника gitleaks | ◐ | Значение секрета никогда не показывается (санитизация на сервере) |
| Evidence | `getEvidence` (command, exit_code, commit, environment, artifact sha256) | ✅ | — |
| Remediation | нет поля в `Finding`; есть `FIX_REQUEST.payload.requested_action` в ACP | ◐ | Ссылка на связанное ACP-сообщение; поле `remediation` — B1 (опц.) |
| Gates lint/typecheck/build/unit/integration/e2e/review/coverage | `GateExecution.gate_type/status/required/evidence_id/not_applicable_reason`; `QualityReport.verdict` | ✅ | Состояния — по 5.6 |
| Coverage | artifact `coverage_report` | ◐ | Показ значения только если оно есть в отчёте; иначе «отчёт доступен для скачивания» |

### 3.8 Standards & Compliance

| Элемент | Источник | Статус |
|---|---|---|
| Registry стандартов/контролей | `listProjectControls` (standard_code, edition, standard_title, clause_ref, control_code, description, verification_method) | ◐ (по проекту; глобального реестра нет — достаточно) |
| Применимость | `ProjectControl.applicability/proposed_applicability/rationale`, `approveProjectControl` | ✅ |
| Матрица | `getRunCompliance` (`ComplianceRow`), `RunReport.compliance_matrix` (`ComplianceTraceRow`) | ✅ |
| Evidence mapping / missing evidence | `ComplianceRow.evidence_id/gate_execution_id/status` (UNVERIFIED) | ✅ |
| Отчёты | `getRunReport` (JSON / text/markdown), `disclaimer` | ✅ |
| «Не сертификация» | `RunReport.disclaimer` + ТЗ §13 | ✅ — постоянный баннер |

### 3.9 CLI Providers

| Элемент | Источник | Статус |
|---|---|---|
| Installed / version / compatibility | `ProviderInstallation.detected_version/compatibility/host` | ✅ |
| Auth status / availability / errors | `auth_status`, `availability`, `last_error_category`, `checked_at`; `probeProvider` + `getProviderProbe` | ✅ |
| Execution history | `analytics.providers` (агрегаты), `listTaskAttempts.executions` (per task) | ◐ → **B1:** `GET /providers/{id}/executions?from=&status=` (из `cli_executions`) |
| Routing settings | `updateProviderSettings` (enabled, max_parallel_sessions, default_timeout_seconds, allowed_roles), step-up | ✅ |
| Measured usage | `analytics.providers` (executions, durations, tokens только если CLI сообщил) | ✅ — **без квот и «остатка подписки»** |

### 3.10 Сводный список GAP

| ID | GAP | Данные в БД | Минимальное дополнение | Без API (честно) | Фаза |
|---|---|---|---|---|---|
| G-01 | Running AI Agents (workspace) | `tasks`, `task_attempts`, `cli_executions` | `GET /tasks?status=&workspace` | N+1 по ≤10 активным runs, подпись выборки | B1 |
| G-02 | Security findings по workspace | `findings` | `GET /findings` (фильтры) или блок в analytics | сумма по последним N runs | B1 |
| G-03 | История исполнений CLI | `cli_executions` | `GET /providers/{id}/executions` | агрегаты analytics | B1 |
| G-04 | Workspace activity timeline | `run_event_log` | `GET /activity` + индекс | SSE workspace + listRuns | B1 (опц.) |
| G-05 | System Health (workers, очереди) | `worker_hosts`, outbox, Redis | `GET /system/health` (admin) | плитка скрыта | B2 |
| G-06 | Symbol search | `code_symbols`, FTS/trgm | `GET …/symbols` | — | B3 |
| G-07 | Dependency graph | `code_dependencies` | `GET …/dependencies` | — | B3 |
| G-08 | Context pack попытки (файлы/символы агента) | `context_packs.manifest` | `GET …/attempts/{id}/context-pack` | агрегаты `analytics.context` | B3 |
| G-09 | Очередь индексации | `indexing_jobs` | `GET …/indexing-jobs` | `latest_index_snapshot.status` | B3 |
| G-10 | Worktrees | `worktrees` | `GET …/worktrees` (read-only) | скрыто | B4 |
| G-11 | Inline review comments | **нет** | новая модель + ADR | findings-маркеры + CHANGE_REQUEST | B5 |
| G-12 | Global search | частично (FTS) | `GET /search` | палитра по загруженным спискам | B6 |
| G-13 | Профили агентов | `agent_profiles` | `GET /agent-profiles` | статические описания из документации | B7 (опц.) |
| G-14 | Quality Score | — | **решение владельца о формуле** | измеренные доли (pass_rate, verified rate) | D-6 |
| G-15 | Уведомления (прочитано/история) | нет → **B8 выполнена 2026-11-09** (ADR-0016, миграция 0020): `GET /notifications`, `GET /notifications/unread-count`, `POST /notifications/mark-read`, кадры `notification.created` в `GET /workspaces/{id}/events` | `notifications` | localStorage-курсор на устройстве (до перехода UI на B8) | B8 |
| G-16 | Remediation в Finding | нет | поле `remediation` (опц.) | ссылка на ACP FIX_REQUEST | B1 (опц.) |

**Ни один GAP не закрывается выдуманными значениями.** Состояние «нет источника» — компонент `DataUnavailable` с причиной и ссылкой на документацию, а не «0», «OK» или «—» без пояснения.

---

## 4. Конфликты и риски

### 4.1 Конфликты с ТЗ и ADR

| # | Конфликт | Источник | Предложение |
|---|---|---|---|
| C-1 | Новый sidebar (13 пунктов) не содержит **Approvals, CLI Providers, Audit, Runs/Task Detail, Repository Explorer**, обязательных по ТЗ §11 (список UI-экранов) | ТЗ §11 | Approvals — бейдж в top bar + плитка + отдельный маршрут; CLI Providers и Audit — подразделы Settings + чипы статуса в top bar; Run Graph/Live Chat/Task Detail — панели Mission Control; Repository Explorer — Projects/Code Intelligence. Решение D-3. |
| C-2 | Виды Mission Control (Agent Conversation/CLI Output/System Events/User Directives/Evidence) ≠ обязательные потоки ТЗ (All Agents/Codex/Claude/Role/Task/Approvals) | ТЗ §11, API_DESIGN §12.1 | Вид = тип содержимого (вкладки), поток = фильтр (сохраняется). Оба измерения доступны одновременно. |
| C-3 | WCAG 2.2 AA в запросе vs WCAG 2.1 AA в ADR-0014 и `tokens.css`/README | ADR-0014 п.5 | Поднять цель до 2.2 AA (надмножество) — **дополнение к ADR-0014** (решение D-1). Новые критерии: 2.4.11, 2.5.7 (перетаскивание панелей — альтернатива клавиатурой/кнопками), 2.5.8, 3.2.6, 3.3.7, 3.3.8. |
| C-4 | «Список зависимостей утверждается; каждая новая — в SBOM/SCA» | ADR-0014 «Последствия» | Новые пакеты (4.4) — в ADR-дополнении и SBOM; точные версии ≥ 2 недель. |
| C-5 | ADR-0014 п.4 упоминает layout dagre/elk; реализован свой `layoutDag` | ADR-0014 | Оставить свой (зависимость не нужна); отметить в дополнении. |
| C-6 | Tabs + split view + persisted layout на уровне «IDE» vs маршрутизация react-router и deep-links | README «Как добавить экран» | Вкладки — внутри экранов (Mission Control, Code Review), состояние панелей — в URL search params (deep-link) + размеры в localStorage; глобальные «документ-вкладки» — не в v1 (D-4). |
| C-7 | Объединение `RunPage`/`RunGraphPage`/`ChatPage`/`TaskDetailPage` ломает селекторы **live E2E AC-05/06/08** и mocked-спеков | `e2e/live/mission-control.spec.ts` | Старые URL → редиректы на Mission Control с соответствующей панелью; обновление спеков в той же фазе; AC-05/06/08 обязательны к прохождению перед merge. |
| C-8 | «Agents» как отдельные сущности vs ТЗ §4: роли — логические профили, не сервисы | ТЗ §4, AGENT_ROLES §1 | Нет статусов «online/idle/always-on», нет «аватаров-персонажей»; карточка = профиль + измеренные задачи. |
| C-9 | Dashboard «Quality Score» vs evidence-first | ТЗ §14, AC-11 | См. G-14 / D-6. |

### 4.2 Безопасность и офлайн-установка

| Риск | Факт | Мера |
|---|---|---|
| CSP | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'` — нет `font-src` ⇒ действует `default-src 'self'` | Шрифты и иконки — только из бандла. Запрещены Google Fonts, CDN, внешние изображения/аватары (Gravatar и т. п.). Worker Monaco уже same-origin. |
| Офлайн-установка | SPA собирается в Docker из lockfile (`pnpm install --frozen-lockfile`) | Шрифты как npm-пакеты (`@fontsource-variable/*`, OFL-1.1) попадают в `dist/` → runtime офлайн. Лицензии OFL — в SBOM/NOTICE. |
| Недоверенные данные | Тексты агентов/репозитория выводятся только как текст (README, RepositoryExplorer) | Редизайн не вводит markdown/HTML-рендер содержимого агентов; подсветка кода — только Monaco (без исполнения). |
| Command Palette | соблазн «выполнить команду» | Только навигация и действия API с теми же правами/подтверждениями/step-up; нет произвольного ввода команд оболочки. |
| Health | `/health/ready` вне `/api/`, nginx не проксирует | Не добавлять в nginx публичный проброс; делать B2 с RBAC. |
| Hostnames | `ProviderInstallation.host`, `worker_hosts` | Показывать hostname (это уже в контракте); никаких путей/credentials. |

### 4.3 Производительность и бандл

| Риск | Мера / бюджет |
|---|---|
| Initial JS сейчас 222 kB gz | Бюджет ≤ 260 kB gz initial; проверка в CI (`vite build` + скрипт размера) |
| Шрифты | Inter Variable (latin + cyrillic, `unicode-range`) + JetBrains Mono Variable (latin + cyrillic); `font-display: swap`; preload только Inter latin/cyrillic. Ориентир ≤ 200 kB woff2 суммарно — **измерить** в фазе 1 |
| Lucide | только именованный импорт (tree-shaking), ≈ 0,3–0,6 kB gz на иконку; запрет `import * as` (ESLint `no-restricted-imports`) |
| React Flow / Monaco | остаются lazy; Code Review подгружает Monaco только при открытии diff |
| Длинные списки | `@tanstack/react-virtual` для CLI Output, чата, событий, аудита, findings (> 200 строк); `aria-rowcount`/`aria-setsize`, клавиатурная навигация, «Перейти к последнему» |
| backdrop-filter | дорог на больших площадях: только top bar, палитра, поповеры; отключается при `prefers-reduced-transparency` и на слабых устройствах (`@supports`) |

### 4.4 Зависимости: решение по каждой

Версии — последние стабильные, выпущенные не позднее 2026-10-25 (≥ 2 недель до даты документа), совместимые с React 18.3.1 (по `peerDependencies` из npm registry, проверено 2026-11-08).

| Пакет | Версия (дата) | Решение | Обоснование |
|---|---|---|---|
| shadcn/ui | — (не npm-зависимость) | **Только паттерны**, без CLI | Генерирует код под Tailwind v4/React 19 (data-slot, tw-animate-css); у нас Tailwind 3.4 + React 18 и свой кит на Radix с токенами и тестами. Копировать структуру вариантов, не код. |
| `class-variance-authority` | 0.7.1 (2024-11-26) | Опционально | Удобно для вариантов кнопок/бейджей; можно обойтись `cx.ts`. Решение — в фазе 1. |
| `tailwind-merge` | **2.6.x** (не 3.x) | Опционально | v3 поддерживает только Tailwind v4. |
| `lucide-react` | 1.61.0 (2026-10-21), peer React ≤19 | **Принять** | Требование владельца; ISC; tree-shakable; заменяет 21 самодельную иконку за API `Icon`. |
| `@fontsource-variable/inter` | 5.3.0 (2026-07-19) | **Принять** | Self-hosted, кириллица, OFL. |
| `@fontsource-variable/jetbrains-mono` | 5.3.0 (2026-07-19) | **Принять** | Код/логи, кириллица, OFL. |
| `@fontsource/geist-sans` | 5.3.0 | Отклонить по умолчанию | Покрытие кириллицы требует проверки; UI русскоязычный — Inter надёжнее. |
| `@tanstack/react-virtual` | 3.14.22 (2026-10-22) | **Принять** | Виртуализация логов/чата; headless, совместим с нашим a11y-подходом; та же экосистема TanStack. |
| `react-resizable-panels` | 4.15.0 (2026-10-24), peer React ^18 | **Принять** | Resizable split + клавиатурные separators (WCAG 2.1.1, 2.5.7) + встроенная персистентность. API v4 — проверить в фазе 2. |
| `cmdk` | 1.1.1 (2025-03-14) | Принять **или** свой | Доступный combobox для палитры (зависит от Radix dialog ^1.1.6 — совместимо с 1.3.1). Альтернатива — ~150 строк на Radix Dialog + listbox. D-5. |
| Radix: select 2.4.1, switch 1.4.1, scroll-area 1.4.0, collapsible 1.2.1, toggle-group 1.2.1, checkbox 1.4.1, radio-group 1.5.1, separator 1.2.0, context-menu 2.4.1 (все 2026-10-12) | — | **Принять по мере надобности** | ADR-0014 п.5; только используемые. |
| `zustand` | 5.0.16 | **Отклонить** | Серверное состояние — TanStack Query; UI-предпочтения — `useSyncExternalStore` + localStorage (паттерн `theme.ts`); выбор в Mission Control — URL params (deep-link). Пересмотреть, если появится сложное межпанельное состояние. |
| `recharts` | 3.10.1 | **Отклонить** | Тянет `@reduxjs/toolkit`, `react-redux`, `immer`, `victory-vendor` (d3) — десятки kB gz; текущие SVG-графики уже доступны (таблица-альтернатива). Нужные формы (sparkline, bar, donut, stacked) — свои SVG. |
| `motion` (Framer Motion) | 14.6.0 | **Отклонить** | Анимации 120–250 мс покрываются CSS transitions/keyframes; глобальный reduced-motion уже есть; минус вес и inline-стили. |
| React Flow, Monaco | без изменений | — | Уже в проекте, lazy. |

### 4.5 Палитра и WCAG (расчёт по относительной яркости WCAG 2.x)

**Тёмная тема — палитра владельца как текст на фонах** (≥ 4,5 — AA для обычного текста; ≥ 3,0 — крупный текст и не-текстовые элементы):

| Токен | на `background` #080B14 | `bg-secondary` #0D1220 | `surface` #111827 | `surface-elevated` #182235 | Вердикт |
|---|---|---|---|---|---|
| text-primary #F5F7FF | 18,38 | 17,46 | 16,58 | 14,88 | ✅ |
| text-secondary #9BA9C2 | 8,28 | 7,87 | 7,47 | 6,71 | ✅ |
| **text-muted #697991** | **4,44** | **4,22** | **4,01** | **3,60** | ❌ текст; только disabled/декор |
| primary #00CFFF | 10,64 | 10,11 | 9,60 | 8,62 | ✅ |
| primary-hover #35DEFF | 12,21 | 11,60 | 11,02 | 9,89 | ✅ |
| **secondary #7555FF** | **4,21** | **4,00** | **3,80** | **3,41** | ❌ текст; ✅ ≥ 3 для заливок/иконок |
| accent #B64DFF | 5,14 | 4,89 | 4,64 | **4,17** | ⚠️ не на elevated |
| success #22C58B | 8,82 | 8,38 | 7,96 | 7,15 | ✅ |
| warning #F5B942 | 11,14 | 10,58 | 10,05 | 9,02 | ✅ |
| danger #F06476 | 6,36 | 6,04 | 5,74 | 5,15 | ✅ |
| info #5A9BFF | 7,08 | 6,73 | 6,39 | 5,74 | ✅ |
| **border #263249** | 1,53 | 1,45 | 1,38 | 1,24 | ❌ как граница поля (1.4.11); ✅ декоративный разделитель |
| border-active #4969FF | 4,44 | 4,22 | 4,01 | 3,60 | ✅ ≥ 3 (выделение/selected) |

Прочие пары: белый на `#00CFFF` — **1,85** ❌ → кнопка primary только с тёмным текстом `#080B14` (10,64 ✅). Текст поверх градиента недопустим: на `#4969FF` и тёмный (4,44), и белый (4,42) текст < 4,5.

**Производные токены (тёмная), предложение:**

| Новый токен | Значение | Контраст (bg / bg2 / surface / elevated) |
|---|---|---|
| `--text-muted` (заменить) | #8090A8 | 6,06 / 5,76 / 5,47 / 4,91 ✅ |
| `--text-disabled` (бывший muted) | #697991 | исключение WCAG для disabled |
| `--secondary-text` | #A08CFF | 7,20 / 6,84 / 6,50 / 5,83 ✅ |
| `--accent-text` | #C77DFF | 7,31 / 6,94 / 6,59 / 5,92 ✅ |
| `--border-input` | #64748F | 4,15 / 3,95 / 3,75 / 3,36 ✅ (≥ 3) |
| `--focus-ring` | #35DEFF | ≥ 9,89 ✅ |
| `--danger-text-strong` (на soft-фоне) | #F7899A | ≥ 6,82 ✅ |

**Soft-фоны статусов (тёмная, 14 % тона поверх surface):** success-soft #133035 (тон-текст 6,28 ✅), warning-soft #312F2B (7,57 ✅), danger-soft #302332 (4,81 ✅), info-soft #1B2A45 (5,17 ✅), primary-soft #0F3245 (7,28 ✅); secondary-soft #1F2145 → текст `--secondary-text` (≥ 5 ✅; исходный #7555FF 3,30 ❌); accent-soft #281F45 → `--accent-text`.

**Светлая тема — предлагаемые соответствия (все ≥ AA):**

| Токен | Значение | на bg #F6F8FC | bg-secondary #EEF2F9 | surface #FFFFFF |
|---|---|---|---|---|
| background | #F6F8FC | — | — | — |
| background-secondary | #EEF2F9 | — | — | — |
| surface / surface-elevated | #FFFFFF (+ тень) | — | — | — |
| text-primary | #0B1220 | 17,61 | 16,68 | 18,72 |
| text-secondary | #3D4A63 | 8,38 | 7,93 | 8,91 |
| text-muted | #55627A | 5,78 | 5,48 | 6,15 |
| primary (текст/ссылки; кнопка с белым текстом 5,82) | #006E8C | 5,47 | 5,18 | 5,82 |
| primary-hover | #005A73 | 7,29 | 6,90 | 7,75 |
| secondary | #5A3BE0 | 6,30 | 5,96 | 6,69 |
| accent | #8A2BD0 | 5,92 | 5,61 | 6,29 |
| success | #0B7A55 | 5,03 | 4,76 | 5,34 |
| warning | #8A5A00 | 5,57 | 5,28 | 5,93 |
| danger | #C42F45 | 5,14 | 4,87 | 5,47 |
| info | #2D5FD0 | 5,40 | 5,11 | 5,74 |
| border (декор) | #D5DCE8 | 1,30 | 1,23 | 1,38 |
| border-input | #7A879C | 3,42 | 3,24 | 3,64 ✅ ≥ 3 |
| border-active / focus-ring | #3A57E8 | 5,33 | 5,04 | 5,66 |

Soft-фоны светлой темы (10 % тона на белом): тон-текст на них 4,67–5,71 ✅.

Все пары фиксируются в `styles/tokens.test.ts` (расширение существующего теста), включая soft-пары и границы.

### 4.6 Роли агентов: сверка

| Источник | Роли |
|---|---|
| ТЗ §4 | requirements, architect, planner, developer, reviewer, security, qa, database, performance, devops, maintenance, documentation |
| AGENT_ROLES.md §2.1–2.12 | те же 12 |
| `AgentRole` (OpenAPI) | те же 12 |
| Запрос на редизайн | «12 roles» — **имена не перечислены** |

**Расхождений внутри репозитория нет.** Риск — в макетах/референсах владельца: если там фигурируют иные имена (Orchestrator, Router, Coder, Tester, Debugger, Researcher, Verifier и т. п.), их нужно сопоставить: Orchestrator/Router/Verification Engine/CLI Gateway — **компоненты платформы**, не агенты (ТЗ §2, AGENT_ROLES §1 п.4); «Coder»→developer, «Tester»→qa, «Debugger»→maintenance (режим DEBUG), «Researcher»→requirements/architect. Решение D-2.

### 4.7 «Glassmorphism» vs контраст

| Ситуация | Результат расчёта | Правило |
|---|---|---|
| Стекло `surface-elevated` 72 % над тёмным canvas | text-muted (исходный) 3,85 ❌, text-secondary 7,18 ✅ | Над плоским canvas — допустимо с новыми токенами |
| Стекло 72 % над cyan-градиентом | text-muted 1,93 ❌, text-secondary 3,60 ❌ | **Запрещено** класть стекло с текстом над градиентом/изображением |
| Стекло 88 % над cyan | text-secondary 5,25 ✅, muted 2,81 ❌ | Минимум α ≥ 0,88; на стекле — только text-primary/secondary |

Итог: стекло — только top bar, Command Palette, поповеры/меню, sheet правой панели на узких экранах; α ≥ 0,88 (тёмная) / 0,92 (светлая), blur 12–16 px, граница 1 px `--glass-border`; fallback без `backdrop-filter` — непрозрачный `surface-elevated`; `prefers-reduced-transparency: reduce` и `forced-colors: active` — непрозрачные поверхности. Плитки bento — **непрозрачные** surface (читаемость данных важнее эффекта); «свечение» — только тонкая градиентная рамка у ключевых плиток и активного узла DAG.

### 4.8 Логотип

| Свойство | Факт |
|---|---|
| Файл | `Logo AiCodingDev.png`, PNG RGB 1536×1024, 1,53 МБ, **без альфа-канала**, контент в bbox (195,79)–(1334,865) |
| Композиция | знак: буква «A» с символом `</>` внутри, шестерня справа, орбита-дуга; ниже — wordmark «AiCodingDev» (градиент cyan → белый → violet) и слоган «AI SOFTWARE DEVELOPMENT PLATFORM» |
| Ключевые цвета (средние по оттенкам) | фон #000418; cyan #1CD4FA; azure #42A1F8; blue #385EEF; indigo #5436E6; violet #983BF2; orchid #D66DFA; белый wordmark |
| Связь с палитрой | `--primary #00CFFF` ≈ cyan логотипа; `--border-active #4969FF` ≈ blue #385EEF; `--secondary #7555FF` ≈ между indigo/violet; `--accent #B64DFF` ≈ violet/orchid. Фон логотипа (#000418) темнее и синее `--background #080B14` — на сайте будет виден прямоугольник. |
| Проблемы | непрозрачный фон (нельзя на светлой теме и на `#080B14` без шва); слоган нечитаем < 200 px; 1,5 МБ; растровый — размыт в favicon 16–32 px; эффекты свечения не масштабируются |
| Нужно | (1) SVG-знак (A + `</>` + дуга, без слогана) — для top bar 28–32 px и favicon; (2) монохромная версия для `forced-colors`; (3) светлый вариант (без свечения, градиент на тёмных штрихах); (4) wordmark — набирать Inter, не растром. Создание векторного знака — дизайнерская работа или утверждённая владельцем перерисовка (D-7). До этого — wordmark текстом + обрезанный прозрачный PNG/WebP ≤ 20 kB. |

---

## 5. Спецификация дизайн-системы

### 5.1 Цветовые токены

Механизм сохраняется: RGB-тройки в `tokens.css`, семантические имена в `tailwind.config.ts`; `:root` (светлая), `@media (prefers-color-scheme: dark) :root:not([data-theme='light'])` и `:root[data-theme='dark']` (тёмная). Компоненты используют только семантические токены.

| Семантический токен | Назначение | Dark | Light |
|---|---|---|---|
| `canvas` | фон приложения | #080B14 | #F6F8FC |
| `canvas-secondary` | sidebar, полосы, фон панелей | #0D1220 | #EEF2F9 |
| `surface` | карточки, плитки, таблицы | #111827 | #FFFFFF |
| `surface-raised` | поповеры, меню, активные плитки | #182235 | #FFFFFF + `shadow-2` |
| `fg` | основной текст | #F5F7FF | #0B1220 |
| `fg-secondary` | вторичный текст | #9BA9C2 | #3D4A63 |
| `fg-muted` | подсказки, мета | #8090A8 | #55627A |
| `fg-disabled` | disabled | #697991 | #8A95A8 |
| `border` | декоративные разделители | #263249 | #D5DCE8 |
| `border-input` | границы полей/контролов (≥ 3:1) | #64748F | #7A879C |
| `border-active` | выделение, selected | #4969FF | #3A57E8 |
| `focus` | кольцо фокуса 2 px + offset 2 px | #35DEFF | #3A57E8 |
| `primary` / `primary-hover` | CTA, ссылки, активный пункт | #00CFFF / #35DEFF | #006E8C / #005A73 |
| `primary-fg` | текст на primary | #080B14 | #FFFFFF |
| `secondary` (+ `-text`) | вторичные акценты, графики | #7555FF (+ #A08CFF) | #5A3BE0 |
| `accent` (+ `-text`) | редкие выделения | #B64DFF (+ #C77DFF) | #8A2BD0 |
| `success` / `warning` / `danger` / `info` | статусы (fg/soft/border) | #22C58B / #F5B942 / #F06476 / #5A9BFF | #0B7A55 / #8A5A00 / #C42F45 / #2D5FD0 |
| `gradient-brand` | **только** декор: рамка ключевой плитки, активный узел DAG, индикатор прогресса, логотип | `linear-gradient(135deg,#00CFFF,#4969FF 50%,#7555FF)` | `linear-gradient(135deg,#006E8C,#3A57E8 50%,#5A3BE0)` |
| `glass-bg` / `glass-border` | стекло (4.7) | rgb(24 34 53 / .88) / rgb(73 105 255 / .24) | rgb(255 255 255 / .92) / rgb(58 87 232 / .18) |

Запреты: текст на градиенте; статус только цветом; цвета ролей из статусных оттенков (зелёный/жёлтый/красный зарезервированы за статусами).

### 5.2 Семантика статусов и 5 состояний верификации

**Базовые тона** (каждый статус = тон + иконка + текстовая метка; никогда только цвет):

| Тон | Иконка (Lucide) | Использование |
|---|---|---|
| neutral | `Circle` | CREATED, PENDING, QUEUED, CANCELLED |
| info / running | `LoaderCircle` (статичная при reduced motion) | RUNNING, LEASED, STARTING, DISCOVERY, PLANNED, VERIFYING |
| success | `CircleCheck` | только подтверждённые: COMPLETED (run), SUCCEEDED (task), gate PASS **с evidence** |
| warning | `TriangleAlert` / `PauseCircle` | AWAITING_APPROVAL, PAUSED, RETRY_WAIT, RATE_LIMITED, DEGRADED, MANUAL_REVIEW |
| danger | `CircleX` / `OctagonX` | FAILED, CRASHED, TIMED_OUT, FAIL, critical/high |
| blocked (warning-контур, штриховка) | `Ban` | BLOCKED, AUTH_REQUIRED, UNAVAILABLE |

**Пять состояний верификации (Quality Gates, Security, Compliance, плитки Dashboard).** Производное отображение поверх контрактных статусов (`GateExecutionStatus`, `EvidenceVerificationStatus`, `ComplianceStatus`); исходный статус контракта всегда виден в подсказке/деталях.

| Состояние UI | Метка (ru) | Условие | Тон / иконка | Паттерн |
|---|---|---|---|---|
| **PASS** | Пройдено | gate `PASS` **и** `evidence_id ≠ null` **и** evidence `VERIFIED` (если загружено); compliance `PASS` с evidence | success / `ShieldCheck` | сплошная заливка |
| **FAIL** | Не пройдено | gate `FAIL`; compliance `FAIL`; verdict FAIL | danger / `ShieldX` | сплошная |
| **BLOCKED** | Заблокировано | gate `ERROR` (проверка не смогла выполниться) **или** задача/run `BLOCKED` до гейта | blocked / `Ban` | диагональная штриховка |
| **NOT RUN** | Не запускалось | обязательный gate без выполнения, `PENDING`, `SKIPPED` (SKIPPED ≠ PASS) | neutral / `CircleDashed` | пунктирный контур |
| **INSUFFICIENT EVIDENCE** | Недостаточно доказательств | gate `PASS` без evidence или evidence `PENDING/UNVERIFIED/REJECTED`; compliance `UNVERIFIED` | warning / `FileQuestion` | контур + точка |
| (вспомогательные) | Выполняется / Не применимо / Ручная проверка | `RUNNING`; `NOT_APPLICABLE` (+ причина); `MANUAL_REVIEW` | info / neutral / warning | — |

Сопоставление ERROR→BLOCKED и SKIPPED→NOT RUN — предложение (D-8). Правило: зелёный «Пройдено» невозможен без evidence (юнит-тест на функцию отображения).

**Severity findings:** critical (danger, `OctagonAlert`, «Критическая»), high (danger, `TriangleAlert`), medium (warning, `CircleAlert`), low (info, `Info`), info (neutral, `Info`) — метка текстом всегда.

### 5.3 Типографика

| Роль | Шрифт | Размер / межстрочный | Вес | Применение |
|---|---|---|---|---|
| display | Inter Variable | 30 / 36 | 600 | заголовок Dashboard (≥ lg) |
| h1 | Inter | 24 / 32 | 600 | заголовок экрана |
| h2 | Inter | 20 / 28 | 600 | секции |
| h3 | Inter | 16 / 24 | 600 | заголовок плитки/панели |
| body | Inter | 16 / 24 | 400 | формы, прозаические блоки |
| body-dense | Inter | 14 / 20 | 400 | таблицы, панели Mission Control, списки |
| caption | Inter | 12 / 16 | 500 | мета, бейджи (минимум; не для основного содержимого) |
| metric | Inter, `tabular-nums` | 28–36 / 1.1 | 600 | числа плиток |
| code | JetBrains Mono Variable | 13 / 20 | 400 | CLI Output, diff, хэши, пути |
| code-sm | JetBrains Mono | 12 / 18 | 400 | SHA, ID в таблицах |

Правила: `font-feature-settings: "cv11","ss01"` для Inter (однозначные цифры), `tabular-nums` для всех чисел и времени; масштабирование до 200 % без потери функций (1.4.4); межстрочный и межбуквенный интервал переопределяемы (1.4.12); пользовательская «плотность» (comfortable/compact) в Settings › Внешний вид меняет только отступы и `body-dense`, не ниже 14 px.

### 5.4 Сетка, отступы, радиусы, возвышение

| Токен | Значения |
|---|---|
| Spacing (4 px) | 0, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64 |
| Радиусы | `r-sm` 6 (поля, кнопки), `r-md` 10 (карточки, панели), `r-lg` 14 (плитки bento, диалоги), `r-full` (пилюли, аватары) |
| Возвышение | `e0` плоско (border); `e1` карточка: border + `0 1px 2px rgb(0 0 0/.24)`; `e2` поповер/меню: `0 8px 24px -8px rgb(0 0 0/.5)`; `e3` модальное окно: `0 24px 48px -12px rgb(0 0 0/.6)`; в светлой теме — те же уровни с прозрачностью 0,12–0,2 |
| Glow (только ключевые элементы) | `0 0 0 1px gradient-brand` + `0 0 24px -8px rgb(0 207 255/.35)`; не на статусах |
| Цели касания | ≥ 44×44 для основных контролов (как сейчас), ≥ 24×24 минимум (WCAG 2.5.8) в плотных таблицах |
| Bento-сетка | 12 колонок, gap 16 (≥ xl 20); плитки 3/4/6/8/12 колонок; высоты кратны 8 |

### 5.5 Motion

| Токен | Значение | Применение |
|---|---|---|
| `--dur-instant` | 120 ms | hover, нажатие, фокус |
| `--dur-base` | 180 ms | раскрытие меню/поповеров, смена вкладок |
| `--dur-slow` | 250 ms | sheet/drawer, сворачивание панелей |
| `--ease-standard` | cubic-bezier(0.2, 0, 0, 1) | общее |
| `--ease-exit` | cubic-bezier(0.4, 0, 1, 1) | закрытие |

Правила: анимируются только `opacity`/`transform`; никаких бесконечных анимаций, кроме индикатора «идёт поток» (и он статичен при `prefers-reduced-motion`); анимация потока рёбер React Flow запрещена (как сейчас); новые элементы ленты появляются без сдвига позиции чтения (sticky-to-bottom только если пользователь у конца, иначе кнопка «N новых»); глобальный reduced-motion сброс в `index.css` сохраняется.

### 5.6 Иконография

Lucide (`lucide-react`), stroke 1,75, размеры 16/20/24; цвет `currentColor`. Компонент `Icon` сохраняет текущий API (`name`), внутри — карта имя → Lucide-компонент (типобезопасно), декоративные иконки `aria-hidden`, смысловые — с `aria-label`/текстом рядом. Бренд-логотипы Codex/Claude **не используются** (товарные знаки; не копировать чужие UI) — текстовые метки «Codex CLI», «Claude Code CLI» + нейтральный глиф `Terminal` и разный рисунок индикатора.

### 5.7 Система идентичности агентов (по `AgentRole`)

Идентичность = **иконка + код + название** (основное) и цвет семейства (вторичное). Цвета ролей не используют статусные зелёный/жёлтый/красный. Семейства выбраны по фазе конвейера и различимы по светлоте (проверить симуляцией дейтеранопии/протанопии в фазе 1).

| Роль | Код | Название (ru) | Lucide (проверить имена в 1.61) | Семейство / цвет (dark · light) |
|---|---|---|---|---|
| requirements | REQ | Требования | `ListChecks` | Discover · #A08CFF · #5A3BE0 |
| architect | ARC | Архитектор | `DraftingCompass` | Discover |
| planner | PLN | Планировщик | `Workflow` | Discover |
| developer | DEV | Разработчик | `CodeXml` | Build · #00CFFF · #006E8C |
| database | DB | База данных | `Database` | Build |
| devops | OPS | DevOps | `Container` | Build |
| reviewer | REV | Ревьюер | `ScanEye` | Verify · #C77DFF · #8A2BD0 |
| security | SEC | Безопасность | `ShieldHalf` | Verify |
| qa | QA | Тестирование | `FlaskConical` | Verify |
| performance | PRF | Производительность | `Gauge` | Verify |
| maintenance | MNT | Сопровождение | `Wrench` | Sustain · #5A9BFF · #2D5FD0 |
| documentation | DOC | Документация | `BookOpen` | Sustain |

Компонент `AgentBadge`: квадрат 24/32 px с иконкой на soft-фоне семейства + код (моно, 12 px) + название (опц.) + провайдер-исполнитель отдельным чипом (`Codex CLI`/`Claude Code CLI`), когда известен (`current_provider`, `attempt.provider`). Карточка роли не имеет «онлайн»-статуса; состояние — «назначено задач: N (по активным runs)» или «нет назначенных задач».

### 5.8 Адаптивность

| Ширина | Sidebar | Top bar | Main | Правая панель | Mission Control |
|---|---|---|---|---|---|
| **375** (< 640) | drawer (Dialog) | логотип-знак, меню, поиск-иконка, уведомления, профиль; статусы CLI — в меню | 1 колонка, gutter 16 | bottom sheet | вкладки: Граф / Лента / Инспектор; Command Center — закреплён снизу |
| **768** (md) | rail 64 px (иконки + tooltip), раскрывается overlay | + переключатель проекта (сокращённый), чипы CLI (точка + код) | 1–2 колонки bento | sheet справа (overlay) | 2 панели: Лента + (Граф/Инспектор вкладками) |
| **1366** (xl) | 240 px, сворачивается в rail | полная | 12-колоночная bento | 320 px, сворачиваемая (push) | 3 панели resizable: Navigator 280 / Лента / Inspector 340 |
| **1920** (3xl, новый breakpoint 1920) | 256 px | полная | max-width для прозы 72ch; данные — во всю ширину | 360–400 px | 3 панели + Command Center снизу, split view diff |

Требования: нет горизонтального скролла страницы на всех 4 ширинах (широкие таблицы/код — собственный скролл-контейнер с `tabindex="0"` и подписью); reflow 320 CSS px (1.4.10); ориентация не фиксирована.

### 5.9 Доступность (WCAG 2.2 AA) — правила

| Критерий | Правило |
|---|---|
| 1.4.3 / 1.4.11 | Контраст по 4.5; `tokens.test.ts` проверяет все пары, включая soft и границы |
| 1.4.1 | Статус/роль/severity — всегда текст + иконка/паттерн |
| 2.1.1 / 2.1.2 | Всё с клавиатуры: панели (separator ← → Home End, Enter — свернуть), DAG (существующий список-альтернатива), палитра, вкладки (roving) |
| 2.4.3 / 2.4.7 | Порядок фокуса: skip link → top bar → sidebar → main → правая панель; видимое кольцо 2 px + offset |
| **2.4.11** Focus Not Obscured | sticky top bar и Command Center: `scroll-padding-top/bottom`; правая панель в overlay-режиме возвращает фокус |
| **2.5.7** Dragging | Ресайз панелей — также клавиатура и кнопки «свернуть/развернуть/сбросить»; DAG — зум/сдвиг кнопками |
| **2.5.8** Target size | ≥ 24×24 везде, основные — 44×44 |
| **3.2.6** Consistent Help | справка (`?`) и ссылка на документацию — в одном месте (top bar) на всех экранах |
| **3.3.7 / 3.3.8** | Не требовать повторного ввода (Command Center сохраняет черновик); вход и step-up — без когнитивных тестов, разрешено вставлять пароль/менеджеры паролей |
| 4.1.3 | Live-регионы: новые события чата/статусы директив — вежливые и агрегированные (не на каждое событие CLI); toasts — Radix |
| Виртуализация | `role="log"`/`feed` с `aria-busy` при подгрузке, `aria-setsize/posinset`; поиск по содержимому — через фильтр API (`q`), т. к. Ctrl+F не видит невиртуализированное |
| Темы | `forced-colors: active` — системные цвета, границы видимы; `prefers-reduced-transparency` — без стекла |

---

## 6. Карта экранов, IA и ключевые сценарии

### 6.1 Навигация: новые пункты → существующие маршруты

| # | Пункт sidebar | Маршрут (внутри `/w/:workspaceId/`) | Было | Действие |
|---|---|---|---|---|
| 1 | Dashboard | `dashboard` | `dashboard` | редизайн (bento) |
| 2 | Projects | `projects`, `projects/new`, `projects/:projectId[?tab=]` | то же | вкладки проекта; Code Review: `projects/:projectId/runs/:runId/review[?task=&file=]` (новый) |
| 3 | AI Mission Control | `mission-control` (выбор активного run) → `projects/:projectId/runs/:runId[?view=&task=&panel=]` | `RunPage`, `…/graph`, `…/chat`, `…/tasks/:taskId` | объединение; старые пути — редиректы с параметрами |
| 4 | Agents | `agents`, `agents/:role` | — | новый |
| 5 | Workflows | `runs` | `runs` («Запуски») | переименование подписи; шаблоны режимов — справка |
| 6 | Code Intelligence | `code[?project=&repo=&commit=&path=]` | части `ProjectDetailPage`/`RepositoryExplorer` | новый раздел, переиспользует Explorer |
| 7 | Security Center | `security`, `security/findings/:findingId` | `quality/findings/:findingId` | новый (findings/evidence); старый путь — редирект |
| 8 | Quality Gates | `quality`, `quality/runs/:runId`, `quality/evidence/:evidenceId` | то же | редизайн, 5 состояний |
| 9 | Standards & Compliance | `compliance/**` | то же | редизайн |
| 10 | GitHub Integration | `integrations` | `integrations` | рестайл |
| 11 | Analytics | `analytics` | `analytics` | рестайл |
| 12 | Documentation | `knowledge` | `knowledge` («Знания») | переименование подписи |
| 13 | Settings | `settings[?section=workspace\|members\|providers\|audit\|appearance]` | `settings`, `providers`, `audit` | подразделы; `providers`/`audit` — редиректы |
| — | Approvals | `approvals`, `approvals/:id` | то же | вне 13 пунктов: top bar (бейдж) + плитка; **D-3** — вернуть ли в sidebar |
| — | Workspaces | `/workspaces`, `/workspaces/new` | то же | через переключатель |

Группы sidebar: **Обзор** (Dashboard, Projects), **Исполнение** (Mission Control, Agents, Workflows), **Код и качество** (Code Intelligence, Security Center, Quality Gates, Standards & Compliance), **Платформа** (GitHub, Analytics, Documentation, Settings). `g`-сочетания сохраняются для существующих букв; новые: Mission Control `m`, Agents `e` (agEnts — согласовать), Code Intelligence `x`, Security `y` — финализировать в фазе 2 без конфликтов (тест `routes.test.ts`).

### 6.2 Компоновка Mission Control

```
┌ Top bar ─────────────────────────────────────────────────────────────────────┐
├ Navigator (DAG / список) ┬ Live Agent Chat ─────────────────────┬ Agent Inspector ┤
│ run: статус, plan vN     │ [Беседа][CLI][Система][Поручения][Evidence]           │
│ React Flow DAG           │ фильтры: поток · роль · задача · поиск │ задача, попытки, │
│ ↔ текстовый список       │ виртуализированная лента             │ ACP, evidence,   │
│                          │                                      │ gates, (context  │
│                          ├ User Command Center ─────────────────┤  pack — B3)      │
│                          │ тип поручения · цель · текст → impact preview → confirm │
└──────────────────────────┴──────────────────────────────────────┴─────────────────┘
```

### 6.3 Ключевые сценарии (сохранение приёмочных AC)

| Сценарий | Шаги в новом UI | AC |
|---|---|---|
| Создать проект и запуск | Projects → «Новый проект» → привязать репозиторий (local_path) → «Новый запуск» (режим, исполнители, профиль качества) → Mission Control | AC-01, AC-03 |
| Наблюдать работу агентов | Mission Control: DAG live, «Беседа» (handoff developer → reviewer), «CLI» (санитизированный вывод) | AC-05 |
| Поручение во время выполнения | Command Center → CHANGE_REQUEST → impact preview → подтвердить → статусы «доставлено → подтверждено → выполнено», план vN+1 в Navigator | AC-06 |
| Восстановление после разрыва | индикатор потока (offline/reconnecting/resync) в шапке ленты; без дублей | AC-08 |
| Ревью изменений | Mission Control/Projects → Code Review: файлы diff, Monaco side-by-side, маркеры findings, gates (5 состояний), «Утвердить» (approval) / «Запросить изменения» (CHANGE_REQUEST) | AC-04, AC-11, AC-13 |
| Разобрать находку | Security Center → фильтр severity/CWE/источник → finding → evidence → переход статуса (право, step-up при необходимости) | §14 |
| Проверить провайдеров | Чип CLI в top bar → Settings › CLI Providers → probe → статус проверки | AC-16 |
| Утвердить опасное действие | Уведомление → Approvals → деталь (контекст, риск) → решение (four-eyes, step-up) | AC-13 |

---

## 7. Поэтапный план реализации

### 7.1 Фазы frontend (без изменений backend)

Общие проверки **каждой** фазы (DoD): `pnpm typecheck`, `pnpm lint` (0 warnings), `pnpm test`, `pnpm e2e` (mocked: axe serious/critical = 0 в светлой и тёмной теме, моки валидируются по контракту), `pnpm check:api`, `pnpm build` + бюджет размера; Playwright-проекты **375 / 768 / 1366 / 1920** с проверкой `scrollWidth ≤ innerWidth` (нет горизонтального переполнения); прогон с `reducedMotion: 'reduce'`; `forced-colors` smoke; обновлённые `README.md`/i18n; никаких фиктивных данных вне `e2e/mocked`.

| Фаза | Содержание | Критерии приёмки | Доп. проверки | Оценка |
|---|---|---|---|---|
| **F0. Решения** | Утверждение плана; дополнение к ADR-0014 (WCAG 2.2, зависимости, шрифты, токены, отказ от Recharts/Motion/Zustand); решения D-1…D-8 | ADR-дополнение Accepted; список GAP и фаз B* согласован | — | 0,5–1 д |
| **F1. Фундамент дизайн-системы** | `tokens.css` (dark/light по 5.1), шрифты `@fontsource-variable/*`, Lucide за API `Icon`, motion/elevation/radius токены, `tailwind.config.ts`; рестайл `components/ui` (Button, Card, Badge, Tabs, Table, Dialog, Menu, Toast, Tooltip), новые: `StatusPill`, `VerificationBadge` (5 состояний), `SeverityBadge`, `AgentBadge`, `ProviderChip`, `Tile`, `DataUnavailable`, `Metric`, `Sparkline`; SVG-знак (временный вариант по D-7) | Все пары токенов в `tokens.test.ts` ≥ AA (текст 4,5; не-текст 3); «PASS без evidence» невозможен (unit); визуально все существующие экраны работают в новой теме | axe 2 темы на всех текущих спеках; размер шрифтов измерен; SBOM обновлён | 4–5 д |
| **F2. Оболочка** | Sidebar (13 пунктов, группы, rail, бейджи), top bar (знак, workspace/project switcher, поиск, Command Palette Ctrl/Cmd+K, чипы Codex/Claude, уведомления, профиль), правая контекстная панель, `react-resizable-panels`, `lib/layoutPrefs.ts`, редиректы старых путей, новые `g`-сочетания | Палитра доступна (combobox ARIA, Esc, возврат фокуса); панели управляются клавиатурой и кнопками (2.5.7); раскладка сохраняется и сбрасывается; все старые deep-links открывают тот же контент | e2e: клавиатурный обход, mobile drawer, palette, persisted layout после reload; `routes.test.ts` без конфликтов букв | 5–6 д |
| **F3. Bento Dashboard** | 13 плиток по 3.2: реальные — analytics/listRuns/listApprovals/listProviders; GAP-плитки — `DataUnavailable` или скрыты (System Health) | Каждое число имеет подпись источника/окна/выборки; `null` ≠ 0; нет «Quality Score» без D-6 | e2e с пустыми/ошибочными ответами каждой операции; snapshot 4 ширин | 3–4 д |
| **F4. AI Mission Control** | Объединение Run/Graph/Chat/Task в один экран; 5 видов ленты + потоки ТЗ как фильтры; Command Center; Agent Inspector; виртуализация (`@tanstack/react-virtual`) ленты/CLI | AC-05/06/08 live проходят; 10 000 строк CLI без деградации (≥ 50 fps прокрутка на референсной машине, измерить); «доставлено ≠ выполнено» видно | **`pnpm e2e:live`** (стенд P7-14); обновлённые mocked chat/run/run-graph/tasks спеки; a11y виртуальных списков | 7–9 д |
| **F5. Agents, Workflows, CLI Providers** | Каталог 12 ролей (5.7) + метрики `by_role`; Workflows (runs) рестайл + справка конвейеров; Settings › CLI Providers (перенос), чипы статуса | Ни одна роль не показана «онлайн»; квоты/лимиты не отображаются; провайдер — «проверено N мин назад» | e2e: роли с нулевой статистикой, провайдер AUTH_REQUIRED/UNAVAILABLE | 3–4 д |
| **F6. Projects & Code Review Workspace** | Вкладки проекта; Code Review: explorer diff, Monaco side-by-side (lazy), маркеры findings, gates/tests/security, approve/request changes через approvals/directives | Нет shell-действий; без approval кнопка «Утвердить» отсутствует; request changes = CHANGE_REQUEST с impact preview | e2e mocked review flow; Monaco грузится только по требованию (проверка сетевых запросов) | 5–7 д |
| **F7. Security Center, Quality Gates, Standards** | Разделение Security/Quality; 5 состояний (5.2); фильтры severity/CWE/source/control; матрица compliance, «нет evidence», баннер «не сертификация» | Тест-таблица всех комбинаций gate status × evidence → состояние; SKIPPED никогда не зелёный | e2e: смешанные статусы, большие списки (виртуализация) | 4–5 д |
| **F8. Code Intelligence (UI-часть)** | Раздел: explorer (переиспользование), статус индекса, knowledge, история commits, агрегаты `analytics.context`; места под B3 — `DataUnavailable` | Нет пустых «заглушек-демо» | e2e | 2–3 д |
| **F9. Hardening и выпуск** | Аудит WCAG 2.2 (ручной: NVDA/VoiceOver, клавиатура, zoom 200 %, reflow 320), производительность, визуальная регрессия 4 ширин × 2 темы, документация (`apps/web/README.md`, этот документ) | 0 серьёзных a11y-нарушений; бюджет бандла; все e2e (mocked + live) зелёные | полный прогон | 3–4 д |

Итого frontend: **≈ 37–48 человеко-дней** (без фаз B*).

#### Статус фаз

| Фаза | Статус | Итог |
|---|---|---|
| F1 | ✅ выполнена 2026-11-09 | `styles/tokens.css` (тёмная по умолчанию / светлая / как в системе; 5 состояний верификации, семейства ролей, стекло α 0,88/0,92 с непрозрачным fallback, градиент — декор, тени e1–e3, радиусы 6/10/14, motion 120/180/250 мс → 0 при reduced motion), `styles/fonts.css` (Inter/JetBrains Mono self-hosted, только latin/cyrillic; CSP без изменений), `lib/theme.ts`, `lib/verification.ts` (D-8, тест всех комбинаций), `lib/agentRoles.ts`; Lucide за API `Icon`; кит: Button, Card (glass/featured), Badge/Callout, StatusBadge, VerificationBadge, SeverityBadge, AgentBadge/AgentAvatar, DataUnavailable, Skeleton, Kbd, Tooltip, Toast, Tabs (line/pills), Dialog, Menu, Popover, Table (density), VirtualList, Resizable (сохранение размеров); временный SVG-знак (D-7) + favicon; справочник `/w/:id/settings/design-system` (вне навигации). `tokens.test.ts` — все пары ≥ AA в обеих темах; mocked e2e `design-system.spec.ts` (тема, reduced motion, 375/768/1366/1920, axe, клавиатура, forced-colors). Начальный JS 232,4 kB gzip (бюджет 260); шрифты — ≈ 119 kB woff2 на типичную русскую страницу (252 kB все файлы). Не вошло в F1 и перенесено: `ProviderChip`, `Tile`, `Metric`, `Sparkline` — в F3/F5 вместе с экранами; `cmdk` — в F2 (Command Palette); подключение `VirtualList`/`Resizable` к экранам — F2/F4; симуляция дейтеранопии/протанопии для цветов ролей — ручная проверка в F9 (сейчас роли различимы глифом и кодом). |
| F2 | ✅ выполнена 2026-11-09 | Оболочка (`apps/web/src/layout/*`, README «Оболочка (F2)»). IA D-3: 14 пунктов в 4 группах (Обзор · Исполнение · Код и качество · Платформа), счётчик approvals «ожидают вашего решения»; CLI-провайдеры и аудит — `settings/providers`, `settings/audit` (+ `settings/appearance`: тема, режим навигации, сброс раскладки); старые `providers`/`audit` — редиректы с query/hash; новые `g m/e/x/y`; Агенты/Code Intelligence/Центр безопасности — «в разработке» с фазой и ссылками на существующие экраны; AI Mission Control — выбор активного run (split view, задачи, сводка в контекстной панели) и резолвер `?run=<id>`. Sidebar: rail/развёрнут с сохранением, авто-rail 768–1279, drawer < 768. Верхняя панель: знак, workspace/проект, поиск `GET /search` (B6, ARIA combobox), Command Palette Ctrl/⌘+K (`cmdk` 1.1.1, ленивый чанк ≈ 7 kB gzip), чипы Codex/Claude только из `listProviders` (без квот), уведомления из `GET /workspaces/{id}/events` + pending approvals (прочитано — localStorage устройства до B8; один поток на браузер через Web Locks/BroadcastChannel из-за лимита 5 SSE), профиль. Рабочая область без `max-w-6xl` (ширина по `handle.width`), правая контекстная панель (slot `ContextPanel`, «Нет выбранного контекста», ширина 280–560 px с клавиатуры, push ≥ 1280 / overlay), переходы экранов WAAPI 180 мс (0 при reduced motion). Отклонения от плана: подразделы Настроек — путями (`settings/providers`), а не `?section=` (у каждого свой h1/заголовок/права и deep link); подписи разделов на русском (Процессы, Гейты качества, Документация, Интеграция с GitHub), «AI Mission Control» и «Code Intelligence» — как у владельца; раскладка хранится по устройству (не per user+workspace). Начальный JS 246,8 kB gzip (бюджет 260). Попутно: стабилизирован `analytics.spec` (фильтры читают текущий URL документа, а не `prev` из transition), поздний 401 фона не стирает `next=` на странице входа. Тесты: unit 206, mocked e2e 78 (+ `app-shell.spec.ts`), live AC-05/06/08 + smoke. |
| F3 | ✅ выполнена 2026-11-09 | Bento Dashboard (`apps/web/src/features/dashboard/*`, README «Bento Dashboard (F3)»): hero со строками бренда владельца (англ., `lang="en"`), 8 KPI только из реальных операций с подписью источника/окна/выборки — активные проекты (`listProjects`), работающие агенты (B1 `GET /tasks?status=LEASED&status=RUNNING`), подтверждённо завершённые задачи и успешность процессов (analytics, окно 30 дней), качество — измеренные доли `verified_completion_rate` и доля гейтов со статусом PASS (без «Quality Score», D-6), открытые находки по серьёзности (B1 `GET /findings`), ожидающие approvals, состояние CLI-провайдеров (`listProviders` + доля успешных исполнений); разделы — лента активности (B1 `GET /activity` + поток workspace, фильтры по типам, виртуализация, «N новых» без сдвига чтения), активные процессы (прогресс только по SUCCEEDED), результаты агентов (`by_role`, `providers`, `review_loops`; разбивки ревью по ролям нет — `DataUnavailable`), обзор качества (5 состояний D-8 по агрегату: PASS контракта без статуса evidence → «Недостаточно доказательств», «Пройдено» по агрегату не выводится), состояние системы (B2, только `system:health` = owner/admin; «неизвестно» не становится «работает»). Кит: `Tile`, `Metric`, `Sparkline`, `Meter`, `Donut` (SVG/CSS, текстовые альтернативы). Экран — ленивый чанк 13,9 kB gzip; начальный JS 248,8 kB gzip (бюджет 260). Отклонения: 4 заголовка разделов переведены (Лента активности AI, Результаты агентов, Обзор качества, Состояние системы); контекстная панель на Dashboard не включается. Тесты: unit 234, mocked e2e 82 (+ `dashboard.spec.ts`), live AC-05/06/08 + smoke. |
| F4 | ✅ выполнена 2026-11-09 | AI Mission Control (`apps/web/src/features/missionControl/*`, README «AI Mission Control (F4)»): `mission-control?run=` — Workflow Navigator (активные run по потоку workspace, этапы FSM, «сейчас выполняют», DAG React Flow ↔ доступный список; узел — роль/провайдер/статус/попытки, прогресс только по проверкам с VERIFIED evidence (D-8), evidence и тесты, предупреждения, ожидание зависимостей/очередь, пульсация активного узла без анимации при reduced motion) · Live Agent Chat (5 видов: беседа агентов с потоками ТЗ как фильтрами и периодом, вывод CLI — `cli.output`, виртуализация, моноширинный, маркеры редакции/усечения; системные события — журнал run целиком с группами; поручения; evidence) · User Command Center (быстрые действия → ASK/NEW_TASK/CHANGE_REQUEST/REVIEW/PAUSE/RESUME/«вернуть на доработку» = CHANGE_REQUEST задаче; цель — все/роль/задача; `expected_run_version`, impact preview → подтверждение → доставлено/подтверждено/выполнено; критические — подтверждение до отправки, step-up; черновик) · Agent Inspector в контекстной панели (сводка run и команды; задача — CLI-статус, время, метрики, 5 состояний проверок, ошибки, артефакты, события, Context Pack B3). Раскладка: ≥ 1366 — три панели (resizable, сохраняются), 768–1365 — две с вкладками, < 768 — одна с вкладками. Старые экраны run/графа/чата — редиректы с тем же выбором и фильтрами, Task Detail — отдельный экран. Бизнес-логика переиспользована (useRunTasks, chatModel/useChatFeed, DirectivesPanel/DirectiveDetails/useCreateDirective, describeEvent, TaskSections, StepUp). Бандл: экраны областей — `lazyPage` (модуль + namespace i18n), в начальном бандле только common/nav/ds; начальный JS 248,8 → **176,3 kB gzip** (Mission Control — ленивый чанк 26 kB gzip). Отклонения: плейсхолдер Command Center по-русски (англ. оригинал — в README); «10 000 строк CLI ≥ 50 fps» не измерялось на референсной машине (виртуализация проверена e2e: 3000 строк — в DOM только окно); период в «Беседе» — среди загруженных сообщений (в API нет фильтра времени); прогресс узла — доля проверок, а не «шагов» (шагов в API нет). Тесты: unit 255 (+ `missionControl.test.ts`, `namespaces.test.ts`), mocked e2e 87 (+ `mission-control.spec.ts`), live AC-05/06/08 + smoke на новом экране. |
| F5 | ✅ выполнена 2026-11-09 | Агенты (`apps/web/src/features/agents/*`, README «Агенты, Процессы, CLI-провайдеры (F5)»): каталог 12 ролей `AgentRole` (D-2) сеткой/списком (URL + устройство), профиль из B7 `GET /agent-profiles`, активность только по задачам B1 `GET /tasks?status=LEASED&status=RUNNING` («активна, когда выполняет задачу», без «онлайн»; неполная выборка → «неизвестно»), результаты `analytics.tasks.by_role` за 30 дней (нет строки — «нет попыток», не нули), провайдеры `eligible_providers`; карточка роли `agents/:role` — права/инструменты/ACP/уровни/независимая проверка/шаблон (версия, sha256, справочник), провайдеры по `allowed_roles` со ссылкой в Настройки › CLI-провайдеры, история задач `GET /tasks?agent_role=`. Процессы — прежний список runs + режим и фильтр `mode`, этап FSM, прогресс только по SUCCEEDED (`getRun.task_counts`, первые 20), длительность, причина блокировки, ожидающие одобрения, переход в Mission Control, справка конвейеров (AGENT_ROLES §4). Настройки › CLI-провайдеры — состояние/«проверено N назад», вход (только подсказка), предупреждения UNTESTED/UNSUPPORTED, установки, измеренное использование `analytics.providers` (токены только от CLI, без квот), история исполнений B1 `GET /providers/{id}/executions` (фильтр статуса, VirtualList, подгрузка), прежние probe и форма настроек со step-up. Начальный JS 176,8 kB gzip (экраны — ленивые чанки 2,8–6,8 kB). Отклонения/ограничения: лента `GET /activity` не фильтруется по роли — «история активности» роли = её задачи; изменить профиль роли нельзя (API нет) — только просмотр; прогресс процесса — доля SUCCEEDED-задач (как Dashboard), а не проверок; шаблоны конвейеров — статическая справка из документации. Тесты: unit 279, mocked e2e (+ `agents.spec.ts`, `workflows.spec.ts`, расширен `providers.spec.ts`). |
| F6 | ✅ выполнена 2026-11-09 | Projects и Code Review Workspace (`apps/web/src/features/projects/*`, `features/review/*`, README «Projects и Code Review Workspace (F6)»). Список проектов (карточки, фильтр); рабочее пространство проекта — вкладки в URL: обзор (режим документации, 11 режимов запуска, AI-исполнители из `listProviders` без квот + ссылка на `settings/providers`, политика качества: профиль/security_profile/routing policy + контроли, последние запуски), репозитории (local/GitHub с `local_path`, GitHub 202 → проверка воркером до SUCCEEDED/FAILED), файлы и изменения (Repository Explorer с веткой/commit), ветки (B3/explorer), worktrees (B4, без путей хоста), запуски (→ Mission Control / Code Review), PR, технологии. Code Review Workspace `projects/:pid/review/:runId?task=`: diff артефактов `git_diff` (Monaco side-by-side/inline — ленивый чанк, текстовый вид с комментариями под строками), File Explorer, inline-треды B5 (создать/ответить/править своё с историей/resolve/unresolve, If-Match, «устарело» относительно commit diff, только текст), находки и тесты/безопасность в 5 состояниях D-8, отправка ревью (Approve — только существующий PENDING approval с `can_decide`, видно какое; Request changes — developer+, поручение CHANGE_REQUEST с impact preview и диалогом хода; Comment; step-up), RBAC (reviewer без REQUEST_CHANGES, viewer — чтение), live `review.comment`, клавиатура (файлы ↑/↓ и `]`/`[`, обсуждения `n`/`p`, комментарий к строке Tab+Enter, Ctrl+Enter), < 768 — текстовый/inline вид и файлы в drawer. Без shell-действий. Отклонения: три колонки ревью — только ≥ 1920 (на 1366 с sidebar и контекстной панелью колонке diff не хватает ширины; панели ревью — под diff); маркеры тредов в Monaco — в поле глифов, сами треды в режимах Monaco — списком под редактором (inline — в текстовом виде); перепривязки строк устаревших тредов нет (ADR-0015 §8). Начальный JS 177,2 kB gzip (CodeReviewPage 16,1 kB gzip, ProjectDetailPage 9,8 kB gzip — ленивые). |
| F7 | ✅ выполнена 2026-11-09 | Центр безопасности, Гейты качества, Стандарты и соответствие (`apps/web/src/features/security/*`, `features/quality/*`, `features/compliance/*`, README «Центр безопасности, Гейты качества, Стандарты и соответствие (F7)»). Центр безопасности вместо заглушки: находки workspace `GET /findings` (B1) — виртуализированный список с подгрузкой, серверные фильтры severity/status/source/project/run и клиентские CWE/контроль (в URL), распределение по серьёзности и статусам исправления, CWE/контроли только из `cwe_id`/`control_refs`, риски зависимостей (SCA/SBOM) и секреты (SECRETS) по `gate_type` из `getRunQuality` (первые 10 run выборки; остальное — «не классифицировано»), evidence проверок безопасности в 5 состояниях, решение по находке из панели (общий `FindingTransitions`: FSM, RBAC, If-Match, step-up). Гейты качества: обзор workspace по `analytics.gates` (окно 7/30/90 дней, проект) в 5 состояниях D-8 по группам lint/typecheck/build/unit/integration/e2e/review/coverage/security и типам — «Пройдено» только `passed_verified`; отчёт run — сводка групп и колонка состояния (evidence из `listRunEvidence`); evidence — карточки и правило «доказательство только VERIFIED». Стандарты: постоянный баннер «не сертификация», реестр стандартов из `listProjectControls` выбранного проекта (глобального реестра в API нет), фильтры стандарт/контроль/утверждение/применимость/блокирование; матрица run — состояние (PASS только с VERIFIED evidence), «Нет evidence»/«не подтверждено» с подсветкой и фильтром, отчёты JSON/Markdown. Тест-таблица всех комбинаций gate status × evidence → состояние (`gatesModel.test.ts`); e2e `security-center`, `quality-gates`, `standards` (axe light/dark, 375/768/1366/1920, reduced motion). Начальный JS 176,7 kB gzip. GAP: workspace-реестра стандартов, фильтра `cwe` в `GET /findings` и `gate_type` в `Finding` в API нет (обходятся выборкой/отчётами run, подписано в UI). |
| F8–F9 | не начаты | — |

### 7.2 Фазы backend (каждая — только после утверждения владельцем)

Для всех: изменение `packages/contracts/openapi.yaml` (+ `docs/api/openapi.yaml`), API_DESIGN (каталог операций, матрица прав), `pnpm gen:api`/`check:api`, contract/negative tests (AC-15), RLS/tenant isolation tests, аудит-политика `x-audit-action`, без смены архитектуры (read-модели существующих таблиц).

| Фаза | Операции | Закрывает | Изменение архитектуры | Оценка | Разблокирует |
|---|---|---|---|---|---|
| **B1. Агрегаты workspace** | `GET /tasks` (workspace, статус/роль/провайдер), `GET /findings` (workspace), `GET /providers/{id}/executions`, опц. `GET /activity`, опц. `Finding.remediation` | G-01…G-04, G-16 | нет (read; возможен индекс `run_event_log(workspace_id, created_at)` — миграция) | 3–5 д | F3/F5/F7 полные плитки |
| **B2. System health** | `GET /system/health` (admin): компоненты, `worker_hosts` heartbeat, outbox lag, глубина очередей | G-05 | нет (чтение Redis/БД из API) | 2–3 д | плитка System Health |
| **B3. Code Intelligence read API** | `…/symbols`, `…/dependencies`, `…/indexing-jobs`, `…/attempts/{id}/context-pack` | G-06…G-09 | нет (функции пакета уже есть) | 4–6 д | F8 полностью, Inspector «контекст агента» |
| **B4. Worktrees** | `GET …/worktrees` (read-only, без путей хоста) | G-10 | нет | 1–2 д | вкладка проекта |
| **B5. Review comments** | новая модель + CRUD + SSE-событие | G-11 | **да — новый ADR** (доставка агентам? ACL, аудит) | 6–10 д | inline-комментарии |
| **B6. Global search** | `GET /search` | G-12 | нет/малое (FTS по таблицам) | 3–5 д | поиск в top bar |
| **B7. Agent profiles** | `GET /agent-profiles` | G-13 | нет | 1 д | Agents: права ролей из данных |
| **B8. Notifications** | хранение/прочитано | G-15 | малое (таблица) | 3–4 д | серверные уведомления |

Рекомендуемый порядок: F0 → F1 → F2 → F3 (с GAP-состояниями) ‖ B1, B2 → F4 → F5 → F6 → F7 ‖ B3 → F8 → F9; B4–B8 — по решению владельца.

**B5 — выполнена 2026-11-09** (D-9, ADR-0015 «Inline-комментарии ревью кода», миграция 0019): треды/комментарии/история правок/ревью, 10 операций `reviews` (API_DESIGN §2.1, §5.6), SSE `review.comment`; комментарии агентам не доставляются — «Request changes» = поручение `CHANGE_REQUEST`, «Approve» = решение существующего approval. G-11 закрыт.

---

## 8. Решения, требуемые от владельца

| ID | Вопрос | Варианты | Рекомендация |
|---|---|---|---|
| D-1 | Цель доступности | (a) WCAG 2.2 AA + дополнение ADR-0014; (b) оставить 2.1 AA | (a) |
| D-2 | Имена 12 ролей в макетах | (a) строго `AgentRole` (ТЗ/контракт); (b) иные «маркетинговые» имена с маппингом | (a); маркетинговые имена — только как подзаголовок при необходимости |
| D-3 | Approvals / CLI Providers / Audit вне списка 13 пунктов | (a) Approvals — 14-й пункт sidebar; Providers и Audit — в Settings; (b) всё в Settings/top bar | (a): approvals — критичный рабочий поток (AC-13) |
| D-4 | Вкладки и split view | (a) внутри экранов (Mission Control, Code Review); (b) глобальные «документ-вкладки» как в IDE | (a) в v1 |
| D-5 | Command Palette | (a) `cmdk` 1.1.1; (b) своя на Radix | (a), если SCA без замечаний |
| D-6 | «Quality Score» | (a) не показывать скаляр, показывать измеренные доли; (b) утвердить формулу (веса gates/verified rate/findings) с раскрытием состава | (a) до утверждения (b) |
| D-7 | Логотип | (a) владелец предоставляет SVG-знак/светлую версию; (b) перерисовка знака в SVG по PNG с утверждением; (c) временно — текстовый wordmark + обрезанный прозрачный растровый знак | (b) или (a); (c) — на время F1–F2 |
| D-8 | Отображение статусов контракта на 5 состояний | ERROR→BLOCKED, SKIPPED/PENDING→NOT RUN, PASS без verified evidence→INSUFFICIENT EVIDENCE, UNVERIFIED→INSUFFICIENT EVIDENCE | принять |
| D-9 | Фазы backend B1–B8 | утвердить состав и порядок | B1, B2, B3 — да; B5 — отдельный ADR; B6–B8 — по приоритету |
| D-10 | Отклонение Recharts/Motion/Zustand и шрифт Geist | принять/пересмотреть | принять |
| D-11 | Дефект контракта (`openapi.yaml:3529`, flow-YAML с запятой в description) | исправить отдельной правкой контракта | исправить (вне редизайна) |
