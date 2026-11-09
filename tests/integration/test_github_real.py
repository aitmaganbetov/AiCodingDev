"""P8-02 (opt-in): интеграция с настоящим GitHub на тестовой организации/установке.

Запускается только при заданных переменных окружения (иначе SKIPPED — как P2-01 с реальными CLI):

* `AICD_TEST_GITHUB_APP_ID` — id GitHub App;
* `AICD_TEST_GITHUB_APP_KEY_FILE` — путь к PEM-ключу App (файл секрета, не переменная);
* `AICD_TEST_GITHUB_INSTALLATION_ID` — установка App в тестовой организации;
* `AICD_TEST_GITHUB_REPO` — `owner/name` тестового репозитория, доступного установке.

Проверяет только чтение: JWT App → установка (права ровно минимальные, SC-GH-04) → installation token на один
репозиторий с правами только на чтение → метаданные репозитория и ветка по умолчанию. Ничего не пишет в GitHub.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aicd_integrations_github.api import AppJwtSigner, GitHubClient, RepoRef, permissions

REQUIRED = (
    "AICD_TEST_GITHUB_APP_ID",
    "AICD_TEST_GITHUB_APP_KEY_FILE",
    "AICD_TEST_GITHUB_INSTALLATION_ID",
    "AICD_TEST_GITHUB_REPO",
)

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in REQUIRED),
    reason="реальный GitHub: задайте " + ", ".join(REQUIRED) + " (opt-in, P8-02)",
)


def _key() -> str:
    return Path(os.environ["AICD_TEST_GITHUB_APP_KEY_FILE"]).read_text()


async def test_real_github_app_read_only_flow() -> None:
    key = _key()
    client = GitHubClient(
        api_url=os.environ.get("AICD_TEST_GITHUB_API_URL", "https://api.github.com"),
        signer=AppJwtSigner(int(os.environ["AICD_TEST_GITHUB_APP_ID"]), key),
        proxy_url=os.environ.get("AICD_EGRESS_PROXY_URL") or None,
    )
    try:
        installation = await client.installation(int(os.environ["AICD_TEST_GITHUB_INSTALLATION_ID"]))
        assert permissions.excess_permissions(installation.permissions) == [], "у установки лишние права (SC-GH-04)"
        assert permissions.missing_permissions(installation.permissions) == []
        repo = RepoRef.parse(os.environ["AICD_TEST_GITHUB_REPO"])
        token = await client.installation_token(installation.installation_id, "read", repositories=[repo.name])
        assert all(level == "read" for level in token.permissions.values())
        info = await client.repository(token, repo)
        assert info.full_name.lower() == repo.full_name.lower()
        branch = await client.branch(token, repo, info.default_branch)
        assert branch is not None and len(branch.sha) == 40
    finally:
        await client.aclose()
