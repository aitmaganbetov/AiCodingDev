# REQUIREMENTS_REGISTRY — реестр требований AiCodingDev v1.0

**Статус:** Proposed (Phase 0). Источник — ТЗ (все разделы §1–§23 изучены). Каждое требование трассируется: `ТЗ § → FR/NFR → задачи backlog → AC/тест`.
Приоритет: **P0** — обязательно для v1.0; **P1** — обязательно для v1.0, допускается в поздних фазах; **P2** — опционально / после v1.0.
Колонка «Фаза» — фаза реализации по [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md).

## 1. Функциональные требования

### IAM — идентичность, RBAC, тенантность
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-IAM-01 | Пользователи, workspaces, членство с ролями `owner/admin/developer/reviewer/viewer` | §7.2, §15 | P0 | P1 | AC-12 |
| FR-IAM-02 | Аутентификация веб-интерфейса, сессии, CSRF для cookie-сессий (способ входа — ADR-0009, OQ-04) | §8, §15 | P0 | P1 | AC-12 |
| FR-IAM-03 | Workspace/project-scoped проверка прав на каждое действие (матрица прав) | §15, §21 | P0 | P1 | AC-12 |
| FR-IAM-04 | Tenant isolation: workspace_id, запрет межтенантных ссылок, RLS | §7.1, §23 | P0 | P1 | AC-12 |
| FR-IAM-05 | Пользователь без прав не может читать чужой workspace, отправлять поручения, утверждать критические операции | §19 п.12 | P0 | P1, P7 | AC-12 |

### PRJ — проекты и репозитории
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-PRJ-01 | CRUD проектов, настройки проекта (`project_settings`) | §7.2, §8 | P0 | P1 | AC-01 |
| FR-PRJ-02 | Привязка локального Git-репозитория (path allowlist) | §1, §8, §19 п.1 | P0 | P1 | AC-01 |
| FR-PRJ-03 | Привязка GitHub-репозитория | §1, §8 | P0 | P8 | — |
| FR-PRJ-04 | Определение стека технологий проекта (`technologies`, `project_technologies`) | §8, §19 п.2 | P0 | P5 | AC-02 |
| FR-PRJ-05 | Repository Explorer (дерево файлов, просмотр по commit) | §11 | P0 | P7 | — |

### ORCH — durable orchestration
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-ORCH-01 | Run FSM со всеми состояниями и запретом невалидных переходов | §6 | P0 | P3 | AC-11 |
| FR-ORCH-02 | Task FSM, max_attempts, retry budget, timeout, lease heartbeat, optimistic locking | §6 | P0 | P3 | AC-07 |
| FR-ORCH-03 | Execution FSM с нормализованными исходами | §6, §9 | P0 | P2 | AC-07 |
| FR-ORCH-04 | DAG-план: валидация ацикличности транзакционно при утверждении; задачи только после зависимостей | §6, §7.3 | P0 | P3 | AC-02 |
| FR-ORCH-05 | 11 режимов run (`run_mode`) с шаблонами пайплайнов | §5 | P0 | P4 | AC-01 |
| FR-ORCH-06 | Pause / resume / cancel run | §8, §11 | P0 | P3 | AC-06 |
| FR-ORCH-07 | Checkpoint и recovery (сверка БД, leases, PID/process group, worktree, артефактов) | §6, §16 | P0 | P3 | AC-09 |
| FR-ORCH-08 | Transactional outbox, at-least-once, идемпотентные consumers | §10, §16 | P0 | P3 | AC-08, AC-09 |
| FR-ORCH-09 | Leases с fencing token | §16 | P0 | P3 | AC-09 |
| FR-ORCH-10 | Повтор не выполняет повторно подтверждённые внешние действия (ledger) | §6, §16 | P0 | P3 | AC-09, AC-13 |
| FR-ORCH-11 | `COMPLETED` только при успешных обязательных gates и выполнении approval policy | §6, §14 | P0 | P3, P6 | AC-11 |
| FR-ORCH-12 | Корреляционные идентификаторы во всех событиях | §6, §16 | P0 | P1, P3 | — |
| FR-ORCH-13 | Policy snapshot в run для воспроизводимости | §5 | P0 | P3 | — |

### ROUTE — Intelligent Router
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-ROUTE-01 | Уровни сложности L1–L5 с соответствующими шаблонами исполнения | §5 | P0 | P4 | AC-03 |
| FR-ROUTE-02 | Учёт сложности, риска, типа файлов, capability matrix, доступности, авторизации, истории качества, политики, числа сессий, наблюдаемых ограничений | §5 | P0 | P4 | AC-03 |
| FR-ROUTE-03 | Оба CLI недоступны → `BLOCKED` без обхода лимитов | §5, §19 п.16 | P0 | P4 | AC-16 |
| FR-ROUTE-04 | Независимое ревью другим провайдером; fallback по policy, без ложной маркировки | §4 | P0 | P4 | AC-04 |
| FR-ROUTE-05 | Сохранение `routing_decisions` с score breakdown и версией политики | §7.2 | P0 | P4 | — |

### AGENT — 12 логических ролей
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-AGENT-01 | 12 профилей ролей с проверяемыми выходами | §4 | P0 | P4 | AC-04 |
| FR-AGENT-02 | Версионированные шаблоны инструкций, allowed_tools, policy profile | §7.2 | P0 | P4 | — |
| FR-AGENT-03 | Любая роль исполнима любым из двух CLI при наличии возможностей | §4 | P0 | P4 | AC-03 |
| FR-AGENT-04 | Агенты не получают unrestricted shell/сеть/secrets/production credentials | §9 | P0 | P2 | AC-13 |

### ACP — Agent Communication Protocol
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-ACP-01 | Конверт `acp/1.0`, 12 типов, JSON Schema payload по типу, строгая валидация | §10 | P0 | P4 | AC-15 |
| FR-ACP-02 | Сообщения только через Orchestrator: schema, policy, ACL, correlation, лимиты, audit | §10 | P0 | P4 | AC-05 |
| FR-ACP-03 | ≤ 32 KiB на сообщение, крупные данные — artifact refs | §10 | P0 | P4 | — |
| FR-ACP-04 | Лимит review/fix loops (default 3) | §10 | P0 | P4 | — |
| FR-ACP-05 | Доставка: outbox + Redis at-least-once + idempotent consumer | §10 | P0 | P4 | — |
| FR-ACP-06 | Недоверенные сообщения не переопределяют system policy; no self-authorized critical actions | §10 | P0 | P4 | AC-13 |

### CLI — CLI Gateway и адаптеры
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-CLI-01 | Только официальные Codex CLI и Claude Code CLI; без AI API/keys | §1, §9 | P0 | P2 | AC-03 |
| FR-CLI-02 | `CLIExecutor` Protocol: probe, check_auth, execute, cancel, collect_result | §9.1 | P0 | P2 | — |
| FR-CLI-03 | ExecutionRequest/ExecutionResult contract v1.0 (JSON Schema) | §9.2–9.3 | P0 | P2 | AC-15 |
| FR-CLI-04 | Runtime probe: версия, команды, машинный вывод, noninteractive, авторизация; compatibility matrix | §3, §9 | P0 | P2 | — |
| FR-CLI-05 | Нормализация ошибок (9 кодов) и событий (6 типов) | §9.3 | P0 | P2 | AC-07 |
| FR-CLI-06 | Raw logs — отдельный защищённый artifact; вывод очищается от секретов и ограничивается | §8.3, §9 | P0 | P2 | — |
| FR-CLI-07 | Usage/лимиты только если предоставлены CLI, иначе `unknown` | §9.3, §16 | P0 | P2 | — |
| FR-CLI-08 | Авторизация недоступна → задача `BLOCKED`, показана причина | §19 п.16 | P0 | P2, P3 | AC-16 |
| FR-CLI-09 | Fake/mock adapters для CI; opt-in suite с реальными CLI | §20 | P0 | P2 | — |
| FR-CLI-10 | Экран CLI Providers: health, capabilities, probe, безопасные настройки | §8, §11 | P0 | P2 (API), P7 (UI) | — |

### SBX — изоляция исполнения
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-SBX-01 | Отдельный Git worktree на задачу | §2, §19 п.4 | P0 | P2 | AC-04 |
| FR-SBX-02 | Docker sandbox: FS allowlist, запись только в worktree, egress policy, CPU/RAM/time limits, kill process tree, no privileged | §3, §15 | P0 | P2 | AC-13 |
| FR-SBX-03 | Локальный fallback только по явной политике | §3 | P1 | P2 | — |

### CHAT — Mission Control и Live Agent Chat
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-CHAT-01 | Потоки `All Agents`, `Codex`, `Claude`, `Role`, `Task`, `Approvals` | §11 | P0 | P7 | AC-05 |
| FR-CHAT-02 | Показ структурированных сообщений, событий, безопасных выдержек CLI, ссылок на diff/тесты/evidence, статусов | §11 | P0 | P7 | AC-05 |
| FR-CHAT-03 | Команды `ASK`, `NEW_TASK`, `CHANGE_REQUEST`, `STOP`, `PAUSE`, `RESUME`, `REVIEW`, `APPROVE`, `REJECT` через impact analysis, RBAC, policy | §11, §8.2 | P0 | P3 (backend), P7 (UI) | AC-06 |
| FR-CHAT-04 | Directive lifecycle: impact preview → confirm → checkpoint/pause → replan → dispatch → ack | §8.2 | P0 | P3 | AC-06 |
| FR-CHAT-05 | Различать «доставлено» и «выполнено» | §11 | P0 | P7 | AC-06 |
| FR-CHAT-06 | Фильтры, поиск, timeline, упоминания ролей, diff viewer, текущий исполнитель | §11 | P0 | P7 | — |
| FR-CHAT-07 | SSE с Last-Event-ID, дедупликацией, разграничением доступа | §8.3 | P0 | P3 | AC-08 |
| FR-CHAT-08 | 13 UI-экранов (Dashboard … Settings/Audit), responsive, клавиатура, ошибки/пустые состояния | §11 | P0 | P7 | — |
| FR-CHAT-09 | Без скрытых chain-of-thought: только официальные события CLI, сообщения, артефакты | §2 п.8, §19 п.5 | P0 | P2, P7 | AC-05 |

### CI — Code Intelligence
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-CI-01 | Commit-aware snapshots, file tree | §12 | P0 | P5 | AC-02 |
| FR-CI-02 | Символы tree-sitter; LSP при доступности | §3, §12 | P0 (LSP P1) | P5 | — |
| FR-CI-03 | ripgrep + PostgreSQL FTS/pg_trgm | §3, §12 | P0 | P5 | AC-10 |
| FR-CI-04 | Граф зависимостей и test links | §12 | P0 | P5 | — |
| FR-CI-05 | Инкрементальное хэширование и reindex | §12 | P0 | P5 | — |
| FR-CI-06 | Context Packs с бюджетом и ранжированием; кэш по content hash + commit + policy | §12 | P0 | P5 | AC-10 |
| FR-CI-07 | Устаревший фрагмент блокируется или маркируется | §12 | P0 | P5 | — |
| FR-CI-08 | Метрика полезности контекста по подтверждённым результатам | §12 | P1 | P8 | — |

### DOC — Documentation (необязательный RAG)
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-DOC-01 | Режимы `OFF`/`SMART` (default)/`STRICT` | §12 | P0 | P5 | AC-10 |
| FR-DOC-02 | SMART не скачивает источник без разрешения | §12, §19 п.10 | P0 | P5 | AC-10 |
| FR-DOC-03 | STRICT: без утверждённого источника → `BLOCKED`/`MANUAL_REVIEW` | §12 | P0 | P5 | — |
| FR-DOC-04 | Импорт URL/PDF/MD/HTML/Git (опционален для запуска задач) с версиями, лицензиями | §12 | P1 | P5 | — |
| FR-DOC-05 | Защита импорта: SSRF, zip bombs, опасные файлы, prompt injection | §12, §15 | P0 (если импорт включён) | P5 | — |
| FR-DOC-06 | Централизованный кэш документации; экран Knowledge/Documentation | §11, §12 | P1 | P5, P7 | — |

### QG — Quality Gates и Evidence
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-QG-01 | Профили `basic`/`strict`/`critical` | §14 | P0 | P6 | — |
| FR-QG-02 | Gates запускаются Verification Engine, не подтверждаются словами агента | §14 | P0 | P6 | AC-11 |
| FR-QG-03 | Обязательные категории при наличии кода (lint…independent review) | §14 | P0 | P6 | AC-11 |
| FR-QG-04 | PASS только с сохранённым результатом процесса и evidence; SKIPPED ≠ PASS | §14 | P0 | P6 | AC-11 |
| FR-QG-05 | Evidence: environment, command, exit code, commit SHA, timestamps, artifacts + checksum | §14, §19 п.14 | P0 | P6 | AC-14 |
| FR-QG-06 | Coverage core ≥ 85% (с оговорёнными исключениями); 100% обязательных тестов проходят | §14 | P0 | P6, P9 | — |
| FR-QG-07 | Findings с severity/status; экран Evidence & Quality; отчёт run | §7.2, §8, §11 | P0 | P6, P7 | — |

### STD — Standards & Compliance
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-STD-01 | Registry: стандарт, редакция, контроль, метод проверки | §13 | P0 | P6 | — |
| FR-STD-02 | Применимость контролей утверждается для проекта | §13 | P0 | P6 | — |
| FR-STD-03 | Статусы `PASS/FAIL/NOT_APPLICABLE/UNVERIFIED/MANUAL_REVIEW`; трассировка до evidence | §13 | P0 | P6 | — |
| FR-STD-04 | Не выдавать формулировки за официальный текст; не заявлять сертификацию; учитывать лицензии | §13 | P0 | P6 | — |
| FR-STD-05 | Экран Standards & Compliance, `GET /runs/{id}/compliance` | §8, §11 | P0 | P6, P7 | — |

### APR — Approvals
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-APR-01 | Approval для production deploy, destructive migration, deleting data, changing secrets, unrestricted network, merge | §15 | P0 | P3 | AC-13 |
| FR-APR-02 | Approval policy run (`risk_based` и др.), L4/L5 — human approval | §5, §8.1 | P0 | P3, P4 | — |
| FR-APR-03 | Экран/поток Approvals, `GET /approvals`, `POST /approvals/{id}/decision` | §8, §11 | P0 | P3, P7 | AC-13 |

### GH — GitHub
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-GH-01 | GitHub App (опционально), привязка репозиториев | §3, §8 | P0 | P8 | — |
| FR-GH-02 | Webhooks: проверка подписи, дедупликация | §15 | P0 | P8 | — |
| FR-GH-03 | PR workflow: push ветки, PR, ревью; уважать branch protection и PR approvals; merge — только с approval | §15, §20 | P0 | P8 | AC-13 |

### OBS / AUD — наблюдаемость, аналитика, аудит
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-OBS-01 | Структурные логи с run_id/task_id/execution_id, редактирование секретов | §16 | P0 | P1 | — |
| FR-OBS-02 | Метрики ТЗ §16 (success rate, verified completion rate, …) | §16 | P0 | P1 (база), P8 | — |
| FR-OBS-03 | OpenTelemetry, Prometheus/Grafana | §3 | P1 | P1, P8 | — |
| FR-OBS-04 | Алерты: auth failures, stuck leases, error rates, storage, failed backups | §16 | P1 | P8, P9 | — |
| FR-OBS-05 | Экран Analytics | §11 | P0 | P8 | — |
| FR-AUD-01 | Append-only audit_events для каждого поручения, решения, approval, запуска CLI, gate | §15, §23 | P0 | P1 (+ каждая фаза) | — |
| FR-AUD-02 | `GET /audit-events`, экран Settings/Audit | §8, §11 | P0 | P1, P7 | — |

### API
| ID | Требование | ТЗ | Pri | Фаза | AC |
|---|---|---|---|---|---|
| FR-API-01 | REST `/api/v1`, OpenAPI 3.1, все эндпоинты ТЗ §8 | §8, §21 | P0 | P1–P8 | AC-15 |
| FR-API-02 | RFC 7807, cursor pagination, Idempotency-Key, ETag/version, rate limiting | §8 | P0 | P1 | AC-15 |
| FR-API-03 | Отчёт run (`/runs/{id}/report`) | §8 | P0 | P6 | — |
| FR-API-04 | Backward compatibility checks и negative tests контрактов | §19 п.15 | P0 | P1 | AC-15 |

## 2. Нефункциональные требования

| ID | Требование | ТЗ | Проверка |
|---|---|---|---|
| NFR-SEC-01 | Нет AI API/keys/сторонних провайдеров/неофициальной авторизации | §1 | SCA + grep-gate в CI на SDK провайдеров; ревью |
| NFR-SEC-02 | Секреты и CLI tokens не хранятся в PG и не попадают в логи/артефакты/SSE | §7.1, §15, §23 | secret-leak тесты, gitleaks по артефактам |
| NFR-SEC-03 | Пользовательские/репозиторные тексты — недоверенные; prompt injection не меняет policy/permissions | §15 | security tests (prompt injection corpus) |
| NFR-SEC-04 | Нет открытых критических находок в release gate | §23 | release checklist |
| NFR-SEC-05 | Supply chain: pinning, SCA, SBOM, integrity, secure CI | §15 | CI gates |
| NFR-SEC-06 | Следование OWASP ASVS, NIST SSDF, OpenSSF практикам (без заявления сертификации) | мастер-промпт, §13 | SECURITY_CONTROLS mapping |
| NFR-REL-01 | Восстановление после fault-injection без потери подтверждённых действий | §23 | chaos suite |
| NFR-REL-02 | Повторная доставка не выполняет повторно опасное действие | §16 | chaos/duplicate tests |
| NFR-REL-03 | Backups PostgreSQL и артефактов; тест восстановления | §16 | DR drill |
| NFR-REL-04 | Миграции с dry-run и планом rollback | §7.1, §16 | migration tests |
| NFR-REL-05 | Retention и архивирование сообщений/событий | §7.3, §15 | retention job tests |
| NFR-ISO-01 | Cross-tenant тесты отрицательны | §23 | security suite |
| NFR-AUD-01 | Каждое поручение/решение/approval/запуск CLI/gate — traceable event | §23 | audit coverage test |
| NFR-MAINT-01 | Backward-compatible contracts, ADR, versioned migrations, runbooks | §23 | CI oasdiff, review |
| NFR-MAINT-02 | Границы модулей через публичные интерфейсы | §17 | import-linter |
| NFR-PERF-01 | SLO устанавливаются после baseline; без неподтверждённых цифр | §20, §23 | perf suite baseline |
| NFR-OBS-01 | Корреляция логов/трейсов | §16 | log schema tests |
| NFR-UX-01 | Responsive, клавиатурная доступность, ошибки/пустые состояния; русский по умолчанию, готовность к i18n | шапка, §11 | Playwright + axe |
| NFR-PORT-01 | Self-hosted/local, без обязательного Kubernetes и облака | §1 | compose smoke |
| NFR-DOC-01 | Локальный индекс работает независимо от Documentation Cache | §23 | AC-10 тест |
| NFR-TEST-01 | Обычный CI не требует авторизации подписки; opt-in real-CLI suite | §20 | CI config |
| NFR-HONEST-01 | Не утверждать прохождение тестов без запуска; AI-ответ ≠ подтверждённый результат | мастер-промпт, §21 | evidence-only DoD |

## 3. Сводка покрытия

- FR: 103 требования в 18 доменах; NFR: 22.
- Каждое требование P0/P1 имеет хотя бы одну задачу в [IMPLEMENTATION_BACKLOG.md](IMPLEMENTATION_BACKLOG.md) (колонка «Требования»); контроль — проверка скриптом в P1 (P1-14: генерация матрицы трассировки и падение CI при непокрытом требовании).
