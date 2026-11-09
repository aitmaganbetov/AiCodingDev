# Release checklist AiCodingDev v1.0 (P9-09, черновик)

**Статус: ЧЕРНОВИК — требует утверждения владельцем.** Каждый пункт ТЗ §23 и release gate (SECURITY_CONTROLS RG-01…RG-15)
связан с доказательством. Отметка ставится только по сохранённому отчёту на **release commit** (не по заявлению);
отчёты с «рабочее дерево изменено» — промежуточные и повторяются на зафиксированном коммите.

Состояние на 2026-11-08 (Phase 9, ветка разработки; исполнитель агентов — Fake CLI):

## 1. ТЗ §23 — нефункциональные требования и критерии выпуска

| # | Критерий ТЗ §23 | Доказательство | Состояние |
|---|---|---|---|
| 1 | **Корректность:** 100% обязательных acceptance tests проходят; незавершённые/непроверенные задачи не получают `COMPLETED` | [reports/acceptance/ACCEPTANCE_REPORT.md](../../reports/acceptance/ACCEPTANCE_REPORT.md) (P9-01); false-pass тесты `tests/integration/test_false_pass_prevention.py`, `tests/e2e/test_completion_guard.py` | ☐ не выполнен: 15/16 AC PASS на Fake CLI, 1 FAIL; real-CLI части BLOCKED (P2-01) |
| 2 | **Безопасность:** нет открытых критических находок в release gate; все опасные действия gated | [docs/security/THREAT_MODEL.md](../security/THREAT_MODEL.md) (P9-07), `reports/security/` (P9-02), RG-01…RG-15 ниже | ☐ в работе (P9-02, P9-07) |
| 3 | **Надёжность:** восстановление после запланированных fault-injection без потери подтверждённых действий | chaos suite (P9-03, `tests/chaos`, backlog: 20 passed); DR drill [reports/dr/DR_DRILL.md](../../reports/dr/DR_DRILL.md) (P9-05) | ☐ IN_REVIEW (P9-03); DR drill PASS — повторить на release candidate |
| 4 | **Изоляция:** cross-tenant тесты отрицательны; секреты не попадают в логи/артефакты | `tests/security/test_cross_tenant_api.py`, `tests/security/test_rls.py`; DR drill — RLS после восстановления; redaction-тесты | ☐ подтвердить на release commit (P9-02) |
| 5 | **Аудит:** каждое поручение, решение, approval, запуск CLI и gate имеет traceable event | `tests/integration/test_api_*.py` (аудит), `audit_events` append-only (миграции, RLS-тесты); acceptance AC-14 | ☐ подтвердить в отчёте P9-01 |
| 6 | **Поддерживаемость:** backward-compatible contracts, ADR, versioned migrations, operator runbooks | `tests/contract` (OpenAPI, oasdiff P1-18), [ADR](../architecture/adr/README.md), `infrastructure/migrations` (`tests/migrations`), [runbooks](../runbooks/README.md) (P9-06) | ☐ runbooks IN_REVIEW — ревью оператором (табличное упражнение RG-14) |
| 7 | **Производительность:** SLO устанавливаются после базового нагрузочного тестирования; без неподтверждённых цифр | [reports/perf/PERF_BASELINE.md](../../reports/perf/PERF_BASELINE.md) (P9-04) | ☐ IN_REVIEW: повторный замер на выделенном стенде, утверждение SLO владельцем |
| 8 | **Документация:** локальный индекс работает независимо от Documentation Cache | AC-10 в acceptance-отчёте; `tests/e2e/test_phase5_acceptance.py` | ☐ подтвердить в отчёте P9-01 |
| — | Покрытие core ≥ 85% и 100% обязательных тестов (FR-QG-06) | [reports/coverage/COVERAGE_REPORT.md](../../reports/coverage/COVERAGE_REPORT.md) (P9-10) | ☐ core 93.08% ≥ 85%, но 3 теста FAIL в полном прогоне — повторить на release commit |

## 2. Release gate (SECURITY_CONTROLS §… RG)

| RG | Критерий (кратко) | Доказательство | Состояние |
|---|---|---|---|
| RG-01 | нет открытых findings critical; high — только ACCEPTED_RISK с подписью owner | отчёт findings на release commit (P9-07) | ☐ |
| RG-02 | контроли P0 реализованы, тесты ST-* PASS; нереализованные P1 утверждены | матрица контроль→тест (P9-02/P9-07) | ☐ |
| RG-03 | security-тесты ТЗ §20 — 100% PASS | `reports/security/junit.xml` (P9-02) | ☐ |
| RG-04 | cross-tenant по всем операциям OpenAPI → 404 | `tests/security/test_cross_tenant_api.py` | ☐ |
| RG-05 | нет секретов в логах/SSE/артефактах/context packs/**бэкапах**; gitleaks 0 | redaction-тесты; бэкапы шифруются, учётные данные CLI исключены (`scripts/backup/secrets_bundle.py`, DR drill) | ☐ (бэкапы — ✔ DR drill) |
| RG-06 | SCA/Trivy без critical/high с исправлением | SCA gates + офлайн-БД ([vulndb-update.md](../runbooks/vulndb-update.md)); `pip-audit`, `pnpm audit` в CI | ☐ прогнать на release commit |
| RG-07 | SAST без неразобранных high+ | Semgrep gate / CI | ☐ |
| RG-08 | SBOM для всех образов; образы подписаны; подписи и provenance проверяются | [reports/release/SUPPLY_CHAIN_VERIFY.md](../../reports/release/SUPPLY_CHAIN_VERIFY.md) (P9-08): SBOM CycloneDX (исходники + образы), cosign-подписи, SLSA provenance, проверка и tamper-тесты PASS | ◐ механизм ✔; release candidate собран с `--no-git-in-backend` (окружение без deb.debian.org) — `releasable=false`; повторить на машине сборки с доступом к apt и чистым деревом |
| RG-09 | fault-injection/recovery без повторных опасных действий | chaos suite (P9-03) | ☐ IN_REVIEW |
| RG-10 | все опасные действия gated (AC-13), four-eyes, TOCTOU | approvals-тесты (P9-02) | ☐ |
| RG-11 | threat model пересмотрен, residual high/critical решены владельцем | THREAT_MODEL.md (P9-07) | ☐ |
| RG-11 (TH-46) | условие принятия TH-46 (OQ-48 (b)): TLS ≥ 1.2, сертификат (доверие, имя, ≥ 14 дней), HSTS ≥ 1 год + includeSubDomains, HTTP → HTTPS, заголовки безопасности nginx, `/metrics` не публичен — на целевом edge | `uv run python -m scripts.release.preflight_tls --url https://<хост> --json` ([release.md §5](../runbooks/release.md)); тесты `tests/unit/test_preflight_tls.py` | ◐ скрипт и тесты ✔; прогон на целевом развёртывании — при развёртывании (production — по отдельному разрешению) |
| RG-12 | sandbox на целевом профиле проверен | ST-SBX/ST-NET (P9-02); `aicd doctor` — не реализован | ☐ |
| RG-13 | бэкап и восстановление проверены на release candidate; бэкапы зашифрованы, без учётных данных CLI | [reports/dr/DR_DRILL.md](../../reports/dr/DR_DRILL.md) (P9-05) | ◐ PASS на ветке разработки; повторить на release candidate |
| RG-14 | runbooks существуют и проверены табличным упражнением | [docs/runbooks/](../runbooks/README.md) (P9-06) | ◐ написаны и команды проверены; табличное упражнение — оператор |
| RG-15 | нет заявлений о сертификации | review документации | ☐ |

## 3. Процедура выпуска

1. Зафиксировать release commit (чистое дерево, тег-кандидат); все отчёты §1–§2 пересобрать на нём.
2. `uv run python -m scripts.release.pipeline --version <v> --out <каталог> --key-dir <офлайн-ключи> --password-file <…>`
   на машине сборки с доступом к apt (без `--no-git-in-backend`); `release-manifest.json` → `releasable: true`.
3. `uv run python -m scripts.release.verify --release <каталог> --public-key <доверенный cosign.pub> --expect-commit <sha> --tamper-test`.
4. `uv run python -m scripts.dr.drill` на release candidate (RG-13).
4a. На целевом развёртывании (`compose.yaml` + `compose.sandbox.yaml`, сервисы `worker-*` healthy):
   `uv run python -m scripts.release.preflight_tls --url https://<хост>` → все `PASS` (RG-11/TH-46), отчёт `--json` — в каталог релиза.
5. Утверждение владельцем (подпись в журнале решений OPEN_QUESTIONS §3) → публикация `cosign.pub` и каталога релиза.
   Production-развёртывание — только с отдельного разрешения владельца ([installation.md](../runbooks/installation.md)).

## 4. Известные блокеры (на 2026-11-08)

* Реальные Codex/Claude CLI не подключены (P2-01, P2-05, P4-13): acceptance real-CLI BLOCKED.
* ~~В `compose.yaml` нет сервисов воркеров~~ — **выполнено (OQ-46 (3), OQ-51 (d))**: `worker-outbox`, `worker-orchestrator`,
  `worker-exec`, `worker-index` (проверено на одноразовой копии стека — [installation.md §3](../runbooks/installation.md)).
  До P2-01 worker-exec в production не стартует (Fake CLI запрещён) — это ожидаемо и закрывается вместе с реальными CLI.
* ~~Gate SAST (semgrep) в контейнере без сети: probe > 60 с~~ — **исправлено**: объявленные gate переменные офлайн-режима
  передаются в контейнер (`tests/integration/test_verification_engine.py::test_semgrep_sast_in_container_offline_gets_declared_env`).
* ~~Установки CLI с версией вне матрицы (`untested`) исключались Router всегда~~ — **исправлено**: `allow_untested_cli_versions`
  в snapshot run (basic/strict — да с предупреждением `UNTESTED_CLI_VERSION`, critical — нет).
