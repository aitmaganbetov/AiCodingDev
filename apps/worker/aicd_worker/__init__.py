"""Фоновые воркеры AiCodingDev (ADR-0001/0002): outbox-диспетчер, orchestrator-консьюмер, исполнитель задач.

Точка входа — `aicd-worker outbox|orchestrator|exec`. Бизнес-логики нет: маршрутизация событий в
доменные сервисы (aicd_orchestration) и транспорт Dramatiq.
"""
