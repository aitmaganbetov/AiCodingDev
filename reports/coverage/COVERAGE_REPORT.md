# Coverage report — core ≥ 85% и 100% обязательных тестов (P9-10)

> Сгенерировано `scripts/coverage_report.py` из `coverage.json` и junit полного прогона — не редактировать вручную.

- **Дата (UTC):** 2026-11-08T17:48:51.189597Z; **commit:** `51ab125e70d8adcec4f2a12668be9f5e90bdaae9`; рабочее дерево изменено (113 путей)
- **Команда:** `uv run pytest -p no:cacheprovider -q -rs --cov --cov-report=json:reports/coverage/coverage.json --cov-report=term -o junit_family=xunit1 --junitxml=reports/coverage/junit-full.xml` → exit 1, 908.1 с (начало 2026-11-08T17:33:42.997205Z)
- **Инструмент:** coverage.py 7.17.2 (branch=true); метрика порога — line coverage
- **Core (OQ-15 (a), `coverage-exceptions.yaml`):** `packages/orchestration`, `packages/collaboration`, `packages/cli_adapters`, `packages/verification`, `packages/security`, `packages/identity`
- **Тесты:** 2834 passed, 3 failed, 4 skipped из 2841 (junit sha256 `e5430210f989f9a1…`)
- **Итог core:** 93.08% line (с исключениями), 93.08% (без исключений); порог 85.0% — **выполнен для каждого core-пакета**

## Core-пакеты

| Пакет | Файлов | Line, % (с искл.) | Line, % (без искл.) | Branch, % | Statements (покрыто) | ≥ порога |
|---|---|---|---|---|---|---|
| `packages/orchestration` | 34 | 91.78 | 91.78 | 81.54 | 4723 (4335) | да |
| `packages/collaboration` | 15 | 95.08 | 95.08 | 87.5 | 1200 (1141) | да |
| `packages/cli_adapters` | 12 | 94.53 | 94.53 | 83.74 | 933 (882) | да |
| `packages/verification` | 19 | 94.87 | 94.87 | 89.44 | 3041 (2885) | да |
| `packages/security` | 8 | 89.99 | 89.99 | 84.05 | 1069 (962) | да |
| `packages/identity` | 6 | 93.65 | 93.65 | 81.25 | 315 (295) | да |
| **core всего** |  | **93.08** | 93.08 | 84.84 | 11281 (10500) | да |

## Остальные пакеты (не core, справочно)

| Пакет | Line, % | Branch, % | Statements |
|---|---|---|---|
| `apps/api` | 94.67 | 85.44 | 3172 |
| `apps/cli` | 43.26 | 13.64 | 141 |
| `apps/worker` | 73.1 | 54.13 | 907 |
| `packages/analytics` | 99.19 | 83.33 | 123 |
| `packages/code_intelligence` | 92.11 | 83.5 | 3585 |
| `packages/contracts` | 100.0 | 100.0 | 214 |
| `packages/integrations_github` | 85.66 | 68.52 | 572 |
| `packages/platform` | 92.19 | 79.15 | 1638 |
| `packages/projects` | 84.94 | 70.49 | 405 |
| `packages/standards` | 99.1 | 94.55 | 445 |
| **всё** | **91.85** | 82.61 | 22483 |

## Исключения (coverage-exceptions.yaml)

| Файл | Статус | Обоснование |
|---|---|---|
| `packages/cli_adapters/aicd_cli_adapters/fake/fake_cli.py` | approved | Исполняемый файл Fake CLI — запускается дочерним процессом адаптера; сценарии проверяются тестами адаптера через настоящий процесс (tests/integration/test_fake_adapter.py, test_gateway.py, e2e). |

## Наименее покрытые файлы core (по числу непокрытых строк)

| Файл | Непокрыто строк | Line, % |
|---|---|---|
| `packages/orchestration/aicd_orchestration/directives.py` | 70 | 86.3 |
| `packages/orchestration/aicd_orchestration/github_flow.py` | 51 | 81.7 |
| `packages/orchestration/aicd_orchestration/engine.py` | 47 | 86.6 |
| `packages/verification/aicd_verification/engine.py` | 41 | 87.4 |
| `packages/security/aicd_security/ssrf.py` | 38 | 87.0 |
| `packages/security/aicd_security/docker_runner.py` | 33 | 83.3 |
| `packages/orchestration/aicd_orchestration/github_verification.py` | 32 | 70.4 |
| `packages/orchestration/aicd_orchestration/recovery.py` | 31 | 80.7 |
| `packages/cli_adapters/aicd_cli_adapters/subprocess_adapter.py` | 28 | 90.9 |
| `packages/verification/aicd_verification/gates.py` | 27 | 90.6 |
| `packages/verification/aicd_verification/tools/parsers.py` | 24 | 89.7 |
| `packages/orchestration/aicd_orchestration/agent_io.py` | 21 | 89.6 |
| `packages/verification/aicd_verification/security_gates.py` | 20 | 95.4 |
| `packages/orchestration/aicd_orchestration/github_recovery.py` | 19 | 82.1 |
| `packages/orchestration/aicd_orchestration/worktrees.py` | 19 | 84.1 |

## Обязательные тесты

**FAILED:**

- `tests/acceptance/test_ac15_contracts.py::test_ac15_oasdiff_no_breaking_changes_against_baseline`
- `tests/contract/test_architecture.py::test_every_table_has_owner`
- `tests/security/test_secret_leakage.py::test_cli_output_secrets_absent_from_db_and_artifacts`

**SKIPPED (не засчитываются как PASS):**

- `tests/integration/test_gate_runners_tools.py::test_phpstan_fixtures[pass-PASS]` — PHPStan недоступен: composer не скачал phpstan/phpstan (github.com закрыт egress), образа нет
- `tests/integration/test_gate_runners_tools.py::test_phpstan_fixtures[fail-FAIL]` — PHPStan недоступен: composer не скачал phpstan/phpstan (github.com закрыт egress), образа нет
- `tests/integration/test_github_real.py::test_real_github_app_read_only_flow` — реальный GitHub: задайте AICD_TEST_GITHUB_APP_ID, AICD_TEST_GITHUB_APP_KEY_FILE, AICD_TEST_GITHUB_INSTALLATION_ID, AICD_TEST_GITHUB_REPO (opt-in, P8-02)
- `tests/security/test_security_gates_docker.py::test_trivy_vuln_with_offline_db` — AICD_TRIVY_DB_DIR (офлайн-БД trivy) не задан
