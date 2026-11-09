"""Отдельный процесс GitHub-воркера для chaos-тестов P9-03: тест убивает его SIGKILL посреди внешнего действия.

`python -m tests.support.chaos_github_worker <json>`; JSON: database_url, api_url, app_id, key_file,
repository_roots, workspace_id, op (`pr` | `merge`), job (для `pr`) или approval_id (для `merge`).
Workflow собирается так же, как на воркере orchestrator (`GitHubWorkflow` с настоящим `GitHubClient`).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from aicd_integrations_github.api import AppJwtSigner, GitHubClient
from aicd_orchestration.github_flow import GitHubWorkflow
from aicd_platform.db import create_engine


async def main(config: dict[str, str]) -> None:
    engine = create_engine(config["database_url"])
    pem = await asyncio.to_thread(Path(config["key_file"]).read_text)
    client = GitHubClient(api_url=config["api_url"], signer=AppJwtSigner(int(config["app_id"]), pem))
    roots = {k: Path(v) for k, v in json.loads(config["repository_roots"]).items()}
    workflow = GitHubWorkflow(engine, client, repository_roots=roots, git_url=config["api_url"])
    ws = uuid.UUID(config["workspace_id"])
    try:
        if config["op"] == "pr":
            print(await workflow.open_pull_request(ws, json.loads(config["job"])), flush=True)
        else:
            print(await workflow.merge(ws, uuid.UUID(config["approval_id"])), flush=True)
    finally:
        await client.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(json.loads(sys.argv[1])))
