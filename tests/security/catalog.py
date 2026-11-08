"""Каталог security suite (P9-02): контроль SECURITY_CONTROLS → тесты, которые его проверяют.

Единственный источник для:
* маркера `security` (`pytest -m security` — один запускаемый набор: всё в `tests/security/` + перечисленные
  здесь тесты других каталогов; маркер ставит `tests/conftest.py`);
* отчёта `reports/security/SECURITY_REPORT.md` (`scripts/security_report.py`: контроль → тест → результат по
  junit-XML фактического прогона).

`tests` — префиксы node id pytest: файл (`tests/x.py` — все тесты файла) или тест (`tests/x.py::test_y`,
включая параметризации). `kind`:
* `automated` — контроль проверяется перечисленными тестами;
* `partial` — тесты покрывают часть контроля, остаток описан в `note` (и в THREAT_MODEL §8 / OPEN_QUESTIONS);
* `manual` — контроль организационный/инфраструктурный, автоматического теста нет (`note` — чем проверяется).

ТЗ §20 «Security»: SSRF, prompt injection, path traversal, command injection, cross-tenant access, secret leakage,
webhook spoofing — категория указана в `TZ20` (каждая категория обязана иметь хотя бы один тест).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

Kind = Literal["automated", "partial", "manual"]


@dataclass(frozen=True)
class Control:
    tests: tuple[str, ...] = ()
    kind: Kind = "automated"
    note: str = ""
    tz20: tuple[str, ...] = field(default=())


U, I, S, M, C, E, X = (
    "tests/unit/",
    "tests/integration/",
    "tests/security/",
    "tests/migrations/",
    "tests/contract/",
    "tests/e2e/",
    "tests/chaos/",
)

CONTROLS: Final[dict[str, Control]] = {
    # --- IAM ---------------------------------------------------------------------------------------------
    "SC-IAM-01": Control((U + "test_passwords.py::test_argon2id_hash_and_verify",)),
    "SC-IAM-02": Control(
        (U + "test_passwords.py::test_policy_rejects_weak", I + "test_cli.py::test_weak_password_rejected")
    ),
    "SC-IAM-03": Control(
        (
            I + "test_api_auth.py::test_login_sets_hardened_cookies_and_matches_contract",
            I + "test_api_auth.py::test_idle_timeout_expires_session",
            U + "test_passwords.py::test_tokens_hash_only",
        )
    ),
    "SC-IAM-04": Control(
        (
            I + "test_api_auth.py::test_logout_revokes_session",
            S + "test_step_up.py::test_stale_session_requires_step_up_then_reauth_rotates_session",
        ),
        "partial",
        "Ротация при входе и step-up, отзыв при logout проверены; смены пароля и принудительного завершения сессий "
        "owner/admin в API v1 нет (функции отсутствуют, OQ-04).",
    ),
    "SC-IAM-05": Control(
        (
            I + "test_api_auth.py::test_unsafe_requests_require_csrf_and_same_origin",
            I + "test_api_auth.py::test_login_rejects_cross_site_origin",
            S + "test_step_up.py::test_stale_session_requires_step_up_then_reauth_rotates_session",
        )
    ),
    "SC-IAM-06": Control(
        (
            I + "test_api_auth.py::test_lockout_after_failures_even_with_correct_password",
            I + "test_api_auth.py::test_bad_credentials_are_indistinguishable",
            S + "test_rate_limits.py::test_login_is_rate_limited_per_ip",
        )
    ),
    "SC-IAM-07": Control(
        (
            U + "test_rbac.py",
            C + "test_contracts.py::test_every_contract_permission_is_known_to_rbac",
            I + "test_github_api.py::test_rbac_matrix_and_cross_workspace",
            I + "test_runs_api.py::test_runs_are_tenant_isolated_and_role_checked",
            I + "test_api_projects.py::test_role_restrictions",
        )
    ),
    "SC-IAM-08": Control((S + "test_step_up.py",)),
    "SC-IAM-09": Control((I + "test_cli.py::test_create_owner_then_login",)),
    # --- TEN ---------------------------------------------------------------------------------------------
    "SC-TEN-01": Control(
        (
            M + "test_migrations.py::test_every_tenant_table_has_forced_rls_and_policy",
            M + "test_migrations.py::test_app_roles_cannot_bypass_rls",
            S + "test_rls.py",
            M + "test_orchestration_invariants.py::test_tenant_tables_isolated",
            M + "test_security_hardening_migration.py::test_verification_table_rls_grants_and_terminal_states",
        ),
        tz20=("cross-tenant",),
    ),
    "SC-TEN-02": Control(
        (
            S + "test_rls.py::test_context_sees_only_own_workspace",
            M + "test_orchestration_invariants.py::test_app_cannot_write_foreign_run",
        ),
        "partial",
        "SET LOCAL в каждой транзакции и fail-closed без контекста проверены; линтер «SET без LOCAL» не реализован.",
    ),
    "SC-TEN-03": Control(
        (
            S + "test_rls.py::test_composite_fk_blocks_cross_tenant_reference",
            M + "test_orchestration_invariants.py::test_cross_run_dependency_rejected",
        ),
        tz20=("cross-tenant",),
    ),
    "SC-TEN-04": Control(
        (
            S + "test_cross_tenant_all_operations.py",
            S + "test_cross_tenant_api.py",
            I + "test_artifact_download.py::test_expired_missing_foreign_and_unknown_are_404",
            I + "test_sse.py::test_non_member_gets_404_and_activity",
            I + "test_workspace_events.py::test_workspace_stream_errors_and_isolation",
            I + "test_quality_api.py::test_non_members_get_404_and_anonymous_401",
            I + "test_findings_standards_api.py::test_non_members_get_404_and_anonymous_401",
            I + "test_repository_explorer.py::test_workspace_isolation",
        ),
        tz20=("cross-tenant",),
    ),
    "SC-TEN-05": Control(
        (
            I + "test_artifact_download.py",
            I + "test_artifacts_s3.py::test_foreign_bucket_ref_rejected",
            U + "test_artifacts_fs.py::test_bad_refs",
        ),
        tz20=("cross-tenant", "path traversal"),
    ),
    "SC-TEN-06": Control(
        (
            I + "test_sse.py::test_revoked_member_stream_closes",
            I + "test_workspace_events.py::test_workspace_stream_closes_after_member_removed",
        ),
        "partial",
        "Данные клиенту — только из run_event_log после authz (проверено); префиксы ключей Redis по workspace "
        "тестом не проверяются.",
    ),
    "SC-TEN-07": Control(
        (
            I + "test_code_index_db.py::test_rls_isolates_index",
            I + "test_context_pack.py::test_cache_hit_reuses_artifact",
        )
    ),
    "SC-TEN-08": Control(
        (
            M + "test_migrations.py::test_app_roles_cannot_bypass_rls",
            I + "test_retention.py::test_retention_requires_maintenance_role",
        )
    ),
    # --- SBX ---------------------------------------------------------------------------------------------
    "SC-SBX-01": Control(
        (
            U + "test_container_policy.py::test_build_host_config_hardening",
            S + "test_sandbox.py::test_runs_as_non_root_without_capabilities",
            S + "test_sandbox.py::test_inspect_confirms_hardening_and_limits",
        )
    ),
    "SC-SBX-02": Control((S + "test_sandbox.py::test_writes_only_inside_worktree", S + "test_adapter_in_sandbox.py")),
    "SC-SBX-03": Control(
        (
            S + "test_sandbox.py::test_pids_limit_enforced",
            S + "test_sandbox.py::test_memory_limit_kills_allocation",
            S + "test_sandbox.py::test_timeout_terminates_and_removes",
            S + "test_sandbox.py::test_output_truncated_but_process_accounted",
            I + "test_fake_adapter.py::test_output_limit_truncates_without_killing",
        )
    ),
    "SC-SBX-04": Control(
        (I + "test_local_runner.py::test_terminate_escalates_to_sigkill_and_kills_children", X + "test_recovery.py")
    ),
    "SC-SBX-05": Control(
        (
            U + "test_container_policy.py::test_linter_rejects_forbidden_config",
            U + "test_container_policy.py::test_linter_image_allowlist_and_digest",
            S + "test_sandbox.py::test_socket_proxy_denies_forbidden_endpoints",
            S + "test_sandbox.py::test_policy_violation_rejected_before_create",
        )
    ),
    "SC-SBX-06": Control(
        (
            S + "test_sandbox.py::test_no_network_on_none",
            S + "test_sandbox.py::test_no_direct_egress_on_internal_network",
            S + "test_adapter_in_sandbox.py::test_network_is_unavailable_in_runner",
        )
    ),
    "SC-SBX-07": Control(
        (
            I + "test_verification_engine.py::test_scope_gate_in_verification_container",
            I + "test_gate_runners_docker.py",
            U + "test_security_gates.py::test_container_spec_is_read_only_offline_and_passes_policy_lint",
        )
    ),
    "SC-SBX-08": Control(
        (
            I + "test_gate_runners_docker.py::test_unpinned_image_is_rejected_by_server_policy",
            U + "test_security_gates.py::test_images_are_pinned_by_digest_without_latest",
        ),
        "partial",
        "Pinning по digest проверен; подпись Cosign runner-образов при запуске не проверяется (P9 ops, OQ).",
    ),
    "SC-SBX-09": Control((), "manual", "gVisor/rootless — опция профиля server, opt-in; автоматического теста нет."),
    "SC-SBX-10": Control(
        (
            I + "test_local_runner.py::test_environment_is_not_inherited",
            I + "test_worker_actors.py::test_exec_worker_refuses_fake_adapter_in_production",
        ),
        "partial",
        "Local fallback без approval — запрет конфигурацией; E2E AC с approval LOCAL_EXECUTION_FALLBACK не автоматизирован.",
    ),
    "SC-SBX-11": Control(
        (
            S + "test_command_injection.py::test_platform_git_ignores_malicious_repository_config",
            S + "test_command_injection.py::test_worktree_gitlink_redirect_is_refused",
            S + "test_command_injection.py::test_worktree_gitattributes_filters_do_not_run",
            U + "test_code_index_builder.py::test_read_blobs_does_not_apply_filters",
        ),
        tz20=("command injection",),
    ),
    "SC-SBX-12": Control(
        (
            U + "test_paths.py",
            I + "test_worktrees.py::test_paths_outside_root_rejected",
            I + "test_repository_explorer.py::test_unsafe_paths_rejected",
            I + "test_repository_explorer.py::test_binary_large_and_symlink_files",
            U + "test_agent_result.py::test_declared_output_must_exist_and_not_escape",
            U + "test_code_index_builder.py::test_tree_parsing_drops_unsafe_paths",
            I + "test_api_projects.py::test_attach_rejects_unsafe_paths",
            I + "test_retention.py::test_archive_sink_rejects_traversal",
            U + "test_gates_applicability.py::test_scanner_allows_symlink_inside_worktree",
        ),
        tz20=("path traversal",),
    ),
    # --- CLI ---------------------------------------------------------------------------------------------
    "SC-CLI-01": Control(
        (
            U + "test_cli_core.py::test_argv_is_array_from_allowlisted_placeholders",
            U + "test_cli_core.py::test_argv_rejections",
        ),
        tz20=("command injection",),
    ),
    "SC-CLI-02": Control(
        (S + "test_command_injection.py::test_objective_never_reaches_argv_or_shell",), tz20=("command injection",)
    ),
    "SC-CLI-03": Control(
        (
            S + "test_sandbox.py::test_host_secrets_not_visible",
            I + "test_fake_adapter.py::test_process_environment_is_allowlisted",
        ),
        "partial",
        "Canary-файл в томе учётных данных реального провайдера — opt-in suite реальных CLI.",
    ),
    "SC-CLI-04": Control(
        (
            I + "test_providers.py::test_probe_recorded_and_listed",
            U + "test_cli_core.py::test_env_built_from_allowlist_and_api_keys_refused",
        )
    ),
    "SC-CLI-05": Control(
        (
            U + "test_routing.py::test_both_unavailable_blocks_with_right_reason_ac16",
            U + "test_cli_core.py::test_signature_priority_and_retry_after",
        )
    ),
    "SC-CLI-06": Control((I + "test_fake_adapter.py", U + "test_agent_result.py::test_strict_json")),
    "SC-CLI-07": Control(
        (), "manual", "Строгий режим разрешений реального CLI — opt-in suite реальных CLI (подписки)."
    ),
    "SC-CLI-08": Control((U + "test_cli_core.py::test_compat_matrix",)),
    "SC-CLI-09": Control(
        (U + "test_cli_core.py::test_compat_matrix",),
        "partial",
        "Только документированные события — ревью шаблонов и адаптеров.",
    ),
    # --- SEC ---------------------------------------------------------------------------------------------
    "SC-SEC-01": Control(
        (
            M + "test_migrations.py::test_no_secret_columns",
            S + "test_secret_leakage.py::test_cli_output_secrets_absent_from_db_and_artifacts",
        ),
        tz20=("secret leakage",),
    ),
    "SC-SEC-02": Control(
        (
            U + "test_config.py::test_short_secret_rejected",
            U + "test_redaction.py::test_structures_redact_sensitive_keys",
        )
    ),
    "SC-SEC-03": Control(
        (
            S + "test_secret_leakage.py",
            U + "test_redaction.py",
            I + "test_fake_adapter.py::test_secrets_redacted_in_events_and_raw_log",
            I + "test_quality_api.py::test_evidence_fields_links_and_redaction",
        ),
        tz20=("secret leakage",),
    ),
    "SC-SEC-04": Control(
        (
            I + "test_verification_engine.py::test_secret_in_diff_fails_scope_without_leaking_value",
            U + "test_gates_applicability.py::test_scanner_detects_secret_without_printing_value",
            S + "test_security_gates_docker.py::test_gitleaks_detects_secrets_ignoring_repo_suppressions",
        ),
        tz20=("secret leakage",),
    ),
    "SC-SEC-05": Control(
        (U + "test_code_index_builder.py::test_full_build_exclusions_and_flags",), tz20=("secret leakage",)
    ),
    "SC-SEC-06": Control(
        (), "manual", "Runbook ротации секретов (ops); approval SECRET_CHANGE — правило four-eyes (SC-APR-02)."
    ),
    "SC-SEC-07": Control(
        (S + "test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets",),
        "partial",
        "Без трассировок в ответах — проверено; RLIMIT_CORE и запрет debug в prod — конфигурация образа.",
    ),
    # --- INJ ---------------------------------------------------------------------------------------------
    "SC-INJ-01": Control(
        (U + "test_prompt_injection_corpus.py", U + "test_prompt_assembly.py"), tz20=("prompt injection",)
    ),
    "SC-INJ-02": Control(
        (
            U + "test_prompt_injection_corpus.py::test_injection_does_not_alter_policy_or_escape_fence",
            U + "test_prompt_assembly.py::test_policy_block_from_profile_only",
            I + "test_agent_io.py::test_acl_violations_rejected_valid_message_accepted",
        ),
        tz20=("prompt injection",),
    ),
    "SC-INJ-03": Control(
        (U + "test_acp_schema.py", U + "test_acp_acl.py", U + "test_acp_pipeline.py"), tz20=("prompt injection",)
    ),
    "SC-INJ-04": Control(
        (
            I + "test_false_pass_prevention.py",
            U + "test_verdict_rules.py",
            U + "test_agent_result.py::test_valid_result_claims_unverified_and_candidates",
        ),
        tz20=("prompt injection",),
    ),
    "SC-INJ-05": Control(
        (
            I + "test_directives.py::test_rbac_version_mismatch_and_idempotency",
            U + "test_rbac.py::test_directive_rules",
        ),
        tz20=("prompt injection",),
    ),
    "SC-INJ-06": Control(
        (U + "test_agent_result.py::test_suspicious_text_flagged_not_blocked", U + "test_quality_report.py"),
        "partial",
        "Экранирование в UI — unit-тесты web (vitest) и React; XSS-корпус через e2e браузера не автоматизирован.",
    ),
    "SC-INJ-07": Control(
        (
            S + "test_command_injection.py::test_untrusted_branch_names_rejected",
            S + "test_command_injection.py::test_git_argument_values_are_not_shell_interpreted",
            U + "test_code_index_search.py::test_injection_is_literal",
            U + "test_github_app.py::test_push_ref_guards",
            I + "test_worktrees.py::test_base_commit_must_be_existing_full_sha",
        ),
        tz20=("command injection",),
    ),
    # --- NET ---------------------------------------------------------------------------------------------
    "SC-NET-01": Control((S + "test_sandbox.py::test_egress_proxy_enforces_allowlist",), tz20=("SSRF",)),
    "SC-NET-02": Control(
        (C + "test_observability.py::test_observability_profile_is_pinned_and_not_public",),
        "partial",
        "Матрица достижимости сетей compose — ops/smoke профиля server.",
    ),
    "SC-NET-03": Control((), "manual", "TLS/HSTS на edge — конфигурация reverse-proxy (ops)."),
    "SC-NET-04": Control((), "manual", "Redis ACL/requirepass, PG TLS — конфигурация развёртывания (ops)."),
    "SC-NET-05": Control((S + "test_sandbox.py::test_no_direct_egress_on_internal_network",), tz20=("SSRF",)),
    # --- DOC (SSRF) --------------------------------------------------------------------------------------
    "SC-DOC-01": Control(
        (S + "test_ssrf.py::test_url_pipeline_blocks", S + "test_ssrf.py::test_valid_url_normalized"), tz20=("SSRF",)
    ),
    "SC-DOC-02": Control(
        (
            S + "test_ssrf.py::test_connection_uses_pinned_ip_not_second_resolution",
            S + "test_ssrf.py::test_any_private_answer_blocks_whole_name",
            S + "test_ssrf.py::test_redirect_rebinding_is_rechecked",
        ),
        tz20=("SSRF",),
    ),
    "SC-DOC-03": Control(
        (
            S + "test_ssrf.py::test_denylist_blocks_private_and_special_addresses",
            S + "test_ssrf.py::test_metadata_addresses_blocked_even_when_allowlisted",
            S + "test_ssrf.py::test_ip_literals_checked_against_denylist_when_allowed",
        ),
        tz20=("SSRF",),
    ),
    "SC-DOC-04": Control(
        (
            S + "test_ssrf.py::test_redirect_to_blocked_target",
            S + "test_ssrf.py::test_too_many_redirects",
            S + "test_ssrf.py::test_redirect_from_local_server_to_metadata_blocked",
        ),
        tz20=("SSRF",),
    ),
    "SC-DOC-05": Control(
        (
            S + "test_ssrf.py::test_oversized_body_aborted_regardless_of_content_length",
            S + "test_ssrf.py::test_gzip_content_encoding_bomb_capped",
            S + "test_ssrf.py::test_wrong_mime_rejected",
        ),
        tz20=("SSRF",),
    ),
    "SC-DOC-06": Control((S + "test_doc_archives.py",), tz20=("path traversal",)),
    "SC-DOC-07": Control(
        (
            S + "test_doc_archives.py::test_html_sanitization",
            S + "test_doc_archives.py::test_pdf_not_parsed_in_process",
        ),
        tz20=("prompt injection",),
    ),
    "SC-DOC-08": Control((U + "test_docs_policy.py", I + "test_docs_tech_api.py::test_importer_end_to_end")),
    # --- GH ----------------------------------------------------------------------------------------------
    "SC-GH-01": Control(
        (
            I + "test_github_api.py::test_webhook_signature_dedup_replay_and_limits",
            U + "test_github_app.py::test_webhook_signature_checks",
            I + "test_observability_signals.py::test_rejected_webhook_signatures_are_counted",
        ),
        tz20=("webhook spoofing",),
    ),
    "SC-GH-02": Control(
        (
            I + "test_github_api.py::test_webhook_signature_dedup_replay_and_limits",
            I + "test_github_workflow.py::test_webhook_processing_rereads_state_and_dedups",
        ),
        tz20=("webhook spoofing",),
    ),
    "SC-GH-03": Control(
        (I + "test_github_workflow.py::test_webhook_processing_rereads_state_and_dedups",), tz20=("webhook spoofing",)
    ),
    "SC-GH-04": Control(
        (
            U + "test_github_app.py::test_app_permission_policy",
            U + "test_github_app.py::test_installation_token_is_repo_scoped_and_minimal",
            U + "test_github_app.py::test_push_uses_askpass_and_leaves_no_token",
            I + "test_github_api.py::test_installation_flow_state_and_permissions",
        ),
        "partial",
        "Отступление OQ-47 (1): `statuses: write` вместо `read` — принято владельцем.",
    ),
    "SC-GH-05": Control(
        (
            I + "test_github_api.py::test_pull_request_request_and_ci_config_guard",
            U + "test_github_app.py::test_force_push_over_foreign_history_is_rejected",
            I + "test_github_workflow.py::test_merge_requires_approval_four_eyes_and_branch_protection",
        )
    ),
    "SC-GH-06": Control(
        (
            I + "test_github_api.py::test_api_process_never_loads_github_app_key",
            I + "test_github_api.py::test_attach_github_repository",
            U + "test_github_app.py::test_agent_cli_environment_rejects_github_tokens",
        )
    ),
    # --- APR ---------------------------------------------------------------------------------------------
    "SC-APR-01": Control(
        (
            U + "test_approvals_rules.py::test_role_matrix_per_action_type",
            I + "test_external_actions.py::test_ac13_merge_requires_approved_matching_approval",
            I + "test_false_pass_prevention.py::test_destructive_migration_requires_approval",
        )
    ),
    "SC-APR-02": Control(
        (
            U + "test_approvals_rules.py::test_four_eyes_blocks_requester",
            M + "test_orchestration_invariants.py::test_four_eyes_and_decider_role",
            I + "test_approvals_api.py",
        )
    ),
    "SC-APR-03": Control(
        (
            I + "test_github_workflow.py::test_merge_rejected_when_head_moved_and_crash_after_merge",
            I + "test_external_actions.py::test_ac13_expired_approval_blocks_execution",
            U + "test_approvals_rules.py::test_subject_hash_is_canonical",
        )
    ),
    "SC-APR-04": Control((I + "test_external_actions.py", X + "test_github_recovery.py")),
    "SC-APR-05": Control(
        (U + "test_routing.py::test_self_review_is_never_marked_independent", E + "test_routing_review.py")
    ),
    "SC-APR-06": Control(
        (
            S + "test_step_up.py::test_critical_approval_requires_step_up_non_critical_does_not",
            I + "test_approvals_api.py::test_plan_approval_flow_etag_idempotency_and_hook",
        )
    ),
    # --- AUD ---------------------------------------------------------------------------------------------
    "SC-AUD-01": Control((M + "test_migrations.py::test_audit_events_append_only",)),
    "SC-AUD-02": Control(
        (), "manual", "Hash chain + WORM-якорь — P9 приоритет P2, не реализовано (THREAT_MODEL TH-34 residual)."
    ),
    "SC-AUD-03": Control((S + "test_audit_coverage.py", I + "test_api_projects.py::test_audit_trail_records_actions")),
    "SC-AUD-04": Control(
        (S + "test_cross_tenant_api.py::test_foreign_workspace_is_invisible",),
        "partial",
        "Чтение аудита только owner/admin — матрица прав (SC-IAM-07); allowlist metadata — ревью кода.",
    ),
    # --- SUP ---------------------------------------------------------------------------------------------
    "SC-SUP-01": Control(
        (U + "test_ci_gates.py::test_real_lockfiles_are_parsed",),
        "partial",
        "Pinning Actions/образов — CI-линтер (ops).",
    ),
    "SC-SUP-02": Control(
        (
            S + "test_security_gates_docker.py::test_osv_scanner_offline_db_finds_vulnerability",
            U + "test_security_gates.py::test_sca_and_iac_verdicts",
        ),
        "partial",
        "Блок релиза по SCA — CI-пайплайн платформы (ops).",
    ),
    "SC-SUP-03": Control(
        (S + "test_security_gates_docker.py::test_syft_sbom_is_cyclonedx_artifact",),
        "partial",
        "SBOM релиза — scripts ops (P9-06).",
    ),
    "SC-SUP-04": Control(
        (S + "test_security_gates_docker.py::test_cosign_verifies_blob_offline_and_rejects_tampering",),
        "partial",
        "Подпись образов релиза и проверка при развёртывании — ops.",
    ),
    "SC-SUP-05": Control((), "manual", "SLSA provenance — CI релиза (ops), приоритет P2."),
    "SC-SUP-06": Control((), "manual", "Линтер workflow CI (zizmor/actionlint) — CI платформы."),
    "SC-SUP-07": Control(
        (
            I + "test_gate_runners_docker.py",
            U + "test_security_gates.py::test_container_spec_is_read_only_offline_and_passes_policy_lint",
        )
    ),
    "SC-SUP-08": Control(
        (
            U + "test_ci_gates.py::test_gate_detects_forbidden_sdks",
            U + "test_ci_gates.py::test_current_dependencies_pass_gate",
        ),
        "partial",
        "Политика обновлений/SLA — процесс.",
    ),
    # --- DATA --------------------------------------------------------------------------------------------
    "SC-DATA-01": Control(
        (C + "test_architecture.py",), "partial", "Класс данных каждой таблицы — ревью DATABASE_DESIGN."
    ),
    "SC-DATA-02": Control((), "manual", "Шифрование at rest — doctor/ops (P9-05)."),
    "SC-DATA-03": Control((U + "test_backup_crypto.py",), "partial", "Restore drill — ops (P9-05)."),
    "SC-DATA-04": Control((I + "test_retention.py",)),
    "SC-DATA-05": Control(
        (
            U + "test_artifacts_fs.py::test_immutable_and_integrity",
            I + "test_artifacts_s3.py::test_integrity_mismatch_detected",
            I + "test_false_pass_prevention.py::test_artifact_tampering_detected_by_completion_guard",
            I + "test_artifact_download.py::test_integrity_mismatch_aborts_stream",
        )
    ),
    "SC-DATA-06": Control(
        (
            I + "test_artifact_download.py::test_download_streams_content_with_headers_and_audit",
            I + "test_artifact_download.py::test_raw_cli_log_requires_admin",
        )
    ),
    "SC-DATA-07": Control(
        (I + "test_retention.py::test_retention_job_on_seeded_db",),
        "partial",
        "Удаление IP/UA через 30 дней — retention сессий.",
    ),
    "SC-DATA-08": Control(
        (
            I + "test_external_actions.py::test_stale_token_cannot_confirm_or_take_over",
            M + "test_orchestration_invariants.py::test_lease_fencing_token_monotonic",
            I + "test_gateway.py",
        )
    ),
    "SC-DATA-09": Control(
        (
            I + "test_worker_actors.py::test_stub_broker_duplicate_delivery_processed_once",
            X + "test_redis_restart.py",
            I + "test_outbox_dispatcher.py::test_publish_failure_backoff_then_redelivery_without_duplicates",
            I + "test_acp_broker.py::test_duplicate_delivery_has_one_effect",
        )
    ),
    # --- OBS ---------------------------------------------------------------------------------------------
    "SC-OBS-01": Control(
        (
            U + "test_redaction.py::test_json_log_line_has_correlation_and_no_secrets",
            S + "test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets",
        ),
        tz20=("secret leakage",),
    ),
    "SC-OBS-02": Control(
        (
            I + "test_observability_signals.py",
            I + "test_monitoring_promtool.py::test_promtool_rules_are_valid_and_alerts_fire",
        )
    ),
    "SC-OBS-03": Control(
        (
            U + "test_telemetry.py::test_span_attributes_are_allowlisted",
            U + "test_metrics.py::test_label_values_are_bounded",
            S + "test_secret_leakage.py::test_metrics_labels_carry_no_secrets_or_unbounded_ids",
        ),
        tz20=("secret leakage",),
    ),
    # --- API ---------------------------------------------------------------------------------------------
    "SC-API-01": Control(
        (
            I + "test_api_workspaces.py::test_unknown_fields_rejected",
            I + "test_runs_api.py::test_contract_fuzz_never_5xx_and_responses_conform",
            I + "test_github_api.py::test_contract_fuzz_never_5xx_and_responses_conform",
        )
    ),
    "SC-API-02": Control(
        (
            S + "test_rate_limits.py",
            S + "test_step_up.py::test_reauthenticate_is_rate_limited_per_user",
            I + "test_oq45_api.py::test_probe_rbac_rate_limit_and_audit",
        )
    ),
    "SC-API-03": Control(
        (
            I + "test_sse.py::test_replay_window_exceeded_and_stream_limit",
            I + "test_workspace_events.py::test_workspace_stream_respects_connection_limit",
        ),
        "partial",
        "Квоты runs/DAG — unit-лимиты DAG; нагрузочные пороги — P9 performance.",
    ),
    "SC-API-04": Control(
        (
            S + "test_secret_leakage.py::test_error_responses_and_logs_never_echo_secrets",
            I + "test_api_projects.py::test_security_headers_and_problem_format",
            S + "test_cross_tenant_api.py::test_unknown_resource_and_foreign_resource_look_identical",
        ),
        tz20=("secret leakage",),
    ),
    "SC-API-05": Control(
        (
            I + "test_api_projects.py::test_idempotency_replay_reuse_and_missing",
            I + "test_runs_api.py::test_create_run_is_idempotent_and_key_reuse_with_other_body_is_rejected",
        )
    ),
    "SC-API-06": Control(
        (
            I + "test_runs_api.py::test_get_run_etag_and_commands_with_if_match",
            I + "test_api_projects.py::test_project_crud_with_etag",
        )
    ),
    "SC-API-07": Control(
        (
            I + "test_sse.py::test_revoked_member_stream_closes",
            I + "test_sse.py::test_non_member_gets_404_and_activity",
            I + "test_workspace_events.py::test_workspace_stream_closes_after_member_removed",
        ),
        tz20=("cross-tenant",),
    ),
    "SC-API-08": Control((I + "test_api_projects.py::test_security_headers_and_problem_format",)),
    "SC-API-09": Control(
        (
            I + "test_api_workspaces.py::test_unknown_fields_rejected",
            S + "test_cross_tenant_api.py::test_foreign_workspace_is_invisible",
        )
    ),
}

# ТЗ §20 «Security»: каждая категория обязана иметь хотя бы один автоматический тест.
TZ20_CATEGORIES: Final = (
    "SSRF",
    "prompt injection",
    "path traversal",
    "command injection",
    "cross-tenant",
    "secret leakage",
    "webhook spoofing",
)


def _matches(nodeid: str, prefix: str) -> bool:
    return nodeid == prefix or nodeid.startswith((prefix + "[", prefix + "::"))


def controls_for_nodeid(nodeid: str) -> list[str]:
    return sorted(cid for cid, c in CONTROLS.items() if any(_matches(nodeid, p) for p in c.tests))


def is_security_test(nodeid: str) -> bool:
    return nodeid.startswith("tests/security/") or bool(controls_for_nodeid(nodeid))
