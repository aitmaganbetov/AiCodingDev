# ADR-0004: Централизованный ACP, evidence-first верификация, human approvals

- **Статус:** Accepted (ТЗ §2 п.4–5, §10, §14, §22)
- **Дата:** 2026-11-07

## Решение
1. Агенты не общаются напрямую. Каждое сообщение — ACP-конверт `acp/1.0`, публикуемый через Orchestrator (ACP Broker), который в одной транзакции валидирует схему, размер (≤ 32 KiB), ACL роль→тип→роль, policy, целостность `correlation_id`/`reply_to_id`, лимит review/fix-циклов (по умолчанию 3), сохраняет `agent_messages` + `outbox_events`.
2. Агенты «говорят» только через результат CLI-исполнения: адаптер извлекает структурированный блок ACP из вывода/файла-результата в worktree (формат — `CLI_ADAPTER_CONTRACT.md`); невалидное сообщение отклоняется и фиксируется как `warning`, а не исполняется.
3. **Evidence-first:** `claims` агента имеют статус `UNVERIFIED`, пока Verification Engine не подтвердит их собственным запуском проверок на конкретном commit SHA. Task → `SUCCEEDED` только после `VERIFYING` с PASS всех обязательных gates.
4. **Human approvals:** действия из списка `approval_action_type` требуют записи `approvals` со статусом `APPROVED` от пользователя с нужной ролью; requester ≠ approver для критических действий (four-eyes).
5. Агент не может сам авторизовать критическое действие: `DECISION_PROPOSAL` от агента порождает `approvals`, а не действие.

## Последствия
+ Полный audit trail, воспроизводимость, защита от prompt injection в межагентном канале.
− Задержка из-за брокера и approvals; компенсируется risk-based approval policy.

## Связанные требования
FR-ACP-*, FR-QG-*, FR-APR-*, AC-05, AC-11, AC-13.
