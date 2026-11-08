# ADR-0002: PostgreSQL как source of truth, Redis/Dramatiq как транспорт, transactional outbox

- **Статус:** Accepted (ТЗ §2 п.2, §16, §22)
- **Дата:** 2026-11-07

## Контекст
Состояние runs/tasks/approvals должно переживать падения процессов; доставка сообщений между компонентами — at-least-once (ТЗ §10, §16). Exactly-once для внешних процессов заявлять запрещено.

## Решение
1. **PostgreSQL 17** — единственный источник истины для всего состояния. Redis не хранит ничего, что нельзя восстановить из PG.
2. **Transactional outbox:** любое изменение состояния агрегата и запись `outbox_events` выполняются в одной транзакции. `worker-outbox` выбирает неопубликованные события (`SELECT … FOR UPDATE SKIP LOCKED`, partial index `published_at IS NULL`), публикует в Dramatiq (Redis broker), пишет `run_event_log` (для SSE) и помечает `published_at`.
3. **Dramatiq** — транспорт задач: актор получает только ID агрегата + `event_id`, перечитывает состояние из PG, проверяет `processed_events(consumer,event_id)` (идемпотентный consumer).
4. Redis также используется для: pub/sub пробуждения SSE, rate-limit счётчиков, короткоживущих кэшей. Redis AOF включён, но потеря Redis не теряет состояние — outbox переопубликует неподтверждённые события.
5. Dead-letter: после `max_retries` Dramatiq сообщение попадает в DLQ; reconciler периодически сверяет «застрявшие» агрегаты с PG.

## Альтернативы
- RabbitMQ/Kafka — лишняя инфраструктура для self-hosted. Celery — слабее гарантии ack/visibility, хуже типизация; Dramatiq проще и указан ТЗ.
- LISTEN/NOTIFY как единственный транспорт — нет персистентности/ретраев.

## Последствия
+ Нет «двойной записи»; восстановление после падения Redis.
− Дубликаты доставки неизбежны → все consumers обязаны быть идемпотентны (проверяется contract/chaos тестами).
− Нагрузка на PG от polling outbox — ограничивается batch + `NOTIFY` как подсказка.

## Связанные требования
FR-ORCH-08, NFR-REL-01..04, AC-08, AC-09.
