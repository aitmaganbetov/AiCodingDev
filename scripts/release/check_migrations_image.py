"""Проверка образа `migrations-python` (OQ-43 (a)) штатным драйвером MIGRATION gate на эталонном Alembic-проекте.

    uv run python -m scripts.release.check_migrations_image aicd/migrations-python:17-3.12

Контейнер — как у gate: `network none`, пользователь 70:70, read-only rootfs, tmpfs /tmp, проект — только чтение.
Ожидается: upgrade → downgrade → upgrade успешны, схема после downgrade = исходной, повторный upgrade = первому.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from aicd_verification.migrations import MIGRATION_DRIVER

ALEMBIC_INI = """[alembic]
script_location = alembic
"""
ENV_PY = """import os
from alembic import context
from sqlalchemy import create_engine
url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
with create_engine(url).connect() as conn:
    context.configure(connection=conn, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
"""
VERSION = '''"""create items"""
from alembic import op
revision = "0001"
down_revision = None
def upgrade():
    op.execute("CREATE TABLE items (id bigint PRIMARY KEY, name text NOT NULL)")
    op.execute("CREATE INDEX ix_items_name ON items (name)")
def downgrade():
    op.execute("DROP TABLE items")
'''


def check(image: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="aicd-migimg-") as tmp:
        root = Path(tmp)
        (root / "alembic" / "versions").mkdir(parents=True)
        (root / "alembic.ini").write_text(ALEMBIC_INI)
        (root / "alembic" / "env.py").write_text(ENV_PY)
        (root / "alembic" / "script.py.mako").write_text("")
        (root / "alembic" / "versions" / "0001_items.py").write_text(VERSION)
        for path in [root, *root.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o644)
        cmd = ["docker", "run", "--rm", "--network", "none", "--user", "70:70", "--read-only", "--tmpfs", "/tmp",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-v", f"{root}:/work:ro", "-w", "/work", image,
               "sh", "-c", MIGRATION_DRIVER, "aicd-migrate", "alembic", "alembic.ini"]  # fmt: skip
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)  # noqa: S603
    out = proc.stdout + proc.stderr
    schemas = dict(re.findall(r"AICD_SCHEMA phase=(\w+) sha256=([0-9a-f]+)", out))
    ok = (
        proc.returncode == 0
        and "AICD_MIGRATION_RESULT ok" in out
        and schemas.get("downgrade") == schemas.get("base")
        and schemas.get("upgrade1") == schemas.get("upgrade2")
        and schemas.get("upgrade1") != schemas.get("base")
    )
    return ok, out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: python -m scripts.release.check_migrations_image <image>", file=sys.stderr)
        return 2
    ok, out = check(args[0])
    print(out.strip())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
