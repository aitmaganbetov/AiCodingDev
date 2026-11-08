"""Модуль orchestration (MODULE_BOUNDARIES §3). В Phase 2 — управление worktree и справочник переходов FSM;
runs/tasks/DAG/scheduler/approvals появятся в Phase 3."""

OWNED_TABLES = frozenset({"worktrees", "allowed_transitions"})
