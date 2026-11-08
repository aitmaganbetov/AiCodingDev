"""Отдельный процесс-воркер для chaos-тестов: продвигает run как настоящий воркер, тест убивает его SIGKILL.

`python -m tests.support.chaos_worker <json>`; JSON: database_url, tmp, repository_roots, scenario,
workspace_id, run_id. Процесс CLI запускается в своей группе (start_new_session) и переживает гибель воркера —
это именно та ситуация, которую разбирает recovery reconciler.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from aicd_platform.db import create_engine
from tests.support.phase3 import build_orchestrator


async def main(config: dict[str, str]) -> None:
    engine = create_engine(config["database_url"])
    roots = {k: Path(v) for k, v in json.loads(config["repository_roots"]).items()}
    orch = build_orchestrator(engine, Path(config["tmp"]), roots, config["scenario"])
    orch.lease_ttl_seconds = int(config.get("lease_ttl_seconds", "120"))
    run_id, ws = uuid.UUID(config["run_id"]), uuid.UUID(config["workspace_id"])
    await orch.on_run_created(run_id, ws)
    await orch.drive(run_id, ws)


if __name__ == "__main__":
    asyncio.run(main(json.loads(sys.argv[1])))
