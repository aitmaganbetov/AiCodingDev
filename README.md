# AiCodingDev

Self-hosted платформа управляемой AI-разработки и мультиагентной оркестрации: 12 логических ролей, исполняемых **только** официальными Codex CLI и Claude Code CLI по подпискам, с независимой верификацией, evidence и human approvals.

- Требования: [`AiCodingDev_v1_TZ.md`](AiCodingDev_v1_TZ.md)
- Архитектура и план: [`docs/`](docs/README.md); ход работ и доказательства — [`docs/planning/IMPLEMENTATION_BACKLOG.md`](docs/planning/IMPLEMENTATION_BACKLOG.md)
- Установка: [`docs/runbooks/installation.md`](docs/runbooks/installation.md); операторские runbooks — [`docs/runbooks/`](docs/runbooks/README.md)
- Релиз: чеклист [`docs/release/RELEASE_CHECKLIST.md`](docs/release/RELEASE_CHECKLIST.md), сборка/подпись/проверка — [`docs/runbooks/release.md`](docs/runbooks/release.md)

## Состояние

Phases 0–8 реализованы и проверены (см. журнал выполнения в backlog): идентичность и RBAC с RLS, durable workflow engine
(outbox, аренды с fencing token, recovery), CLI Gateway (пока Fake CLI — реальные Codex/Claude адаптеры ждут машины с
авторизованными CLI, P2-01), 12 ролей агентов и ACP, Mission Control и Live Agent Chat, Code Intelligence, quality gates и
Standards Engine, GitHub App, наблюдаемость и Analytics. Phase 9 (hardening/release) — в работе: бэкап/восстановление с DR
drill ([`reports/dr/DR_DRILL.md`](reports/dr/DR_DRILL.md)), runbooks, supply chain релиза (SBOM, cosign, SLSA provenance —
[`reports/release/SUPPLY_CHAIN_VERIFY.md`](reports/release/SUPPLY_CHAIN_VERIFY.md)). Выпуск v1.0 — после release checklist и
утверждения владельцем; production-развёртывание — только с отдельного разрешения.

## Структура

```text
apps/api        FastAPI (REST /api/v1)          apps/web     React + TS + Vite + Tailwind
apps/cli        админ-CLI `aicd`                apps/worker  aicd-worker outbox|orchestrator|exec|index
packages/       platform, contracts, identity, projects, security, cli_adapters, orchestration,
                verification, standards, collaboration, code_intelligence, analytics, integrations_github
infrastructure/ migrations (Alembic), postgres bootstrap, docker (compose, Dockerfiles), monitoring
scripts/        backup (бэкап/восстановление), dr (DR drill), release (SBOM/подпись/provenance), vulndb
tests/          unit, contract, migrations, integration, security, chaos, e2e
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

Эксплуатация (нужен Docker): `uv run python -m scripts.dr.drill` — DR drill на одноразовых контейнерах;
`uv run python -m scripts.backup --help` — бэкап/восстановление; `uv run python -m scripts.release.pipeline --help` и
`uv run python -m scripts.release.verify --help` — релиз.

## Лицензия

GPL-3.0 — см. [LICENSE](LICENSE).
