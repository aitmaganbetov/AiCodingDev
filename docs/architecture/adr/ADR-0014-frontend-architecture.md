# ADR-0014: Архитектура frontend

- **Статус:** Proposed
- **Дата:** 2026-11-07

## Решение
1. React 18+ + TypeScript (strict) + Vite + Tailwind CSS (ТЗ §3). SPA, отдаётся reverse proxy с того же origin, что и API (cookie SameSite=Strict, без CORS).
2. Данные: TanStack Query (кэш, инвалидация по SSE-событиям), React Router; типы API — генерация из OpenAPI (ADR-0013).
3. SSE: единый `EventSource`-менеджер на run, хранит `lastEventId`, дедупликация по `event_id`, экспоненциальный реконнект.
4. Run Graph — React Flow (`@xyflow/react`) с layout dagre/elk; Diff viewer — Monaco diff editor (lazy-load).
5. Компоненты доступности: Radix UI primitives (клавиатура, ARIA) + Tailwind; цель — WCAG 2.1 AA (проверка axe-core в Playwright).
6. i18n: `i18next`, локаль `ru` по умолчанию, ключи сообщений вместо литералов (ТЗ: архитектура готова к i18n).
7. Тесты: Vitest + Testing Library (unit), Playwright (E2E, a11y).

## Последствия
Каждая новая зависимость фиксируется в SBOM и проходит SCA; список зависимостей утверждается в Phase 1.
