# ADR-0014: Архитектура frontend

- **Статус:** Accepted (утверждено владельцем 2026-11-07)
- **Дата:** 2026-11-07

## Решение
1. React 18+ + TypeScript (strict) + Vite + Tailwind CSS (ТЗ §3). SPA, отдаётся reverse proxy с того же origin, что и API (cookie SameSite=Strict, без CORS).
2. Данные: TanStack Query (кэш, инвалидация по SSE-событиям), React Router; типы API — генерация из OpenAPI (ADR-0013).
3. SSE: единый `EventSource`-менеджер на run, хранит `lastEventId`, дедупликация по `event_id`, экспоненциальный реконнект.
4. Run Graph — React Flow (`@xyflow/react`) с layout dagre/elk; Diff viewer — Monaco diff editor (lazy-load).
5. Компоненты доступности: Radix UI primitives (клавиатура, ARIA) + Tailwind; цель — WCAG 2.1 AA (проверка axe-core в Playwright). **Поправка 2026-11-09 (редизайн UI, D-1):** целевой уровень — WCAG 2.2 AA.
6. i18n: `i18next`, локаль `ru` по умолчанию, ключи сообщений вместо литералов (ТЗ: архитектура готова к i18n).
7. Тесты: Vitest + Testing Library (unit), Playwright (E2E, a11y).

## Последствия
Каждая новая зависимость фиксируется в SBOM и проходит SCA; список зависимостей утверждается в Phase 1.


## Поправка 2026-11-09 — редизайн UI «Neural Engineering Command Center»

Утверждено владельцем продукта (решения D-1…D-11, `docs/design/UI_REDESIGN_PLAN.md`): цель доступности — **WCAG 2.2 AA**; дополнительные зависимости — `lucide-react`, `@fontsource-variable/inter`, `@fontsource-variable/jetbrains-mono` (шрифты раздаются самим приложением, без CDN — CSP `default-src 'self'`), `@tanstack/react-virtual`, `react-resizable-panels`, `cmdk`; shadcn/ui — только как образцы компонентов поверх Radix (без зависимости: текущие версии требуют Tailwind 4 / React 19); Recharts, Motion, Zustand — не используются (графики — доступные SVG, анимации — CSS 120–250 мс с `prefers-reduced-motion`, состояние — TanStack Query + URL + localStorage).
