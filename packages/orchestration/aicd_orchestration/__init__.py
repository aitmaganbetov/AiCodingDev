"""Модуль orchestration (MODULE_BOUNDARIES §3): runs/tasks/DAG, FSM, approvals, directives, checkpoints,
ledger внешних действий, worktrees и справочник переходов FSM."""

OWNED_TABLES = frozenset(
    {
        "worktrees",
        "allowed_transitions",
        "runs",
        "tasks",
        "task_dependencies",
        "task_attempts",
        "agent_profiles",
        "agent_assignments",
        "routing_decisions",
        "agent_decisions",
        "approvals",
        "checkpoints",
        "user_directives",
        "directive_impacts",
        "directive_acknowledgements",
        "policies",
        "external_actions",
    }
)
