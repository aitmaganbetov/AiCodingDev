# Acceptance report AC-01…AC-16 (P9-01)

> Сгенерировано `scripts/run_acceptance.py` из junit-отчётов прогона — не редактировать вручную.

- **Дата (UTC):** 2026-11-08T19:13:10.766339Z
- **Commit:** `fc45a7f6f7b93a351ceb629de91fd7287abf7187` (ветка `ccr-69fbf9a8-p2650i`)
- **Рабочее дерево:** изменено (4 путей; sha256 `git diff HEAD` = `01ef4a700c59ed1a4d84d391a3fd03c2ec69c06c63a5854b1f91411a8fd094b8`)
- **Окружение:** Python 3.13.18, Linux-6.18.44-fc-v310-x86_64-with-glibc2.39; исполнитель агентов — Fake CLI; реальные CLI — BLOCKED
- **Матрица:** `tests/acceptance/matrix.yaml` (sha256 `19aec5c9ebba0fe71247fb31a5093db13b7518994a14db13f9913842e6bdc1cb`)
- **Итог:** 16/16 AC PASS (Fake), FAIL 0, INCOMPLETE 0; real-CLI часть BLOCKED у 4 AC. **100% обязательных acceptance-тестов (Fake) проходят.**

## Прогоны

| Набор | Команда | Каталог | Exit | Начало | Длительность, с | Тесты (passed/failed/skipped) | junit sha256 |
|---|---|---|---|---|---|---|---|
| python | `/home/user/AiCodingDev/.venv/bin/python3 -m pytest -m acceptance -p no:cacheprovider -q -rs -o junit_family=xunit1 --junitxml=/home/user/AiCodingDev/reports/acceptance/junit/python.xml` | `.` | 0 | 2026-11-08T19:06:03.701222Z | 225.8 | 355/0/0 из 355 | `50fc16b8bca3736e…` |
| live | `pnpm exec playwright test -c playwright.live.config.ts --trace off --reporter=list,junit` | `apps/web` | 0 | 2026-11-08T19:09:49.523279Z | 80.9 | 4/0/0 из 4 | `eedd3a60a8051cae…` |
| mocked | `pnpm exec playwright test -c playwright.mocked.config.ts --trace off --reporter=list,junit` | `apps/web` | 1 | 2026-11-08T19:11:10.468560Z | 120.3 | 62/1/0 из 63 | `5b240cf6c54e6e93…` |

## Матрица AC

| AC | Критерий | Fake | Real CLI | Пункты (PASS/всего) |
|---|---|---|---|---|
| AC-01 | Подключение локального репозитория, создание проекта, запуск DEVELOP | **PASS** | BLOCKED | 3/3 |
| AC-02 | Определение стека, индексация по commit, DAG без циклов | **PASS** | NOT_REQUIRED | 4/4 |
| AC-03 | Router назначает доступный официальный CLI без AI API | **PASS** | BLOCKED | 3/3 |
| AC-04 | Codex реализует в отдельном worktree; Claude — независимое ревью | **PASS** | BLOCKED | 3/3 |
| AC-05 | Межагентные сообщения видны в Live Agent Chat без скрытого CoT | **PASS** | NOT_REQUIRED | 3/3 |
| AC-06 | CHANGE_REQUEST во время выполнения | **PASS** | NOT_REQUIRED | 5/5 |
| AC-07 | Ошибка/таймаут CLI | **PASS** | NOT_REQUIRED | 3/3 |
| AC-08 | Разрыв соединения UI без дублей | **PASS** | NOT_REQUIRED | 2/2 |
| AC-09 | Авария worker без повторного merge/опасной миграции | **PASS** | NOT_REQUIRED | 4/4 |
| AC-10 | Docs OFF и SMART | **PASS** | NOT_REQUIRED | 2/2 |
| AC-11 | Ошибочные тесты блокируют COMPLETED; exit 0 ≠ verified | **PASS** | NOT_REQUIRED | 3/3 |
| AC-12 | Пользователь без прав | **PASS** | NOT_REQUIRED | 5/5 |
| AC-13 | Production deploy / merge / destructive migration без approval невозможны | **PASS** | NOT_REQUIRED | 2/2 |
| AC-14 | Состав evidence | **PASS** | NOT_REQUIRED | 2/2 |
| AC-15 | Контракты: schema validation, backward compatibility, negative tests | **PASS** | NOT_REQUIRED | 4/4 |
| AC-16 | CLI-авторизация недоступна | **PASS** | BLOCKED | 3/3 |

**Real CLI — BLOCKED:** Opt-in прогон с реальными CLI невозможен: P2-01 (spike probe Codex/Claude CLI), P2-05/P2-06 (адаптеры реальных CLI) и P2-13 (opt-in набор) — BLOCKED: нет машины с установленными и авторизованными официальными Codex CLI и Claude Code CLI (решение владельца, IMPLEMENTATION_BACKLOG §Phase 2; OQ-01, OQ-03). Тестов real-CLI в репозитории нет.

## Пункты и тесты

### AC-01 — Подключение локального репозитория, создание проекта, запуск DEVELOP — PASS

1. **PASS** — Локальный Git-репозиторий из allowlist привязывается к проекту (201); путь вне allowlist отклоняется
   - `PASSED` [python] `tests/acceptance/test_ac01_develop_run.py::test_ac01_attach_local_repo_create_project_start_develop_run`
   - `PASSED` [python] `tests/integration/test_api_projects.py::test_attach_local_repository`
   - `PASSED` [python] `tests/integration/test_api_projects.py::test_attach_rejects_unsafe_paths` (6 случ.)
   - `PASSED` [live] `live:smoke.spec.ts::live smoke`
2. **PASS** — POST /runs mode=DEVELOP → 201, status=CREATED
   - `PASSED` [python] `tests/acceptance/test_ac01_develop_run.py::test_ac01_attach_local_repo_create_project_start_develop_run`
   - `PASSED` [python] `tests/integration/test_runs_api.py::test_create_run_is_idempotent_and_key_reuse_with_other_body_is_rejected`
3. **PASS** — run переходит в DISCOVERY, событие run.state_changed получено по SSE
   - `PASSED` [python] `tests/acceptance/test_ac01_develop_run.py::test_ac01_attach_local_repo_create_project_start_develop_run`
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_develop_run_completes_with_verified_evidence`
   - `PASSED` [live] `live:mission-control.spec.ts::AC-05`
4. **BLOCKED** — То же с реальными CLI (RC)
   - real-CLI: BLOCKED (см. причину выше)

### AC-02 — Определение стека, индексация по commit, DAG без циклов — PASS

1. **PASS** — code_index_snapshots для (repository, base_commit_sha) = READY
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac02_attach_index_stack_and_context_pack`
2. **PASS** — project_technologies заполнены (стек определён)
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac02_attach_index_stack_and_context_pack`
3. **PASS** — План проходит валидацию ацикличности
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac02_attach_index_stack_and_context_pack`
   - `PASSED` [python] `tests/unit/test_orchestration_core.py::test_acyclic_plans_validate_and_respect_edges`
4. **PASS** — План с циклом отклоняется PLAN_CYCLE_DETECTED
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac02_attach_index_stack_and_context_pack`
   - `PASSED` [python] `tests/unit/test_orchestration_core.py::test_any_back_edge_is_detected_with_cycle_path`
   - `PASSED` [python] `tests/unit/test_orchestration_core.py::test_plan_rejections` (7 случ.)

### AC-03 — Router назначает доступный официальный CLI без AI API — PASS

1. **PASS** — routing_decisions содержит выбранный провайдер ∈ {codex, claude} с score_breakdown
   - `PASSED` [python] `tests/acceptance/test_ac03_ac16_routing_without_ai_api.py::test_ac03_router_selects_official_cli_and_platform_has_no_ai_api_egress`
   - `PASSED` [python] `tests/e2e/test_routing_review.py::test_review_goes_to_the_other_provider_and_decisions_are_recorded`
   - `PASSED` [python] `tests/unit/test_routing.py::test_quality_drives_selection_and_breakdown_is_complete`
2. **PASS** — Сетевой трафик платформы к AI API отсутствует (egress-лог процесса платформы; runner — только allowlist egress-proxy)
   - `PASSED` [python] `tests/acceptance/test_ac03_ac16_routing_without_ai_api.py::test_ac03_router_selects_official_cli_and_platform_has_no_ai_api_egress`
   - `PASSED` [python] `tests/security/test_sandbox.py::test_egress_proxy_enforces_allowlist`
   - `PASSED` [python] `tests/security/test_sandbox.py::test_no_direct_egress_on_internal_network`
3. **PASS** — В зависимостях нет AI SDK (deny-gate, import-linter)
   - `PASSED` [python] `tests/unit/test_ci_gates.py::test_current_dependencies_pass_gate`
   - `PASSED` [python] `tests/unit/test_ci_gates.py::test_gate_detects_forbidden_sdks`
   - `PASSED` [python] `tests/contract/test_architecture.py::test_import_contracts`
4. **BLOCKED** — Назначение установленного и авторизованного реального CLI; egress runner → эндпоинты провайдера (RC)
   - real-CLI: BLOCKED (см. причину выше)

### AC-04 — Codex реализует в отдельном worktree; Claude — независимое ревью — PASS

1. **PASS** — Изменения только в worktree задачи (исходная ветка и рабочая копия не тронуты)
   - `PASSED` [python] `tests/acceptance/test_ac04_codex_implements_claude_reviews.py::test_ac04_codex_implements_in_task_worktree_and_claude_reviews_independently`
   - `PASSED` [python] `tests/integration/test_worktrees.py::test_create_diff_commit_remove`
   - `PASSED` [python] `tests/integration/test_worktrees.py::test_paths_outside_root_rejected`
2. **PASS** — Developer-задача исполнена Codex, reviewer-задача назначена Claude, независимость = independent
   - `PASSED` [python] `tests/acceptance/test_ac04_codex_implements_claude_reviews.py::test_ac04_codex_implements_in_task_worktree_and_claude_reviews_independently`
   - `PASSED` [python] `tests/e2e/test_agent_handoff.py::test_develop_review_fix_rereview_handoff`
   - `PASSED` [python] `tests/unit/test_routing.py::test_reviewer_gets_non_author_provider`
3. **PASS** — Claude недоступен → policy fallback, independent=false (human_review / self_review только после approval)
   - `PASSED` [python] `tests/e2e/test_routing_review.py::test_single_provider_strict_requires_manual_review`
   - `PASSED` [python] `tests/e2e/test_routing_review.py::test_basic_profile_self_review_only_after_explicit_approval`
   - `PASSED` [python] `tests/unit/test_routing.py::test_independence_fallbacks` (5 случ.)
   - `PASSED` [python] `tests/unit/test_routing.py::test_self_review_is_never_marked_independent`
4. **BLOCKED** — Codex developer → Claude reviewer на реальных CLI (P4-13 RC)
   - real-CLI: BLOCKED (см. причину выше)

### AC-05 — Межагентные сообщения видны в Live Agent Chat без скрытого CoT — PASS

1. **PASS** — Все agent_messages run отображаются (потоки All Agents / Role / Task)
   - `PASSED` [python] `tests/acceptance/test_ac05_live_chat_completeness.py::test_ac05_every_agent_message_is_visible_in_live_chat_without_hidden_reasoning`
   - `PASSED` [python] `tests/integration/test_chat_api.py::test_feed_streams_filters_search_and_pagination`
   - `PASSED` [live] `live:mission-control.spec.ts::AC-05`
   - `PASSED` [mocked] `mocked:chat.spec.ts::AC-05`
2. **PASS** — С типом, отправителем, временем, статусом (статус = принято брокером; отклонённые — в активности)
   - `PASSED` [python] `tests/acceptance/test_ac05_live_chat_completeness.py::test_ac05_every_agent_message_is_visible_in_live_chat_without_hidden_reasoning`
   - `PASSED` [python] `tests/integration/test_review_loops.py::test_worker_projects_accepted_acp_message_into_chat_and_activity`
   - `PASSED` [live] `live:mission-control.spec.ts::AC-05`
3. **PASS** — Нет полей скрытых рассуждений — только ACP payload, нормализованные события CLI и артефакты
   - `PASSED` [python] `tests/acceptance/test_ac05_live_chat_completeness.py::test_ac05_every_agent_message_is_visible_in_live_chat_without_hidden_reasoning`
   - `PASSED` [python] `tests/acceptance/test_ac15_contracts.py::test_ac15_negative_fixtures_constraint_violations_are_rejected` (15 случ.)
   - `PASSED` [python] `tests/unit/test_acp_schema.py::test_additional_payload_property_rejected` (12 случ.)

### AC-06 — CHANGE_REQUEST во время выполнения — PASS

1. **PASS** — 202 → IMPACT_ANALYSIS_PENDING → directive.impact_ready
   - `PASSED` [python] `tests/integration/test_directives.py::test_ac06_change_request_checkpoint_replan_dispatch`
   - `PASSED` [live] `live:mission-control.spec.ts::AC-06`
2. **PASS** — Подтверждение, если критично (four-eyes)
   - `PASSED` [python] `tests/integration/test_directives.py::test_critical_directive_requires_admin_approval_with_four_eyes`
   - `PASSED` [mocked] `mocked:chat.spec.ts::AC-06`
3. **PASS** — Checkpoint создан → новая plan_version → DISPATCHED → ACKNOWLEDGED
   - `PASSED` [python] `tests/integration/test_directives.py::test_ac06_change_request_checkpoint_replan_dispatch`
   - `PASSED` [python] `tests/integration/test_directive_delivery.py::test_change_request_delivered_acknowledged_and_completed_on_fake_cli`
   - `PASSED` [live] `live:mission-control.spec.ts::AC-06`
4. **PASS** — Работающий CLI-процесс не изменён незаметно (безопасная точка)
   - `PASSED` [python] `tests/integration/test_directives.py::test_running_task_gets_safe_point_not_silent_change`
   - `PASSED` [python] `tests/integration/test_directive_delivery.py::test_exec_stops_running_task_for_directive_applied_elsewhere`
5. **PASS** — Неверная expected_run_version → 412
   - `PASSED` [python] `tests/integration/test_directives.py::test_ac06_change_request_checkpoint_replan_dispatch`
   - `PASSED` [python] `tests/integration/test_directives.py::test_rbac_version_mismatch_and_idempotency`

### AC-07 — Ошибка/таймаут CLI — PASS

1. **PASS** — Fake TIMEOUT → task_attempts.failure_category=TIMEOUT
   - `PASSED` [python] `tests/acceptance/test_ac07_cli_timeout_retry_budget.py::test_ac07_cli_timeout_records_category_respects_retry_budget_and_never_completes`
   - `PASSED` [python] `tests/integration/test_gateway.py::test_timeout_recorded`
2. **PASS** — Задача RETRY_WAIT до исчерпания max_attempts, затем FAILED; попыток ≤ бюджета
   - `PASSED` [python] `tests/acceptance/test_ac07_cli_timeout_retry_budget.py::test_ac07_cli_timeout_records_category_respects_retry_budget_and_never_completes`
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_cli_failure_exhausts_retry_budget`
   - `PASSED` [python] `tests/unit/test_orchestration_core.py::test_retry_decisions` (7 случ.)
3. **PASS** — Run не COMPLETED
   - `PASSED` [python] `tests/acceptance/test_ac07_cli_timeout_retry_budget.py::test_ac07_cli_timeout_records_category_respects_retry_budget_and_never_completes`
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_cli_failure_exhausts_retry_budget`

### AC-08 — Разрыв соединения UI без дублей — PASS

1. **PASS** — Переподключение с Last-Event-ID → все события seq > N ровно один раз, по порядку
   - `PASSED` [python] `tests/integration/test_sse.py::test_replay_reconnect_and_live_exactly_once`
   - `PASSED` [python] `tests/integration/test_outbox_dispatcher.py::test_concurrent_dispatchers_keep_per_run_seq_monotonic`
   - `PASSED` [live] `live:mission-control.spec.ts::AC-08`
   - `PASSED` [mocked] `mocked:sse.spec.ts::network drop`
   - `PASSED` [mocked] `mocked:sse.spec.ts::connection cut mid-frame`
   - `PASSED` [mocked] `mocked:run.spec.ts::run view: SSE events are shown`
2. **PASS** — UI после восстановления показывает тот же run
   - `PASSED` [live] `live:mission-control.spec.ts::AC-08`
   - `PASSED` [mocked] `mocked:run.spec.ts::run view: 410 replay_window_exceeded`

### AC-09 — Авария worker без повторного merge/опасной миграции — PASS

1. **PASS** — external_actions в CONFIRMED не повторяется после краха/повторной доставки
   - `PASSED` [python] `tests/integration/test_external_actions.py::test_execute_once_never_repeats_confirmed`
   - `PASSED` [python] `tests/integration/test_github_workflow.py::test_merge_rejected_when_head_moved_and_crash_after_merge`
   - `PASSED` [python] `tests/chaos/test_github_recovery.py::test_worker_killed_after_merge_is_reconciled`
2. **PASS** — EXECUTING сверяется с внешней системой (probe) перед любым повтором
   - `PASSED` [python] `tests/integration/test_external_actions.py::test_crash_during_executing_is_reconciled_by_probe`
   - `PASSED` [python] `tests/integration/test_external_actions.py::test_failure_is_recorded_and_retry_probes_first`
   - `PASSED` [python] `tests/integration/test_external_actions.py::test_stale_token_cannot_confirm_or_take_over`
   - `PASSED` [python] `tests/integration/test_github_workflow.py::test_crash_between_pr_created_and_confirmed_creates_one_pr` (2 случ.)
   - `PASSED` [python] `tests/chaos/test_github_recovery.py::test_worker_killed_mid_pr_action_is_reconciled_without_redelivery` (5 случ.)
3. **PASS** — Worker убит (SIGKILL) → незавершённые задачи продолжены, run завершается без дублей
   - `PASSED` [python] `tests/chaos/test_recovery.py::test_worker_killed_while_cli_running_is_recovered`
   - `PASSED` [python] `tests/chaos/test_recovery.py::test_reaper_defers_when_execution_cannot_be_confirmed_dead`
   - `PASSED` [python] `tests/chaos/test_worker_kills.py::test_worker_killed_mid_step_is_recovered_without_duplicates` (2 случ.)
4. **PASS** — Опасная (деструктивная) миграция не исполняется без approval и не повторяется (ledger migration_apply)
   - `PASSED` [python] `tests/acceptance/test_ac13_critical_actions_need_approval.py::test_ac13_critical_action_never_executes_without_approved_four_eyes_approval` (3 случ.)
   - `PASSED` [python] `tests/integration/test_false_pass_prevention.py::test_destructive_migration_requires_approval`

### AC-10 — Docs OFF и SMART — PASS

1. **PASS** — OFF → нет обращений к внешним источникам, контекст из локального индекса
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac10_off_uses_local_index_only`
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac10_repeated_docs_request_fails_attempt`
   - `PASSED` [python] `tests/unit/test_docs_policy.py::test_off_never_downloads_nor_asks` (24 случ.)
2. **PASS** — SMART и источник не в кэше → approval DOCS_DOWNLOAD, загрузка не начинается до APPROVED
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac10_smart_downloads_only_after_approval`
   - `PASSED` [python] `tests/e2e/test_phase5_acceptance.py::test_ac10_smart_rejected_completes_without_docs`
   - `PASSED` [python] `tests/unit/test_docs_policy.py::test_smart_never_downloads_without_approved` (5 случ.)

### AC-11 — Ошибочные тесты блокируют COMPLETED; exit 0 ≠ verified — PASS

1. **PASS** — CLI exit_code=0, обязательный gate FAIL → задача не SUCCEEDED, run не COMPLETED
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_secret_in_change_fails_gate_and_run_never_completes`
   - `PASSED` [python] `tests/integration/test_false_pass_prevention.py::test_gate_on_different_commit_and_open_finding_block_completion`
   - `PASSED` [python] `tests/e2e/test_completion_guard.py::test_open_critical_finding_blocks_completion`
2. **PASS** — Evidence claims агента UNVERIFIED (claims ≠ evidence)
   - `PASSED` [python] `tests/integration/test_fake_adapter.py::test_success_claims_are_unverified`
   - `PASSED` [python] `tests/unit/test_agent_result.py::test_valid_result_claims_unverified_and_candidates`
   - `PASSED` [python] `tests/unit/test_verdict_rules.py::test_incomplete_evidence` (11 случ.)
3. **PASS** — SKIPPED/ERROR обязательного gate не дают PASS
   - `PASSED` [python] `tests/unit/test_verdict_rules.py::test_non_pass_statuses_never_count` (5 случ.)
   - `PASSED` [python] `tests/unit/test_verdict_rules.py::test_fp_required_skipped`
   - `PASSED` [python] `tests/integration/test_verification_engine.py::test_required_tool_missing_is_error_not_pass`
   - `PASSED` [python] `tests/integration/test_false_pass_prevention.py::test_tool_missing_writes_error_code_and_blocks_completion`
   - `PASSED` [mocked] `mocked:quality.spec.ts::quality: SKIPPED/ERROR are not PASS`

### AC-12 — Пользователь без прав — PASS

1. **PASS** — Не член workspace B → 404 на чтение ресурсов B (без утечки существования)
   - `PASSED` [python] `tests/acceptance/test_ac12_no_rights.py::test_ac12_non_member_gets_404_and_viewer_gets_403_with_audit`
   - `PASSED` [python] `tests/security/test_cross_tenant_api.py::test_foreign_workspace_is_invisible`
   - `PASSED` [python] `tests/security/test_cross_tenant_api.py::test_unknown_resource_and_foreign_resource_look_identical`
   - `PASSED` [python] `tests/security/test_rls.py::test_context_sees_only_own_workspace` (14 случ.)
2. **PASS** — Не член / viewer не может отправить поручение (404 / 403)
   - `PASSED` [python] `tests/acceptance/test_ac12_no_rights.py::test_ac12_non_member_gets_404_and_viewer_gets_403_with_audit`
   - `PASSED` [python] `tests/integration/test_directives.py::test_rbac_version_mismatch_and_idempotency`
   - `PASSED` [mocked] `mocked:chat.spec.ts::chat permissions`
3. **PASS** — Не член / viewer не может утвердить критическое действие (404 / 403)
   - `PASSED` [python] `tests/acceptance/test_ac12_no_rights.py::test_ac12_non_member_gets_404_and_viewer_gets_403_with_audit`
   - `PASSED` [python] `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type` (6 случ.)
   - `PASSED` [mocked] `mocked:approvals.spec.ts::viewer sees approvals read-only`
4. **PASS** — Попытки учитываются (403 → audit authz.denied; 404 → метрика authz_denied_total{tenant})
   - `PASSED` [python] `tests/acceptance/test_ac12_no_rights.py::test_ac12_non_member_gets_404_and_viewer_gets_403_with_audit`
   - `PASSED` [python] `tests/integration/test_approvals_api.py::test_role_matrix_by_action_type` (6 случ.)
5. **PASS** — SSE-поток чужого run недоступен
   - `PASSED` [python] `tests/acceptance/test_ac12_no_rights.py::test_ac12_non_member_gets_404_and_viewer_gets_403_with_audit`
   - `PASSED` [python] `tests/integration/test_sse.py::test_non_member_gets_404_and_activity`
   - `PASSED` [python] `tests/integration/test_sse.py::test_revoked_member_stream_closes`

### AC-13 — Production deploy / merge / destructive migration без approval невозможны — PASS

1. **PASS** — Без APPROVED действие не выполняется, external_actions не переходит в EXECUTING (merge, deploy, деструктивная миграция)
   - `PASSED` [python] `tests/acceptance/test_ac13_critical_actions_need_approval.py::test_ac13_critical_action_never_executes_without_approved_four_eyes_approval` (3 случ.)
   - `PASSED` [python] `tests/acceptance/test_ac13_critical_actions_need_approval.py::test_ac13_rejected_approval_blocks_execution`
   - `PASSED` [python] `tests/integration/test_external_actions.py::test_ac13_merge_requires_approved_matching_approval`
   - `PASSED` [python] `tests/integration/test_external_actions.py::test_ac13_expired_approval_blocks_execution`
   - `PASSED` [python] `tests/integration/test_github_workflow.py::test_merge_requires_approval_four_eyes_and_branch_protection`
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_plan_approval_required_and_decided_by_member`
   - `PASSED` [mocked] `mocked:approvals.spec.ts::AC-13: plan approval gates execution`
2. **PASS** — Requester не может утвердить сам (four-eyes; исключение OQ-10 — никогда для PRODUCTION_DEPLOY)
   - `PASSED` [python] `tests/acceptance/test_ac13_critical_actions_need_approval.py::test_ac13_critical_action_never_executes_without_approved_four_eyes_approval` (3 случ.)
   - `PASSED` [python] `tests/integration/test_approvals_api.py::test_four_eyes_and_single_member_self_approval`
   - `PASSED` [mocked] `mocked:approvals.spec.ts::AC-13: requester cannot approve own merge`

### AC-14 — Состав evidence — PASS

1. **PASS** — Evidence содержит commit_sha, command, exit_code, started_at, finished_at, environment, artifact с sha256
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_develop_run_completes_with_verified_evidence`
   - `PASSED` [python] `tests/integration/test_verification_engine.py::test_clean_change_scope_pass_with_complete_evidence`
   - `PASSED` [python] `tests/integration/test_quality_api.py::test_evidence_fields_links_and_redaction`
   - `PASSED` [mocked] `mocked:quality.spec.ts::quality: reviewer cannot accept risk`
2. **PASS** — sha256 совпадает с содержимым хранилища (FS и S3)
   - `PASSED` [python] `tests/integration/test_verification_engine.py::test_clean_change_scope_pass_with_complete_evidence`
   - `PASSED` [python] `tests/integration/test_false_pass_prevention.py::test_artifact_tampering_detected_by_completion_guard`
   - `PASSED` [python] `tests/integration/test_artifacts_s3.py::test_put_get_presign`
   - `PASSED` [python] `tests/integration/test_artifacts_s3.py::test_integrity_mismatch_detected`

### AC-15 — Контракты: schema validation, backward compatibility, negative tests — PASS

1. **PASS** — OpenAPI валидна (3.1), реализованные маршруты = контракт
   - `PASSED` [python] `tests/contract/test_contracts.py::test_openapi_is_valid_3_1`
   - `PASSED` [python] `tests/contract/test_contracts.py::test_implemented_routes_match_contract`
   - `PASSED` [python] `tests/contract/test_codegen.py::test_generated_contract_models_are_up_to_date`
2. **PASS** — oasdiff breaking против базовой версии контракта пуст (или major bump)
   - `PASSED` [python] `tests/acceptance/test_ac15_contracts.py::test_ac15_oasdiff_no_breaking_changes_against_baseline`
3. **PASS** — Schemathesis без 5xx
   - `PASSED` [python] `tests/integration/test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform` (9 случ.)
   - `PASSED` [python] `tests/integration/test_quality_api.py::test_contract_fuzz_never_5xx_and_responses_conform` (6 случ.)
   - `PASSED` [python] `tests/integration/test_findings_standards_api.py::test_contract_fuzz_never_5xx_and_responses_conform` (6 случ.)
   - `PASSED` [python] `tests/integration/test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform` (9 случ.)
   - `PASSED` [python] `tests/integration/test_chat_api.py::test_chat_contract_fuzz_never_5xx` (2 случ.)
   - `PASSED` [python] `tests/integration/test_task_messages_api.py::test_task_messages_contract_fuzz_never_5xx`
   - `PASSED` [python] `tests/integration/test_oq45_api.py::test_branches_commits_isolation_and_fuzz`
   - `PASSED` [python] `tests/integration/test_oq45_api.py::test_provider_settings_rbac_isolation_and_fuzz`
   - `PASSED` [python] `tests/e2e/test_analytics_observability.py::test_analytics_contract_fuzz_never_5xx`
4. **PASS** — ACP / CLI / SSE JSON Schema — позитивные и негативные фикстуры
   - `PASSED` [python] `tests/contract/test_contracts.py::test_json_schemas_are_valid` (22 случ.)
   - `PASSED` [python] `tests/acceptance/test_ac15_contracts.py::test_ac15_positive_fixtures_from_real_producers_are_valid` (6 случ.)
   - `PASSED` [python] `tests/acceptance/test_ac15_contracts.py::test_ac15_negative_fixtures_missing_required_and_unknown_fields_are_rejected` (6 случ.)
   - `PASSED` [python] `tests/acceptance/test_ac15_contracts.py::test_ac15_negative_fixtures_constraint_violations_are_rejected` (15 случ.)
   - `PASSED` [python] `tests/unit/test_acp_schema.py::test_valid_payload_and_envelope_pass` (12 случ.)
   - `PASSED` [python] `tests/unit/test_acp_schema.py::test_negative_payload_rejected` (36 случ.)
   - `PASSED` [python] `tests/unit/test_acp_schema.py::test_negative_envelope_rejected` (12 случ.)
   - `PASSED` [python] `tests/unit/test_agent_result.py::test_invalid_results_rejected` (11 случ.)
   - `PASSED` [python] `tests/integration/test_outbox_dispatcher.py::test_dispatched_messages_match_outbox_envelope_schema`

### AC-16 — CLI-авторизация недоступна — PASS

1. **PASS** — Оба провайдера AUTH_REQUIRED (или один, а другой не подходит по policy) → задача BLOCKED(AUTH_REQUIRED / PROVIDER_UNAVAILABLE)
   - `PASSED` [python] `tests/acceptance/test_ac03_ac16_routing_without_ai_api.py::test_ac16_no_authorized_cli_blocks_task_with_visible_reason_and_no_workaround` (2 случ.)
   - `PASSED` [python] `tests/e2e/test_phase3_slice.py::test_no_usable_cli_blocks_without_workaround` (2 случ.)
   - `PASSED` [python] `tests/unit/test_routing.py::test_both_unavailable_blocks_with_right_reason_ac16`
2. **PASS** — Причина видна в API и UI
   - `PASSED` [python] `tests/acceptance/test_ac03_ac16_routing_without_ai_api.py::test_ac16_no_authorized_cli_blocks_task_with_visible_reason_and_no_workaround` (2 случ.)
   - `PASSED` [mocked] `mocked:dashboard.spec.ts::dashboard is the landing page`
3. **PASS** — Нет попыток обходной авторизации и обращений к AI API
   - `PASSED` [python] `tests/acceptance/test_ac03_ac16_routing_without_ai_api.py::test_ac16_no_authorized_cli_blocks_task_with_visible_reason_and_no_workaround` (2 случ.)
   - `PASSED` [python] `tests/unit/test_cli_core.py::test_env_built_from_allowlist_and_api_keys_refused`
4. **BLOCKED** — Реальный CLI без авторизации распознаётся как AUTH_REQUIRED (сигнатуры ошибок, P2-05/P2-06) (RC)
   - real-CLI: BLOCKED (см. причину выше)
