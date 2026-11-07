# ACCEPTANCE_CRITERIA — критерии приёмки AiCodingDev v1.0

**Статус:** Proposed. Источник — ТЗ §19 (16 сценариев), §14, §23. Формат: Given / When / Then + проверка + фаза, в которой критерий впервые становится проверяемым. Каждый AC в P9-01 прогоняется на Fake adapters (обычный CI) и, где помечено **RC**, — в opt-in прогоне с реальными CLI.

**Общее правило доказательства:** AC считается пройденным только при наличии evidence: ID CI-прогона/команды, commit SHA, exit code, время, артефакты отчёта с sha256. Утверждение агента или разработчика без evidence не принимается.

| AC | Сценарий (ТЗ §19) | Given / When / Then | Тест (тип, ID) | Фаза |
|---|---|---|---|---|
| AC-01 | Подключение локального репозитория, создание проекта, запуск DEVELOP | **Given** пользователь `developer` в workspace и локальный Git-репозиторий в allowlist; **When** создаёт проект, привязывает репозиторий и `POST /runs` с `mode=DEVELOP`; **Then** 201, `status=CREATED`, run переходит в `DISCOVERY`, событие `run.state_changed` получено по SSE | E2E `e2e/test_ac01_*`; **RC** | P1 (проект/репо), P3 (run) |
| AC-02 | Определение стека, индексация по commit, DAG без циклов | **Given** run в `DISCOVERY`; **When** индексация завершена; **Then** `code_index_snapshots` для `(repository, base_commit_sha)` = `READY`, `project_technologies` заполнены, план проходит валидацию ацикличности; план с циклом отклоняется `PLAN_CYCLE_DETECTED` | integration + unit DAG | P3, P5 |
| AC-03 | Router назначает доступный официальный CLI без AI API | **Given** установлены codex и claude, хотя бы один `AUTHENTICATED`; **When** задача READY; **Then** `routing_decisions` содержит выбранный провайдер ∈ {codex, claude} с `score_breakdown`; сетевой трафик платформы к AI API отсутствует (egress-лог: только runner → эндпоинты провайдера); в зависимостях нет AI SDK (CI deny-gate) | integration + CI gate; **RC** | P4 |
| AC-04 | Codex реализует в отдельном worktree; Claude — независимое ревью | **Given** policy L3+ требует независимого ревью, оба провайдера доступны; **When** developer-задача исполнена Codex; **Then** изменения только в worktree задачи, reviewer-задача назначена Claude, evidence ревью помечено `independent=true`; если Claude недоступен — применяется policy fallback и `independent=false` | integration; **RC** | P4 |
| AC-05 | Межагентные сообщения видны в Live Agent Chat без скрытого CoT | **Given** run с handoff; **When** пользователь открывает поток `All Agents`/`Role`/`Task`; **Then** все `agent_messages` run отображаются с типом, отправителем, временем, статусом; в данных нет полей скрытых рассуждений — только ACP payload, нормализованные события CLI и артефакты | E2E Playwright | P4 (API), P7 (UI) |
| AC-06 | CHANGE_REQUEST во время выполнения | **Given** run `RUNNING` с задачей `RUNNING`; **When** `POST /runs/{id}/directives` (`CHANGE_REQUEST`, верный `expected_run_version`); **Then** 202 → `IMPACT_ANALYSIS_PENDING` → `directive.impact_ready` → (подтверждение, если критично) → checkpoint создан → новая `plan_version` → `DISPATCHED` → `ACKNOWLEDGED`; работающий CLI-процесс не изменён незаметно; неверная версия → 412 | integration + E2E | P3, P7 |
| AC-07 | Ошибка/таймаут CLI | **Given** Fake возвращает `TIMEOUT`; **When** задача исполняется; **Then** `task_attempts.failure_category=TIMEOUT`, задача `RETRY_WAIT` до исчерпания `max_attempts`, затем `FAILED`; run не `COMPLETED`; число попыток ≤ бюджета | integration | P3 |
| AC-08 | Разрыв соединения UI без дублей | **Given** клиент получил события до `seq=N`; **When** соединение разорвано и восстановлено с `Last-Event-ID: N`; **Then** получены все события `seq > N` ровно один раз, в порядке `seq` | integration SSE + Playwright | P3, P7 |
| AC-09 | Авария worker без повторного merge/опасной миграции | **Given** `external_actions` типа `MERGE` в `CONFIRMED` или `EXECUTING`; **When** worker убит и workflow восстановлен; **Then** `CONFIRMED` не повторён; `EXECUTING` сверен с внешней системой перед любым повтором; незавершённые задачи продолжены | chaos | P3, P8 |
| AC-10 | Docs OFF и SMART | **Given** `documentation_mode=OFF`; **When** run выполняется; **Then** нет обращений к внешним источникам (egress-лог пуст для docs), контекст из локального индекса. **Given** `SMART` и источник не в кэше; **Then** создан approval `DOCS_DOWNLOAD`, загрузка не начинается до `APPROVED` | integration | P5 |
| AC-11 | Ошибочные тесты блокируют COMPLETED; exit 0 ≠ verified | **Given** CLI завершился с `exit_code=0`, а обязательный gate `FAIL`; **Then** задача не `SUCCEEDED`, run не `COMPLETED`; evidence claims агента `UNVERIFIED`; `SKIPPED`/`ERROR` обязательного gate не дают PASS | integration + false-pass suite | P3 (v0), P6 |
| AC-12 | Пользователь без прав | **Given** пользователь не член workspace B / член с ролью `viewer`; **When** читает ресурсы B / отправляет директиву / утверждает критическое действие; **Then** 404 для чужого workspace (без утечки существования), 403 для недостаточной роли; попытки аудируются; SSE-поток чужого run недоступен | security suite | P1, P3, P7 |
| AC-13 | Production deploy / merge / destructive migration без approval невозможны | **Given** действие из `approval_action_type` критического класса; **When** агент/пользователь инициирует его без `APPROVED`; **Then** действие не выполняется, `external_actions` не переходит в `EXECUTING`; requester не может утвердить сам (four-eyes, кроме политики OQ-10) | integration + permission tests | P3, P8 |
| AC-14 | Состав evidence | **Given** завершённый gate; **Then** evidence содержит `commit_sha`, `command`, `exit_code`, `started_at`, `finished_at`, `environment`, artifact с `sha256`, и sha256 совпадает с содержимым хранилища | integration | P3 (v0), P6 |
| AC-15 | Контракты: schema validation, backward compatibility, negative tests | **When** CI; **Then** OpenAPI валидна, `oasdiff breaking` против предыдущего релиза пуст (или major bump), schemathesis без 5xx, ACP/CLI/SSE JSON Schema — позитивные и негативные фикстуры | contract suite | P1 и далее |
| AC-16 | CLI-авторизация недоступна | **Given** оба провайдера `AUTH_REQUIRED` (или один, а другой не подходит по policy); **When** задача READY; **Then** задача `BLOCKED(AUTH_REQUIRED / PROVIDER_UNAVAILABLE)`, причина видна в UI/API, нет попыток обходной авторизации, нет обращений к AI API | integration; **RC** | P2, P4 |

## Нефункциональные критерии выпуска (ТЗ §23)

| Критерий | Метод проверки |
|---|---|
| 100% обязательных acceptance tests проходят | P9-01, отчёт CI |
| Нет открытых critical findings в release gate | P9-07, отчёт сканеров + ревью |
| Восстановление после fault-injection без потери подтверждённых действий | P9-03 |
| Cross-tenant тесты отрицательны; секреты не в логах/артефактах | P9-02 (+ gitleaks по артефактам и логам тестовых прогонов) |
| Каждое поручение/решение/approval/запуск CLI/gate имеет traceable event | Тест покрытия аудита: для каждого типа действия — запись `audit_events` |
| Backward-compatible contracts, ADR, versioned migrations, runbooks | P1-03, P9-06 |
| SLO — только после baseline, без неподтверждённых цифр | P9-04 |
| Локальный индекс работает без Documentation Cache | AC-10 |
| Coverage core ≥ 85% (с утверждёнными исключениями) | P9-10 |

## Критерии завершения фаз
См. [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md) — для каждой фазы раздел «Критерии завершения».
