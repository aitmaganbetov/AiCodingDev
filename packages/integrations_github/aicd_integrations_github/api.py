"""Публичный интерфейс integrations_github (MODULE_BOUNDARIES §2: другие модули импортируют только `api`)."""

from __future__ import annotations

from aicd_integrations_github import install_state, permissions, store, webhooks
from aicd_integrations_github.app_auth import AppJwtSigner, AppKeyError
from aicd_integrations_github.client import (
    BranchInfo,
    GitHubClient,
    GitHubError,
    GitHubNotConfiguredError,
    Installation,
    InstallationToken,
    PullInfo,
    RepoInfo,
    RepoRef,
    parse_remote_url,
)
from aicd_platform.config import Settings

__all__ = [
    "AppJwtSigner",
    "AppKeyError",
    "BranchInfo",
    "GitHubClient",
    "GitHubError",
    "GitHubNotConfiguredError",
    "Installation",
    "InstallationToken",
    "PullInfo",
    "RepoInfo",
    "RepoRef",
    "build_client",
    "install_state",
    "parse_remote_url",
    "permissions",
    "store",
    "webhooks",
]


def build_client(settings: Settings) -> GitHubClient:
    """Клиент GitHub из настроек. Без app_id/ключа — клиент без подписи (`configured=False`): операции App
    завершаются `GitHubNotConfiguredError`, а API отвечает 503 — интеграция GitHub опциональна (FR-GH-01)."""
    signer: AppJwtSigner | None = None
    key = settings.github_private_key_pem()
    if settings.github_app_id is not None and key is not None:
        signer = AppJwtSigner(settings.github_app_id, key.get_secret_value())
    return GitHubClient(api_url=settings.github_api_url, signer=signer, proxy_url=settings.egress_proxy_url)
