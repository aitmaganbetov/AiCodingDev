"""CLI Gateway: адаптеры официальных Codex CLI и Claude Code CLI (ТЗ §9, ADR-0003).

Публичный интерфейс: `executor.CLIExecutor`, `gateway.ExecutionGateway`, `registry`.
Реальные адаптеры Codex/Claude (P2-05/P2-06) подключаются после подтверждения
команд probe на реальных установках (P2-01); до этого доступен только Fake-адаптер.
"""

OWNED_TABLES = frozenset(
    {
        "worker_hosts",
        "cli_providers",
        "cli_installations",
        "cli_sessions",
        "cli_executions",
        "provider_health",
        "usage_metrics",
    }
)
