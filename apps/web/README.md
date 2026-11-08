# apps/web — web shell (P1-13)

SPA AiCodingDev: вход, layout, рабочие пространства и проекты. Архитектура — ADR-0014; правила API — `docs/api/API_DESIGN.md`; контракт — `packages/contracts/openapi.yaml` (единственный источник, ADR-0013).

## Команды (pnpm, Node ≥ 22.12)

| Команда | Что делает |
|---|---|
| `pnpm dev` | Vite dev server `127.0.0.1:5173`; `/api` проксируется на `AICD_API_URL` (по умолчанию `http://127.0.0.1:8000`) |
| `pnpm build` / `pnpm preview` | Production-сборка в `dist/` / просмотр на `127.0.0.1:4173` (тот же прокси) |
| `pnpm typecheck` | `tsc --noEmit` (strict, `noUncheckedIndexedAccess`) для приложения и конфигов/E2E |
| `pnpm lint` | ESLint flat config: typescript-eslint + jsx-a11y (strict) + react-hooks |
| `pnpm test` | Vitest + Testing Library (клиент API, формы, состояния) |
| `pnpm e2e` | Playwright против `vite preview` с моками `page.route`, валидируемыми по OpenAPI |
| `pnpm e2e:live` | Smoke против реального стенда; пропускается без `AICD_E2E_LIVE_URL` (нужны `AICD_E2E_EMAIL`, `AICD_E2E_PASSWORD`) |
| `pnpm gen:api` | `openapi-typescript` → `src/api/schema.d.ts` из `../../packages/contracts/openapi.yaml` |
| `pnpm check:api` | Drift check для CI: генерирует во временный файл и падает, если `schema.d.ts` устарел |

## Структура

- `src/api/client.ts` — fetch-клиент: база `/api/v1`, `credentials: 'same-origin'`, `X-CSRF-Token` (из `SessionInfo.csrf_token`, fallback — cookie `aicd_csrf`) на небезопасных методах, `Idempotency-Key` на POST-командах (`IdempotentIntent`: один ключ на намерение, тот же ключ при повторе), `X-Workspace-Id` для `/projects`, разбор `application/problem+json` в `ApiError`, 401 → переход на `/login?next=…`. Автоповторы — только безопасные (GET или POST с ключом: сетевая ошибка, `idempotency_key_in_flight`, 503 для GET).
- `src/api/schema.d.ts` — сгенерированные типы (не править вручную).
- `src/auth/` — сессия (`GET /auth/session`), охрана маршрутов, мост 401 → вход.
- `src/pages/` — Login, Workspaces (список + создание), Projects (список с cursor-пагинацией + создание), 404.
- `src/components/` — layout (skip link, переключатель workspace, меню пользователя, навигация только по реализованным разделам), состояния loading/empty/error, поля форм с ошибками по `errors[].pointer`.
- `src/i18n/ru.json` — все строки UI (локаль `ru` по умолчанию; новая локаль — ещё один ресурс).
- `e2e/support/contract.ts` — ajv (JSON Schema 2020-12) по схемам самого контракта: тела запросов UI, обязательные заголовки, CSRF и тела ответов моков; нарушения валят тест.

Права в UI (скрытие «Создать проект» для ролей без `project:create`) — только UX; авторизацию выполняет сервер.

## Зависимости (точные версии; обоснование)

Runtime:
- `react`, `react-dom` 18.3.1 — ADR-0014/ТЗ §3.
- `react-router` 7.18.4 — маршрутизация (ADR-0014); v8 требует React 19.
- `@tanstack/react-query` 5.104.5 — кэш/загрузка данных, infinite queries для cursor-пагинации (ADR-0014).
- `i18next` 26.5.7, `react-i18next` 17.1.5 — i18n, ключи вместо литералов (ADR-0014).

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

Не добавлены (минимальность): Radix UI (ADR-0014 п.5) — в shell нет сложных виджетов; переключатель workspace и меню пользователя сделаны по disclosure-паттерну на нативных `button`/ссылках. Подключить при появлении диалогов/комбобоксов (P7-01). `openapi-fetch` — свой клиент из ~300 строк закрывает CSRF/idempotency/problem.

## Замечания к контракту (не исправлялись здесь)

См. отчёт задачи P1-13; кратко: операции shell не объявляют `500`/`503` (problem `internal_error`/`service_unavailable` из API_DESIGN §6), поэтому E2E-мок ошибки списка использует объявленный `429`.

## Live E2E

```bash
AICD_E2E_LIVE_URL=https://aicd.local AICD_E2E_EMAIL=... AICD_E2E_PASSWORD=... pnpm e2e:live
```
SPA и API должны быть на одном origin (cookie `SameSite=Strict`, без CORS). Тест создаёт workspace `e2e-<suffix>` и проект в нём.
