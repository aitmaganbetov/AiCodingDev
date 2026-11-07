# ADR-0013: Размещение контрактов и генерация кода

- **Статус:** Proposed (разрешает расхождение мастер-промпта и ТЗ §21 — OQ-02)
- **Дата:** 2026-11-07

## Решение
1. Phase 0: черновики контрактов в `docs/api/openapi.yaml` и JSON Schema внутри `docs/protocols/*.md`.
2. Phase 1 (P1-03): единственный источник — `packages/contracts/` (`openapi.yaml`, `cli-execution-request.schema.json`, `cli-execution-result.schema.json`, `acp-envelope.schema.json`, `acp-payloads/*.schema.json`, `events.schema.json`). Файлы в `docs/` заменяются ссылками; дублирование запрещено.
3. Python: Pydantic-модели генерируются `datamodel-code-generator` из JSON Schema (сгенерированный код коммитится, CI проверяет отсутствие расхождений). FastAPI-приложение — **code-first проверяется против spec-first**: CI сравнивает экспортированную OpenAPI с `packages/contracts/openapi.yaml` (`oasdiff`), расхождение = ошибка.
4. TypeScript: типы клиента генерируются `openapi-typescript`.
5. Backward compatibility: `oasdiff breaking` и проверка JSON Schema-совместимости в CI; ломающие изменения — только с новой major-версией.

## Альтернатива
Code-first OpenAPI из FastAPI как источник — отвергнуто: контракты должны утверждаться до реализации (ТЗ §21).
