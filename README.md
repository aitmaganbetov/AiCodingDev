# AiCodingDev

Self-hosted платформа управляемой AI-разработки и мультиагентной оркестрации: 12 логических ролей, исполняемых **только** официальными Codex CLI и Claude Code CLI по подпискам, с независимой верификацией, evidence и human approvals.

- Требования: [`AiCodingDev_v1_TZ.md`](AiCodingDev_v1_TZ.md)
- Архитектура и план: [`docs/`](docs/README.md); ход работ и доказательства — [`docs/planning/IMPLEMENTATION_BACKLOG.md`](docs/planning/IMPLEMENTATION_BACKLOG.md)
- Развёртывание: [`docs/runbooks/local-deployment.md`](docs/runbooks/local-deployment.md)

## Состояние

Phase 0 (архитектура) утверждена; Phase 1 (Project Foundation) реализована: каркас монорепозитория, БД с RLS, аутентификация и RBAC, API workspaces/projects/repositories/audit, веб-оболочка, CI. Оркестрация, CLI-адаптеры и агенты — Phase 2+.

## Структура

```text
apps/api        FastAPI (REST /api/v1)          apps/web   React + TS + Vite + Tailwind
apps/cli        админ-CLI `aicd`                packages/  platform, contracts, identity, projects, security
infrastructure/ migrations (Alembic), postgres bootstrap, docker (compose, Dockerfiles)
tests/          unit, contract, migrations, integration, security
```

## Разработка

```sh
uv sync                                   # Python 3.12+
uv run ruff check . && uv run mypy && uv run lint-imports
uv run python scripts/check_no_ai_sdk.py  # запрет SDK AI-провайдеров (ТЗ §1)
uv run python scripts/traceability.py     # требования → задачи

# тесты с БД: суперпользователь PostgreSQL 17 + Redis
export AICD_TEST_DATABASE_ADMIN_URL=postgresql://admin:pass@127.0.0.1:5432/postgres
export AICD_TEST_REDIS_URL=redis://127.0.0.1:6379/15
uv run pytest --cov

cd apps/web && pnpm install && pnpm check:api && pnpm typecheck && pnpm lint && pnpm test && pnpm e2e
```

## Лицензия

GPL-3.0 — см. [LICENSE](LICENSE).
