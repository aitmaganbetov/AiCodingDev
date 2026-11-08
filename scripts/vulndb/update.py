"""Одобренное и аудируемое обновление офлайн-БД уязвимостей (OQ-43 (f), OQ-51 (b), QUALITY_GATES §4.1, security_gates).

    # 1) запрос approval платформы VULN_DB_UPDATE от имени инициатора (член workspace)
    uv run python -m scripts.vulndb.update request --workspace-id W --project-id P --requested-by you@corp \
        --osv-ecosystems PyPI,npm --trivy --pg-container aicodingdev-postgres-1
    # 2) другой участник (admin+, step-up) утверждает его в UI «Approvals» (four-eyes, срок 24 ч)
    # 3) обновление по утверждённому approval (однократно, предмет должен совпасть с запросом)
    uv run python -m scripts.vulndb.update apply --approval-id A --db-root /var/lib/aicd/vulndb \
        --osv-ecosystems PyPI,npm --trivy --pg-container aicodingdev-postgres-1 [--ca-bundle /path/ca.pem]

Gates `trivy-vuln` и `osv-scanner-sca` работают без сети и читают БД из `/aicd-db` (только чтение); без свежей
БД — ERROR(DB_UNAVAILABLE), не PASS. Этот шаг — единственный, которому нужен egress, и только к:
`osv-vulnerabilities.storage.googleapis.com` (OSV) и реестру БД Trivy (`mirror.gcr.io`, `--trivy-db-repository`).

Порядок `apply`: `sys_vulndb_authorize` (APPROVED, не истёк, предмет совпал, не использован; аудит платформы
`vulndb.update_authorized`) → загрузка в staging → проверки (md5 GCS, целостность zip и JSON-записей, лимиты размера;
формат БД Trivy и свежесть через `aicd_verification.security_gates.*_db_status`) → атомарная замена
`<db-root>/<osv|trivy>` (предыдущая версия — `<db-root>/.previous/<вид>`) → итог в аудит платформы
(`vulndb.update_finished`) и локальный журнал `<db-root>/audit.jsonl`. Офлайн-обхода (файла одобрения) нет:
без платформы gates всё равно не работают, а одобрение должно быть проверяемым (four-eyes, step-up, аудит).
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from aicd_verification.security_gates import OSV_MANIFEST_FILE, osv_db_status, trivy_db_status
from aicd_verification.tools.images import TRIVY
from scripts.backup.docker import PgTarget
from scripts.vulndb.approval import PlatformDb, authorize, build_subject, report, request

OSV_BASE = "https://osv-vulnerabilities.storage.googleapis.com"
OSV_ECOSYSTEMS = frozenset({"PyPI", "npm", "Go", "Packagist", "crates.io", "Maven", "NuGet", "RubyGems", "Hex", "Pub"})
TRIVY_DB_REPOSITORY = "mirror.gcr.io/aquasec/trivy-db:2"
MAX_ZIP_BYTES = 1024 * 1024 * 1024
MAX_ENTRIES = 2_000_000


class UpdateError(RuntimeError):
    pass


def now() -> datetime:
    return datetime.now(UTC)


def _opener(ca_bundle: Path | None) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context(cafile=str(ca_bundle) if ca_bundle else None)
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), urllib.request.ProxyHandler())


def download_osv(ecosystem: str, target: Path, ca_bundle: Path | None) -> dict[str, Any]:
    if ecosystem not in OSV_ECOSYSTEMS:
        raise UpdateError(f"неизвестная экосистема OSV {ecosystem!r}")
    url = f"{OSV_BASE}/{ecosystem}/all.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    md5 = hashlib.md5()  # noqa: S324 — сверка с x-goog-hash сервера (целостность передачи), не безопасность
    sha = hashlib.sha256()
    size = 0
    with _opener(ca_bundle).open(url, timeout=120) as resp, target.open("wb") as out:
        expected_md5 = None
        for header in resp.headers.get_all("x-goog-hash") or []:
            for part in header.split(","):
                key, _, value = part.strip().partition("=")
                if key == "md5":
                    expected_md5 = base64.b64decode(value + "==="[: (4 - len(value) % 4) % 4]).hex()
        while chunk := resp.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                raise UpdateError(f"{ecosystem}: архив больше лимита {MAX_ZIP_BYTES} байт")
            md5.update(chunk)
            sha.update(chunk)
            out.write(chunk)
    if expected_md5 and md5.hexdigest() != expected_md5:
        raise UpdateError(f"{ecosystem}: md5 не совпадает с x-goog-hash (повреждение при передаче)")
    entries = _validate_zip(target)
    return {"ecosystem": ecosystem, "url": url, "bytes": size, "sha256": sha.hexdigest(),
            "server_md5_verified": bool(expected_md5), "entries": entries}  # fmt: skip


def _validate_zip(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_ENTRIES:
            raise UpdateError(f"{path.name}: пустой архив или слишком много записей")
        for info in infos:
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts or not re.fullmatch(r"[A-Za-z0-9._:+-]+\.json", name):
                raise UpdateError(f"{path.name}: недопустимое имя записи {name!r}")
        bad = zf.testzip()
        if bad is not None:
            raise UpdateError(f"{path.name}: повреждена запись {bad}")
        sample = json.loads(zf.read(infos[0]))
        if not isinstance(sample, dict) or "id" not in sample:
            raise UpdateError(f"{path.name}: записи не в формате OSV")
    return len(infos)


def update_osv(
    staging: Path, ecosystems: list[str], ca_bundle: Path | None, approval: dict[str, Any]
) -> dict[str, Any]:
    sources = [download_osv(eco, staging / "osv-scalibr" / eco / "all.zip", ca_bundle) for eco in ecosystems]
    manifest = {"updated_at": now().isoformat().replace("+00:00", "Z"), "ecosystems": ecosystems,
                "sources": sources, "approval": approval}  # fmt: skip
    (staging / OSV_MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    status = osv_db_status(staging, now=now())
    if not status.ready:
        raise UpdateError(f"OSV DB после загрузки не готова: {status.reason}")
    return manifest


def update_trivy(staging: Path, repository: str, ca_bundle: Path | None, seed: Path | None) -> dict[str, Any]:
    """`trivy image --download-db-only` в закреплённом образе; сеть контейнера — только для этого шага."""
    staging.mkdir(parents=True)
    if seed is not None and (seed / "db").is_dir():  # инкремент: trivy не скачивает свежую БД повторно
        shutil.copytree(seed / "db", staging / "db")
    staging.chmod(0o777)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    ]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        cmd += ["-e", f"HTTPS_PROXY={proxy}"]
    if ca_bundle:
        cmd += ["-e", "SSL_CERT_FILE=/aicd-ca.pem", "-v", f"{ca_bundle}:/aicd-ca.pem:ro"]
    cmd += ["-v", f"{staging}:/db", TRIVY.ref, "image", "--download-db-only", "--cache-dir", "/db"]
    cmd += ["--db-repository", repository, "--quiet"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)  # noqa: S603
    if proc.returncode != 0:
        raise UpdateError(f"trivy --download-db-only → {proc.returncode}: {proc.stderr[-800:]}")
    status = trivy_db_status(staging, now=now())
    if not status.ready:
        raise UpdateError(f"Trivy DB после загрузки не готова: {status.reason}")
    db = staging / "db" / "trivy.db"
    return {"repository": repository, "image": TRIVY.ref, "bytes": db.stat().st_size,
            "sha256": _sha256(db), **status.as_json()}  # fmt: skip


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def swap(db_root: Path, kind: str, staging: Path) -> None:
    """Атомарная замена `<db_root>/<kind>`; предыдущая версия — `<db_root>/.previous/<kind>` (откат)."""
    current = db_root / kind
    previous = db_root / ".previous" / kind
    previous.parent.mkdir(parents=True, exist_ok=True)
    if previous.exists():
        shutil.rmtree(previous)
    if current.exists():
        current.rename(previous)
    staging.rename(current)
    for path in [current, *current.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)  # gate-контейнеры читают БД (ro) от непривилегированного uid


def audit(db_root: Path, record: dict[str, Any]) -> None:
    with (db_root / "audit.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _db(args: argparse.Namespace) -> PlatformDb:
    if args.pg_container:
        return PlatformDb(target=PgTarget(host="", user=args.pg_user, container=args.pg_container))
    if args.maintenance_dsn_file:
        return PlatformDb(dsn=args.maintenance_dsn_file.read_text(encoding="utf-8").strip())
    raise SystemExit("укажите --pg-container или --maintenance-dsn-file (роль aicd_maintenance)")


def _subject(args: argparse.Namespace) -> tuple[list[str], dict[str, Any]]:
    ecosystems = [e.strip() for e in args.osv_ecosystems.split(",") if e.strip()]
    unknown = sorted(set(ecosystems) - OSV_ECOSYSTEMS)
    if unknown:
        raise SystemExit(f"неизвестные экосистемы OSV: {unknown}")
    if not ecosystems and not args.trivy:
        raise SystemExit("укажите --osv-ecosystems и/или --trivy")
    return ecosystems, build_subject(ecosystems, args.trivy_db_repository if args.trivy else None)


def cmd_request(args: argparse.Namespace) -> int:
    _, subject = _subject(args)
    approval_id = request(_db(args), workspace_id=args.workspace_id, project_id=args.project_id,
                          requested_by_email=args.requested_by, subject=subject)  # fmt: skip
    print(json.dumps({"ok": True, "approval_id": str(approval_id), "subject": subject,
                      "next": "утвердить в UI Approvals другим участником (admin+), затем `apply --approval-id`"},
                     ensure_ascii=False, indent=2))  # fmt: skip
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    ecosystems, subject = _subject(args)
    db = _db(args)
    db_root = args.db_root.resolve()
    db_root.mkdir(parents=True, exist_ok=True)
    operator = f"{getpass.getuser()}@{socket.gethostname()}"
    record: dict[str, Any] = {"at": now().isoformat(), "operator": operator, "kinds": subject["kinds"],
                              "approval_id": str(args.approval_id)}  # fmt: skip
    try:
        granted = authorize(db, args.approval_id, subject, operator)
    except (psycopg.Error, RuntimeError) as exc:  # DockerError — RuntimeError
        record |= {"result": "denied", "error": str(exc)[:500]}
        audit(db_root, record)
        print(json.dumps({"ok": False, "error": f"approval не действует: {exc}"}, ensure_ascii=False))
        return 1
    approval = {"approval_id": str(args.approval_id), **granted}
    record["approval"] = approval
    results: dict[str, Any] = {}
    try:
        if ecosystems:
            staging = db_root / f".staging-osv-{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            manifest = update_osv(staging, ecosystems, args.ca_bundle, approval)
            swap(db_root, "osv", staging)
            results["osv"] = {"ecosystems": ecosystems, "sources": manifest["sources"]}
        if args.trivy:
            staging = db_root / f".staging-trivy-{os.getpid()}"
            shutil.rmtree(staging, ignore_errors=True)
            results["trivy"] = update_trivy(staging, args.trivy_db_repository, args.ca_bundle, db_root / "trivy")
            swap(db_root, "trivy", staging)
    except (UpdateError, OSError, ValueError, zipfile.BadZipFile) as exc:
        record |= {"result": "failure", "error": str(exc)[:500]}
        audit(db_root, record)
        report(db, args.approval_id, "failure", {"error": str(exc)[:500], "operator": operator})
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    record |= {"result": "success", **results}
    audit(db_root, record)
    summary = {k: {"sha256": v.get("sha256")} if k == "trivy" else {"sha256": [s["sha256"] for s in v["sources"]]}
               for k, v in results.items()}  # fmt: skip
    report(db, args.approval_id, "success", {"operator": operator, **summary})
    print(json.dumps({"ok": True, "approval": approval, **results}, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("request", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--osv-ecosystems", default="", help="через запятую: PyPI,npm,Go,Packagist,…")
        p.add_argument("--trivy", action="store_true", help="обновить БД Trivy")
        p.add_argument("--trivy-db-repository", default=TRIVY_DB_REPOSITORY)
        p.add_argument("--pg-container", help="psql в контейнере сервера (compose: aicodingdev-postgres-1)")
        p.add_argument("--pg-user", default="aicd_maintenance")
        p.add_argument("--maintenance-dsn-file", type=Path, help="или файл с DSN роли aicd_maintenance")
        if name == "request":
            p.add_argument("--workspace-id", type=uuid.UUID, required=True)
            p.add_argument("--project-id", type=uuid.UUID, required=True, help="проект, к которому относится approval")
            p.add_argument("--requested-by", required=True, help="e-mail инициатора (член workspace)")
            p.set_defaults(handler=cmd_request)
        else:
            p.add_argument("--approval-id", type=uuid.UUID, required=True)
            p.add_argument("--db-root", type=Path, required=True, help="каталог БД (в gates — /aicd-db, только чтение)")
            p.add_argument("--ca-bundle", type=Path, help="CA egress-/корпоративного прокси")
            p.set_defaults(handler=cmd_apply)
    args = parser.parse_args(argv)
    result: int = args.handler(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
