"""MIGRATION gate (P6-05; QUALITY_GATES §2.2, §3.1, ТЗ §14): upgrade → downgrade → upgrade на эфемерной
PostgreSQL + анализ деструктивных операций с обязательным approval `DESTRUCTIVE_MIGRATION`.

Обнаружение (по git-объектам commit, без исполнения кода репозитория):
* `alembic` — `alembic.ini` в корне (или каталог `*/versions/` рядом с `env.py`);
* `sql-updown` — пары `<имя>.up.sql`/`<имя>.down.sql` (golang-migrate) в каталоге миграций;
* `sql-forward` — только `*.sql` в каталоге `migrations/` без down-скриптов: необратимы — finding
  `migration/irreversible` (FAIL в strict/critical; в basic — finding medium и проверка только upgrade).
Конфигурация gate (`quality_gates.config.migrations`): `{"kind": "alembic|sql-updown|sql-forward",
"path": "<каталог>", "alembic_ini": "alembic.ini"}` — переопределяет обнаружение.

Исполнение: один контейнер образа `postgres` (ключ образа `postgres`, для Alembic — `migrations-python`:
postgres + python + alembic + зависимости проекта, собирается платформой), `network none`, пользователь
`postgres` (70:70), данные кластера и unix-сокет — в tmpfs `/tmp`. Платформенный драйвер `MIGRATION_DRIVER`
(фиксированный POSIX sh, sha256 в evidence) получает файлы миграций только позиционными аргументами,
исполняет каждый файл `psql -1 -v ON_ERROR_STOP=1` и сравнивает схемы (`pg_dump -s`, sha256):
после downgrade схема должна вернуться к исходной, повторный upgrade — дать ту же схему.

Деструктивные операции (DROP TABLE/COLUMN/SCHEMA, TRUNCATE, DELETE без WHERE, смена типа колонки —
возможное сужение) ищутся статически в upgrade-части изменённых миграций → findings `high` и
`Verdict.approval` (предмет approval DESTRUCTIVE_MIGRATION). Движок запрашивает approval через порт
(`ApprovalsPort`, структурно совпадает с `aicd_orchestration.approvals.Approvals`) и не даёт PASS,
пока approval не APPROVED; после утверждения находки сохраняются с severity `info` (риск принят
человеком в approval). Модуль orchestration не импортируется.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aicd_verification.findings import Finding, make_finding
from aicd_verification.gates import (
    Applicability,
    GateContext,
    GateProvider,
    GateSpec,
    RepoFacts,
    StackInfo,
    Verdict,
)

GATE_NAME = "migrations"
APPROVAL_ACTION = "DESTRUCTIVE_MIGRATION"
POSTGRES_USER = "70:70"  # пользователь postgres в официальном образе alpine
MigrationKind = Literal["alembic", "sql-updown", "sql-forward"]

MIGRATION_DRIVER = r"""set -u
mode=$1
shift
export PGDATA=/tmp/aicd-pgdata PGHOST=/tmp PGUSER=aicd PGDATABASE=aicd_verify
step() { echo "AICD_MIGRATION step=$1 status=$2 file=$3"; }
schema() { pg_dump -s -O -x -d "$PGDATABASE" | grep -v '^--' | grep -v '^SET ' | grep -v '^SELECT pg_catalog' | grep -v '^\\' | sha256sum | cut -d' ' -f1; }
initdb -A trust -U aicd -D "$PGDATA" >/dev/null 2>&1 || { echo "AICD_MIGRATION_RESULT fail initdb"; exit 3; }
pg_ctl -D "$PGDATA" -o "-c listen_addresses= -k /tmp -F" -w -l /tmp/aicd-pg.log start >/dev/null 2>&1 \
  || { echo "AICD_MIGRATION_RESULT fail pg_start"; exit 3; }
createdb aicd_verify || { echo "AICD_MIGRATION_RESULT fail createdb"; exit 3; }
base=$(schema)
fail=""
run_sql() { phase=$1; shift; for f in "$@"; do
  if psql -X -q -1 -v ON_ERROR_STOP=1 -f "$f" >/dev/null; then step "$phase" ok "$f"; else step "$phase" fail "$f"; fail="$phase"; return 1; fi
done; }
run_alembic() { phase=$1; target=$2; if alembic -c "$ini" "$phase" "$target"; then step "$phase" ok "$target"; else step "$phase" fail "$target"; fail="$phase"; return 1; fi; }
if [ "$mode" = sql ]; then
  n=$1
  shift
  ups=""
  downs=""
  i=0
  for f in "$@"; do
    i=$((i+1))
    if [ "$i" -le "$n" ]; then ups="$ups$f
"; else downs="$downs$f
"; fi
  done
  set -f
  IFS='
'
  run_sql upgrade1 $ups && up1=$(schema) && echo "AICD_SCHEMA phase=upgrade1 sha256=$up1"
  if [ -z "$fail" ] && [ -n "$downs" ]; then
    run_sql downgrade $downs && down=$(schema) && echo "AICD_SCHEMA phase=downgrade sha256=$down"
    [ -z "$fail" ] && run_sql upgrade2 $ups && up2=$(schema) && echo "AICD_SCHEMA phase=upgrade2 sha256=$up2"
  fi
else
  ini=$1
  export AICD_MIGRATION_DATABASE_URL="postgresql://aicd@/aicd_verify?host=/tmp" DATABASE_URL="postgresql://aicd@/aicd_verify?host=/tmp"
  # База сравнения — после `stamp base`: таблица версий Alembic переживает downgrade base и не является остатком миграций.
  if alembic -c "$ini" stamp base >/dev/null; then base=$(schema); else step stamp fail base; fail=stamp; fi
  [ -z "$fail" ] && run_alembic upgrade head && up1=$(schema) && echo "AICD_SCHEMA phase=upgrade1 sha256=$up1"
  [ -z "$fail" ] && run_alembic downgrade base && down=$(schema) && echo "AICD_SCHEMA phase=downgrade sha256=$down"
  [ -z "$fail" ] && run_alembic upgrade head && up2=$(schema) && echo "AICD_SCHEMA phase=upgrade2 sha256=$up2"
fi
echo "AICD_SCHEMA phase=base sha256=$base"
pg_ctl -D "$PGDATA" -m immediate -w stop >/dev/null 2>&1
if [ -n "$fail" ]; then echo "AICD_MIGRATION_RESULT fail $fail"; exit 1; fi
echo "AICD_MIGRATION_RESULT ok"
exit 0
"""
MIGRATION_DRIVER_SHA256 = hashlib.sha256(MIGRATION_DRIVER.encode()).hexdigest()

_DIR_NAMES = frozenset({"migrations", "migration", "migrate"})


def is_migration_path(path: str) -> bool:
    p = PurePosixPath(path)
    parts = p.parts[:-1]
    if p.suffix not in (".sql", ".py"):
        return False
    if any(part in _DIR_NAMES for part in parts):
        return True
    return len(parts) >= 2 and parts[-1] == "versions" and "alembic" in parts[-2]


# --- Обнаружение --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationLayout:
    kind: MigrationKind
    directory: str
    alembic_ini: str | None = None
    up_files: tuple[str, ...] = ()
    down_files: tuple[str, ...] = ()  # в порядке исполнения (обратном)

    def markers(self) -> tuple[str, ...]:
        return (self.kind, self.directory or ".", *((self.alembic_ini,) if self.alembic_ini else ()))


def _alembic_location(facts: RepoFacts) -> str | None:
    ini = facts.root_texts.get("alembic.ini")
    if ini is None:
        return None
    m = re.search(r"(?m)^\s*script_location\s*=\s*(\S+)", ini)
    loc = (m.group(1) if m else "alembic").replace("%(here)s/", "").strip("/")
    return loc or "alembic"


def detect_layout(facts: RepoFacts, config: Mapping[str, Any] | None = None) -> MigrationLayout | None:
    cfg = (config or {}).get("migrations")
    cfg = cfg if isinstance(cfg, Mapping) else {}
    forced_kind = cfg.get("kind")
    forced_path = str(cfg.get("path") or "").strip("/")
    if forced_kind in (None, "alembic"):
        loc = _alembic_location(facts)
        if loc is not None:
            ini = str(cfg.get("alembic_ini") or "alembic.ini")
            return MigrationLayout("alembic", forced_path or loc, alembic_ini=ini)
        if forced_kind == "alembic":
            return None
    sql = sorted(p for p in facts.files if p.endswith(".sql") and is_migration_path(p))
    if forced_path:
        sql = sorted(p for p in facts.files if p.endswith(".sql") and p.startswith(forced_path + "/"))
    if not sql:
        return None
    ups = [p for p in sql if p.endswith(".up.sql")]
    if ups and forced_kind in (None, "sql-updown"):
        directory = str(PurePosixPath(ups[0]).parent)
        ups = [p for p in ups if str(PurePosixPath(p).parent) == directory]
        downs = [p[: -len(".up.sql")] + ".down.sql" for p in ups]
        present = [d for d in downs if d in facts.files]
        if len(present) == len(ups):
            return MigrationLayout("sql-updown", directory, up_files=tuple(ups), down_files=tuple(reversed(downs)))
        return MigrationLayout("sql-forward", directory, up_files=tuple(ups))
    forward = [p for p in sql if not p.endswith(".down.sql")]
    if not forward:
        return None
    directory = str(PurePosixPath(forward[0]).parent)
    return MigrationLayout(
        "sql-forward", directory, up_files=tuple(p for p in forward if str(PurePosixPath(p).parent) == directory)
    )


def detect(facts: RepoFacts, stack: StackInfo) -> dict[str, Applicability]:
    layout = detect_layout(facts)
    if layout is None:
        return {
            GATE_NAME: Applicability(
                False, "no database migrations found", ("alembic.ini", "**/migrations/*.sql", "*.up.sql")
            )
        }
    return {GATE_NAME: Applicability(True, f"{layout.kind} migrations in {layout.directory}", layout.markers())}


# --- Деструктивные операции -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DestructiveOp:
    path: str
    line: int
    kind: str
    statement: str

    def subject(self) -> dict[str, str]:
        digest = hashlib.sha256(re.sub(r"\s+", " ", self.statement.lower()).encode()).hexdigest()
        return {"path": self.path, "kind": self.kind, "statement_sha256": digest}


_SQL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("drop_table", re.compile(r"\bDROP\s+TABLE\b", re.I)),
    (
        "drop_column",
        re.compile(r"\bALTER\s+TABLE\b[^;]*?\bDROP\s+(?:COLUMN\s+)?(?!CONSTRAINT|DEFAULT|NOT\b|INDEX)\w", re.I | re.S),
    ),
    ("drop_schema", re.compile(r"\bDROP\s+(?:SCHEMA|DATABASE)\b", re.I)),
    ("truncate", re.compile(r"\bTRUNCATE\b", re.I)),
    ("delete_all", re.compile(r"\bDELETE\s+FROM\s+[\w.\"]+\s*(?:;|$)", re.I)),
    (
        "type_change",
        re.compile(r"\bALTER\s+(?:TABLE\b[^;]*?\bALTER\s+)?COLUMN\b[^;]*?\b(?:SET\s+DATA\s+)?TYPE\b", re.I | re.S),
    ),
)
_ALEMBIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("drop_table", re.compile(r"\bop\.drop_table\s*\(")),
    ("drop_column", re.compile(r"\bop\.drop_column\s*\(")),
    ("drop_schema", re.compile(r"\bop\.execute\s*\(\s*[rRbBuU]?['\"]{1,3}\s*DROP\s+SCHEMA", re.I)),
    ("type_change", re.compile(r"\bop\.alter_column\s*\([^)]*\btype_\s*=", re.S)),
)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), sql, flags=re.S)
    return re.sub(r"--[^\n]*", "", sql)


def _upgrade_part(path: str, content: str) -> tuple[str, int]:
    """Текст upgrade-части и номер её первой строки (для down-файлов — пусто)."""
    if path.endswith(".down.sql"):
        return "", 1
    if path.endswith(".py"):
        m = re.search(r"(?m)^def upgrade\s*\(", content)
        if not m:
            return "", 1
        end = re.search(r"(?m)^def downgrade\s*\(", content[m.end() :])
        body = content[m.start() : m.end() + end.start()] if end else content[m.start() :]
        return body, content[: m.start()].count("\n") + 1
    m = re.search(r"(?mi)^--\s*migrate:down\b", content)  # dbmate: down-секция не анализируется
    return (content[: m.start()] if m else content), 1


def destructive_operations(changed_texts: Mapping[str, str]) -> list[DestructiveOp]:
    ops: list[DestructiveOp] = []
    for path, content in sorted(changed_texts.items()):
        if not is_migration_path(path):
            continue
        body, first_line = _upgrade_part(path, content)
        if not body:
            continue
        if path.endswith(".py"):
            patterns = _ALEMBIC_PATTERNS + _SQL_PATTERNS  # op.execute("DROP TABLE …") ловят SQL-паттерны
            scan = body
        else:
            patterns = _SQL_PATTERNS
            scan = _strip_sql_comments(body)
        seen: set[tuple[str, int]] = set()
        for kind, pattern in patterns:
            for m in pattern.finditer(scan):
                line = first_line + scan[: m.start()].count("\n")
                if (kind, line) in seen:
                    continue
                seen.add((kind, line))
                stmt_end = scan.find(";", m.start())
                statement = scan[m.start() : stmt_end if stmt_end != -1 else m.start() + 200][:300]
                ops.append(DestructiveOp(path, line, kind, statement.strip()))
    return ops


def approval_subject(ops: Sequence[DestructiveOp]) -> dict[str, Any]:
    """Предмет approval: сами операции (без SHA commit — утверждение переживает ретраи задачи)."""
    return {"kind": "destructive_migration", "operations": sorted((o.subject() for o in ops), key=json.dumps)}


def destructive_findings(ops: Sequence[DestructiveOp]) -> tuple[Finding, ...]:
    return tuple(
        make_finding(
            GATE_NAME,
            f"migration/destructive-{op.kind.replace('_', '-')}",
            "high",
            f"destructive migration operation ({op.kind}) requires approval {APPROVAL_ACTION}",
            file_path=op.path,
            line=op.line,
            snippet=op.subject()["statement_sha256"],
        )
        for op in ops
    )


# --- Исполнение и вердикт -----------------------------------------------------------------------------


def _layout(ctx: GateContext) -> MigrationLayout | None:
    return detect_layout(ctx.facts, ctx.config) if ctx.facts is not None else None


def _rel(path: str) -> str:
    return path if path.startswith(("./", "/")) else f"./{path}"


def build_argv(ctx: GateContext) -> tuple[str, ...]:
    layout = _layout(ctx)
    if layout is None:
        return ("sh", "-c", "echo 'AICD_MIGRATION_RESULT fail no-layout'; exit 3")
    if layout.kind == "alembic":
        return ("sh", "-c", MIGRATION_DRIVER, "aicd-migrate", "alembic", _rel(layout.alembic_ini or "alembic.ini"))
    files = [_rel(p) for p in layout.up_files]
    downs = [_rel(p) for p in layout.down_files] if layout.kind == "sql-updown" else []
    return ("sh", "-c", MIGRATION_DRIVER, "aicd-migrate", "sql", str(len(files)), *files, *downs)


def display_argv(ctx: GateContext) -> tuple[str, ...]:
    argv = list(build_argv(ctx))
    if len(argv) > 2 and argv[2] == MIGRATION_DRIVER:
        argv[2] = f"<aicd-migration-driver sha256:{MIGRATION_DRIVER_SHA256}>"
    return tuple(argv)


def probe(ctx: GateContext) -> tuple[str, ...]:
    layout = _layout(ctx)
    if layout is not None and layout.kind == "alembic":
        return ("alembic", "--version")
    return ("pg_ctl", "--version")


_STEP_RE = re.compile(r"^AICD_MIGRATION step=(?P<step>\w+) status=(?P<status>ok|fail) file=(?P<file>.*)$")
_SCHEMA_RE = re.compile(r"^AICD_SCHEMA phase=(?P<phase>\w+) sha256=(?P<sha>[0-9a-f]{64})$")
_RESULT_RE = re.compile(r"^AICD_MIGRATION_RESULT (?P<result>ok|fail)(?: (?P<where>\S+))?$")


def evaluate(code: int, output: str, ctx: GateContext) -> Verdict:
    layout = _layout(ctx)
    ops = destructive_operations(ctx.facts.changed_texts) if ctx.facts is not None else []
    findings = list(destructive_findings(ops))
    subject = approval_subject(ops) if ops else None
    lines = output.splitlines()
    result = next((m for m in (_RESULT_RE.match(x) for x in reversed(lines)) if m), None)
    schemas = {m["phase"]: m["sha"] for m in (_SCHEMA_RE.match(x) for x in lines) if m}
    failed_steps = [m for m in (_STEP_RE.match(x) for x in lines) if m and m["status"] == "fail"]
    if result is None:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"migration driver report missing (exit {code})", tuple(findings))
    if result["result"] == "fail":
        if code == 3 or result["where"] in ("initdb", "pg_start", "createdb", "no-layout"):
            return Verdict("ERROR", "INFRA", f"ephemeral PostgreSQL failed ({result['where']})", tuple(findings))
        step = failed_steps[-1] if failed_steps else None
        where = f"{step['step']} {step['file']}" if step else str(result["where"])
        findings.append(
            make_finding(
                GATE_NAME,
                f"migration/{(step['step'] if step else 'apply')}-failed",
                "high",
                f"migration step failed: {where}",
                file_path=step["file"] if step else None,
            )
        )
        return Verdict("FAIL", message=f"migration failed at {where}", findings=tuple(findings))
    if code != 0:
        return Verdict("ERROR", "REPORT_UNPARSABLE", f"exit {code} contradicts driver result ok", tuple(findings))
    if "upgrade1" not in schemas or "base" not in schemas:
        return Verdict("ERROR", "REPORT_UNPARSABLE", "schema snapshots missing", tuple(findings))
    reversible = layout is not None and layout.kind != "sql-forward"
    message = "upgrade→downgrade→upgrade ok" if reversible else "upgrade ok (forward-only migrations)"
    status: Literal["PASS", "FAIL"] = "PASS"
    if reversible:
        if "downgrade" not in schemas or "upgrade2" not in schemas:
            return Verdict("ERROR", "REPORT_UNPARSABLE", "downgrade/upgrade snapshots missing", tuple(findings))
        if schemas["downgrade"] != schemas["base"]:
            findings.append(
                make_finding(
                    GATE_NAME, "migration/downgrade-incomplete", "high", "downgrade does not restore the schema"
                )
            )
            status, message = "FAIL", "downgrade leaves schema changes behind"
        elif schemas["upgrade2"] != schemas["upgrade1"]:
            findings.append(
                make_finding(GATE_NAME, "migration/schema-drift", "high", "re-upgrade produces a different schema")
            )
            status, message = "FAIL", "schema differs after downgrade+upgrade"
    else:
        severity = "medium" if ctx.profile == "basic" else "high"
        findings.append(make_finding(GATE_NAME, "migration/irreversible", severity, "migrations have no down scripts"))
        if ctx.profile != "basic":
            status, message = "FAIL", "forward-only migrations cannot be rolled back"
    return Verdict(status, None, message, tuple(findings), approval=subject)


SPEC = GateSpec(
    GATE_NAME,
    "MIGRATION",
    "postgres",
    ("pg_ctl", "--version"),
    "command_result",
    "report",
    build_argv,
    display_argv,
    lambda code, _out: Verdict("ERROR", "INFRA", f"no interpreter (exit {code})"),
    evaluate=evaluate,
    probe_for=probe,
    container_user=POSTGRES_USER,
)
PROVIDER = GateProvider(
    "migrations",
    (SPEC,),
    detect,
    (
        (
            GATE_NAME,
            "MIGRATION",
            1200,
            "Миграции: upgrade→downgrade→upgrade на эфемерной PostgreSQL; деструктивные — approval",
        ),
    ),
)


# --- Порт approvals (структурно совпадает с aicd_orchestration.approvals.Approvals) -------------------


class ApprovalsPort(Protocol):
    async def request(
        self,
        conn: AsyncConnection,
        *,
        workspace_id: uuid.UUID,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        task_id: uuid.UUID | None,
        action_type: str,
        risk_level: str,
        subject: dict[str, Any],
        requested_by_type: str,
        requested_by: uuid.UUID | None = None,
        requested_by_role: str | None = None,
    ) -> uuid.UUID: ...


def subject_hash(subject: Mapping[str, Any]) -> str:
    """sha256 канонического JSON — тот же алгоритм, что `aicd_orchestration.approvals.subject_hash`."""
    canonical = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ApprovalState:
    status: str  # APPROVED | REJECTED | PENDING | NONE
    approval_id: uuid.UUID | None = None


async def approval_state(
    conn: AsyncConnection, *, project_id: uuid.UUID, action_type: str, subject: Mapping[str, Any]
) -> ApprovalState:
    """Последнее решение по предмету в проекте (RLS ограничивает workspace)."""
    row = (
        (
            await conn.execute(
                text(
                    "SELECT id, status FROM approvals WHERE project_id = :p AND action_type = :a AND subject_hash = :h"
                    " AND status IN ('APPROVED','REJECTED','PENDING')"
                    " ORDER BY (status = 'APPROVED') DESC, requested_at DESC LIMIT 1"
                ),
                {"p": project_id, "a": action_type, "h": subject_hash(subject)},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return ApprovalState("NONE")
    return ApprovalState(str(row["status"]), row["id"])
