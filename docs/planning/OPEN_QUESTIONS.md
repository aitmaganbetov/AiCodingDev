# OPEN_QUESTIONS — противоречия, риски и вопросы на утверждение

**Статус:** ожидает решений владельца продукта. Для каждого вопроса предложен вариант по умолчанию (**Рекомендация**), который используется в документах Phase 0, пока не принято иное решение.

## 1. Вопросы, требующие утверждения

| ID | Вопрос / противоречие | Варианты | Рекомендация | Блокирует |
|---|---|---|---|---|
| OQ-01 | **Условия использования подписок для автоматизированного (неинтерактивного, многосессионного) запуска Codex CLI и Claude Code CLI.** ТЗ требует «предусмотренный разработчиками способ», но допустимость оркестрации нескольких параллельных сессий по одной подписке определяется условиями провайдеров, которые могут меняться. | (a) Владелец подтверждает юридически, что его тарифы допускают такое использование; (b) ограничить по умолчанию 1 сессию на провайдера до подтверждения | (a)+(b): по умолчанию `max_concurrent_sessions=1` на установку; увеличение — admin-настройкой с предупреждением | P2 (реальные CLI), P4 |
| OQ-02 | **Расхождение структуры каталогов.** Мастер-промпт: `docs/architecture/adr/`, `docs/api/openapi.yaml`; ТЗ §17/§21: `docs/adr/`, `packages/contracts/openapi.yaml`, `docs/architecture/data-model.md`, `state-machines.md`, `permissions-matrix.md`. Также ТЗ §17 не содержит модулей для IAM/проектов/GitHub. | (a) Структура мастер-промпта для docs + `packages/contracts` как единый источник контрактов с Phase 1; (b) буквально ТЗ | (a), ADR-0013; доп. пакеты `platform`, `identity`, `projects`, `collaboration`, `integrations_github`, `analytics`, `apps/cli` (MODULE_BOUNDARIES §1). Файлы ТЗ §21 создаются в Phase 1 как ссылки на нормативные разделы | P1 |
| OQ-03 | **Sandbox vs учётные данные CLI.** Docker-изоляция требует доступа CLI к токенам подписки внутри контейнера; на macOS CLI может хранить их в Keychain, недоступном контейнеру. | (a) Linux-хосты исполнения, том учётных данных на провайдера, монтируемый только в runner (ADR-0010); (b) запуск CLI на хосте под отдельным OS-пользователем со встроенным sandbox CLI (local fallback); (c) обе | (c): (a) по умолчанию, (b) только явной политикой + approval `LOCAL_EXECUTION_FALLBACK`. Целевая ОС исполнения v1.0 — Linux | P2 |
| OQ-04 | **Аутентификация пользователей не определена ТЗ** (только RBAC и CSRF). | (a) Локальные аккаунты + сессии (ADR-0009); (b) только OIDC; (c) оба | (a) в v1.0, OIDC — P2; MFA TOTP и персональные токены для локального CLI — P1 | P1 |
| OQ-05 | **Порядок фаз:** мастер-промпт (Foundation → CLI → Orchestrator → Agents → CI → Quality → Mission Control → GitHub → Release) vs ТЗ §18 (Identity → Workflow → CLI → Agents → Mission Control → CI → Quality → GitHub → Hardening). | (a) Мастер-промпт; (b) ТЗ §18 | (a) — мастер-промпт новее; Identity/RBAC включается в Phase 1; Mission Control UI делается вертикальными срезами начиная с P3 (минимальный Run view + SSE), полный — P7 | Планирование |
| OQ-06 | **Таблицы, отсутствующие в каталоге ТЗ §7.2, но требуемые его же ссылками/требованиями:** `quality_profiles` (на него ссылается `quality_gates.profile_id`), `worker_hosts` (`cli_installations.host_id`), `user_credentials`/`user_sessions`, `idempotency_keys`, `worktrees`, `external_actions`, `run_event_log`, `processed_events`, `github_installations`, `github_webhook_deliveries`, `pull_requests`. | (a) Добавить; (b) хранить в jsonb/Redis | (a) — см. DATABASE_DESIGN, помечены «дополнение к ТЗ» | P1 |
| OQ-07 | **Семантика `findings.source_gate_id`**: ссылка на определение gate (`quality_gates`) или на конкретный запуск (`gate_executions`)? | (a) оставить `source_gate_id → quality_gates` и добавить `gate_execution_id → gate_executions`; (b) переименовать | (a) | P6 |
| OQ-08 | **Task-level ожидание approval** отсутствует среди состояний Task ТЗ §6. | (a) `BLOCKED` + `blocked_reason=APPROVAL_REQUIRED`; (b) новое состояние | (a) — не меняет утверждённую FSM | P3 |
| OQ-09 | **Набор языков v1.0** для tree-sitter/LSP и quality gates (ТЗ упоминает PHPStan, ESLint, Ruff — т.е. PHP, JS/TS, Python). | (a) Python, TypeScript/JavaScript, PHP, SQL, YAML/JSON/Markdown; Go/Java — P2; (b) иное | (a) | P5, P6 |
| OQ-10 | **Approval policy `never`** и самоутверждение в workspace с одним пользователем (four-eyes невозможен). | (a) Разрешить self-approval только для single-member workspace по явной политике с аудитом и пометкой в evidence; (b) запретить | (a), кроме `PRODUCTION_DEPLOY` и `SECRET_CHANGE` — для них всегда второй человек или явный риск-акцепт owner'а | P3 |
| OQ-11 | **Хостинг артефактов:** MinIO или локальная ФС по умолчанию в профиле `local`? | (a) FS adapter по умолчанию, MinIO — профиль `server`; (b) MinIO всегда | (a) — меньше компонентов; интерфейс единый (S3-совместимый адаптер) | P1 |
| OQ-12 | **Лицензия проекта GPL-3.0** (файл LICENSE) — совместимость зависимостей и распространения runner-образов с CLI (CLI имеют собственные лицензии и не должны распространяться в составе образа без проверки). | (a) Runner-образы собираются у пользователя скриптом, CLI устанавливается из официального источника при сборке; (b) публиковать образы с CLI | (a) | P2, P9 |
| OQ-13 | **Срок хранения** run_event_log/agent_messages/audit_events/raw CLI logs. | значения по умолчанию в DATABASE_DESIGN | 90 дней / 1 год / 3 года / 30 дней — настраиваемо | P3, P9 |
| OQ-14 | **Целевые SLO** не задаются до baseline (ТЗ §23). Требуется утвердить эталонное железо для baseline. | — | 8 vCPU / 32 GB RAM / SSD, 4 параллельных run | P9 |
| OQ-15 | **Coverage ≥ 85% «core»** — состав core-модулей. | (a) `orchestration`, `collaboration`, `cli_adapters`, `verification`, `security`, `identity`; (b) все | (a); исключения фиксируются в `coverage-exceptions.yaml` с обоснованием | P6, P9 |
| OQ-16 | **Объём «12 агентов» в одном run:** все роли не обязательно участвуют в каждом run. | Пайплайн по режиму (AGENT_ROLES.md) | Роли включаются шаблоном режима + уровнем L | P4 |
| OQ-17 | **Дата ТЗ 2026-11-08** позже текущей даты репозитория (2026-11-07) — информационно, не влияет. | — | — | — |

## 2. Реестр технических рисков

| ID | Риск | Вероятность | Влияние | Митигирующие меры | Владелец |
|---|---|---|---|---|---|
| RISK-01 | Изменение условий подписок/лимитов провайдеров делает автоматизацию недопустимой или нестабильной | Средняя | Критическое | OQ-01, лимит сессий, `RATE_LIMITED` cooldown, BLOCKED без обхода, мониторинг `provider_health` | Product owner |
| RISK-02 | Несовместимые изменения CLI (флаги, формат вывода) | Высокая | Высокое | Runtime probe, compat matrix, `UNSUPPORTED_VERSION`, contract-тесты на фикстурах, opt-in real-CLI suite в nightly | Tech lead |
| RISK-03 | Конфликт Docker-sandbox с авторизацией/встроенным sandbox CLI | Средняя | Высокое | ADR-0010, spike P2-01 в начале Phase 2, fallback по политике | DevSecOps |
| RISK-04 | Отсутствие стабильного машиночитаемого вывода у CLI → хрупкий парсинг ACP/claims | Средняя | Высокое | Агент пишет результат в файл `.aicd/result.json` в worktree (контракт инструкции), парсинг вывода — вторичен; `OUTPUT_PARSE_ERROR` ретраится 1 раз | Tech lead |
| RISK-05 | Сложность собственного durable engine (гонки, split-brain) | Средняя | Высокое | ADR-0007, fencing tokens, property-based тесты FSM, chaos suite с P3 | Backend lead |
| RISK-06 | Prompt injection через код/документацию/чат | Высокая | Высокое | Политика вне модели, permissions на уровне sandbox, fencing недоверенных данных, ACP-валидация | Security |
| RISK-07 | Утечка секретов через вывод CLI/артефакты/context packs | Средняя | Критическое | Redaction pipeline, gitleaks по артефактам, исключение `.env`/секретных путей из context packs | Security |
| RISK-08 | Производительность индексации больших монорепозиториев | Средняя | Среднее | Инкрементальность по sha256, лимиты, фоновые jobs, `.aicdignore` | CI lead |
| RISK-09 | Ложные PASS (gate не запустился, инструмент отсутствует) | Средняя | Критическое | SKIPPED≠PASS, обязательный gate без инструмента → FAIL/MANUAL_REVIEW, false-pass тесты (P6) | QA lead |
| RISK-10 | Расход лимитов подписок на повторы и review-циклы | Высокая | Среднее | retry budget, max review loops, L1 детерминированные инструменты, кэш context packs | Product |
| RISK-11 | Объём работ (≈ 20 недель при достаточной команде, ТЗ §18) | Высокая | Высокое | Вертикальные срезы, MVP-срез к концу P4 (AC-01…AC-05 на fake + real CLI) | PM |
| RISK-12 | Docker socket = эскалация до root хоста | Средняя | Критическое | docker-socket-proxy с минимальным API, rootless Docker/gVisor рекомендуются, exec-worker в отдельной сети | DevSecOps |
| RISK-13 | Лицензии стандартов (ISO/IEEE тексты платные) | Высокая | Среднее | Только собственные формулировки контролей и ссылки на пункты; полный текст — только загруженный пользователем с лицензией | Compliance |

## 3. Журнал решений
| Дата | Вопрос | Решение | Кем |
|---|---|---|---|
| — | — | (ожидает утверждения) | — |
