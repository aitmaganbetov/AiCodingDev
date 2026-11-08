# ADR-0012: Журнал внешних действий (side-effect ledger)

- **Статус:** Accepted (утверждено владельцем 2026-11-07)
- **Дата:** 2026-11-07

## Контекст
ТЗ §6, §16, AC-09: повтор/recovery не должны повторять подтверждённые внешние действия (merge, опасная миграция, push, PR).

## Решение
1. Таблица `external_actions` (`idempotency_key UNIQUE`, `status`: `PLANNED → EXECUTING → CONFIRMED | FAILED | COMPENSATED`).
2. Протокол: (a) в транзакции FSM вставить `PLANNED` с детерминированным ключом `sha256(run_id, task_id, action_type, target, plan_version)`; (b) перед вызовом — `EXECUTING` + fencing token аренды; (c) вызов внешней системы с её собственным idempotency-механизмом, где он есть (например, проверка существования PR по head-ветке); (d) `CONFIRMED` + `external_ref`.
3. Recovery для `EXECUTING`: сначала **запрос состояния** во внешней системе (существует ли PR, смёржен ли коммит, применена ли миграция по `alembic_version`), затем `CONFIRMED` или повтор; `CONFIRMED` никогда не повторяется.
4. Действия, требующие approval, ссылаются на `approval_id`; ledger проверяет `APPROVED` и неистёкший срок.

## Последствия
+ Детерминированная защита от дублей опасных действий. − Для каждого типа действия нужен reconciler-запрос.
