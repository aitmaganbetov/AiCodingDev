"""Преобразование строк БД в представления API (схемы контракта)."""

from __future__ import annotations

from typing import Any


def workspace(row: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "slug": row["slug"],
        "role": role,
        "settings": row.get("settings") or {},
        "created_at": row["created_at"],
    }


def member(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user": {"id": row["user_id"], "display_name": row["display_name"]},
        "email": row["email"],
        "role": row["role"],
        "joined_at": row["joined_at"],
        "version": row["version"],
    }


def project(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "name": row["name"],
        "description": row["description"],
        "status": row["status"],
        "settings": {
            "documentation_mode": row["documentation_mode"],
            "quality_profile": row["quality_profile"],
            "routing_policy_id": row["routing_policy_id"],
            "security_profile": row["security_profile"],
            "settings_version": row["settings_version"],
        },
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def repository(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "provider": row["provider"],
        "remote_url": row["remote_url"],
        "default_branch": row["default_branch"],
        "local_root_ref": row["local_root_ref"],
        "last_synced_at": row["last_synced_at"],
        # Последний READY-снимок индекса кода репозитория (P5-02); до первой индексации — null.
        "latest_index_snapshot": (
            {
                "snapshot_id": row["index_snapshot_id"],
                "commit_sha": row["index_commit_sha"],
                "status": row["index_status"],
            }
            if row.get("index_snapshot_id")
            else None
        ),
    }


def audit_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "actor_type": row["actor_type"],
        "actor_id": row["actor_id"],
        "action": row["action"],
        "resource_type": row["resource_type"],
        "resource_id": str(row["resource_id"]),
        "metadata": {**(row["metadata"] or {}), "outcome": row["outcome"]},
        "created_at": row["created_at"],
    }
