"""`uv run python -m scripts.backup <команда>` — бэкап и восстановление (docs/runbooks/backups.md).

Общие параметры: `--repo` (AICD_BACKUP_REPO) — каталог репозитория бэкапов; `--key-file`
(AICD_BACKUP_KEY_FILE) — ключ (base64, 0600, хранится отдельно от репозитория); `--status-file`
(AICD_BACKUP_STATUS_FILE) — дополнительный файл статуса для метрик `aicd_backup_*`.
Вывод — JSON; код возврата 0 — успех, 1 — сбой (статус компонента = failure → алерт AicdBackupFailed).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.backup import artifacts as art
from scripts.backup import pg
from scripts.backup.crypto import BackupCryptoError, BackupKey, generate_key_file
from scripts.backup.docker import POSTGRES_IMAGE, DockerError, PgTarget
from scripts.backup.repo import Repository, RepositoryError
from scripts.backup.secrets_bundle import backup_secrets, restore_secrets


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", type=Path, default=_env_path("AICD_BACKUP_REPO"))
    p.add_argument("--key-file", type=Path, default=_env_path("AICD_BACKUP_KEY_FILE"))
    p.add_argument("--status-file", type=Path, default=_env_path("AICD_BACKUP_STATUS_FILE"))


def _add_pg(p: argparse.ArgumentParser, default_user: str = "aicd_admin") -> None:
    p.add_argument("--pg-container", help="docker exec в контейнер сервера (unix-сокет); нужен для pg-base")
    p.add_argument("--pg-host", default="postgres", help="хост PostgreSQL из сети --pg-network (compose: postgres)")
    p.add_argument("--pg-port", type=int, default=5432)
    p.add_argument("--pg-user", default=default_user)
    p.add_argument("--pg-password-file", type=Path, help="пароль роли (сетевой режим)")
    p.add_argument("--pg-db", default="aicd")
    p.add_argument("--pg-network", default=None, help="сеть Docker (compose: aicodingdev_backend)")
    p.add_argument("--pg-image", default=POSTGRES_IMAGE)


def _add_store(p: argparse.ArgumentParser) -> None:
    p.add_argument("--s3-endpoint")
    p.add_argument("--s3-bucket", default="aicd-artifacts")
    p.add_argument("--s3-access-key-file", type=Path)
    p.add_argument("--s3-secret-key-file", type=Path)
    p.add_argument("--fs-root", type=Path, help="корень FS-хранилища (AICD_ARTIFACT_FS_ROOT, том artifacts)")


def _add_rows_source(p: argparse.ArgumentParser) -> None:
    p.add_argument("--maintenance-dsn-file", type=Path, help="файл с DSN роли aicd_maintenance")
    p.add_argument("--pg-container", help="или: psql в контейнере сервера (compose: aicodingdev-postgres-1)")
    p.add_argument("--pg-user", default="aicd_maintenance")


def _target(args: argparse.Namespace) -> PgTarget:
    return PgTarget(
        host=args.pg_host,
        port=args.pg_port,
        user=args.pg_user,
        password_file=args.pg_password_file,
        dbname=args.pg_db,
        network=args.pg_network,
        image=args.pg_image,
        container=args.pg_container,
    )


def _store(args: argparse.Namespace) -> art.ArtifactSource:
    if args.s3_endpoint:
        if not (args.s3_access_key_file and args.s3_secret_key_file):
            raise SystemExit("для S3 нужны --s3-access-key-file и --s3-secret-key-file")
        return art.S3Source.from_files(
            args.s3_endpoint, args.s3_bucket, args.s3_access_key_file, args.s3_secret_key_file
        )
    if args.fs_root:
        return art.FsSource(args.fs_root)
    raise SystemExit("укажите --s3-endpoint … или --fs-root")


def _rows(args: argparse.Namespace) -> art.RowsLoader:
    """Строки `artifacts`: DSN роли aicd_maintenance (файл) или psql в контейнере сервера (`--pg-container`)."""
    if getattr(args, "pg_container", None):
        return art.rows_loader(target=PgTarget(host="", user=args.pg_user, container=args.pg_container))
    if not args.maintenance_dsn_file:
        raise SystemExit("укажите --maintenance-dsn-file (DSN роли aicd_maintenance) или --pg-container")
    return art.rows_loader(dsn=str(Path(args.maintenance_dsn_file).read_text(encoding="utf-8").strip()))


def _repo(args: argparse.Namespace, *, create: bool = False) -> Repository:
    if args.repo is None or args.key_file is None:
        raise SystemExit("укажите --repo и --key-file (или AICD_BACKUP_REPO / AICD_BACKUP_KEY_FILE)")
    return Repository.open(args.repo, BackupKey.load(args.key_file), create=create)


Handler = Callable[[argparse.Namespace], dict[str, Any]]
COMPONENT_OF = {
    "pg-base": "postgres_base",
    "pg-wal-sync": "postgres_wal",
    "pg-dump": "postgres_dump",
    "artifacts": "artifacts",
    "secrets": "secrets",
}


def cmd_keygen(args: argparse.Namespace) -> dict[str, Any]:
    key = generate_key_file(args.out)
    return {"key_file": str(args.out), "key_id": key.key_id_hex}


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args, create=True)
    return {"repo": str(repo.root), "key_id": repo.key.key_id_hex}


def cmd_pg_base(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    with repo.lock():
        return pg.base_backup(repo, _target(args))


def cmd_wal_sync(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    with repo.lock():
        return pg.wal_sync(repo, args.wal_volume, image=args.pg_image, delete_synced=args.delete_synced)


def cmd_pg_dump(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    with repo.lock():
        return pg.logical_dump(repo, _target(args))


def cmd_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    with repo.lock():
        return art.backup_artifacts(repo, _store(args), _rows(args), full=args.full)


def cmd_secrets(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    with repo.lock():
        return backup_secrets(repo, args.dir)


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    wal: dict[str, Any] = {}
    for sysdir in sorted(p for p in (repo.root / "wal").glob("*") if p.is_dir()):
        names = pg.wal_names(repo, sysdir.name)
        wal[sysdir.name] = {
            "files": len(names),
            "first": names[0] if names else None,
            "last": names[-1] if names else None,
        }
    status_path = repo.root / "status.json"
    return {
        "key_id": repo.key.key_id_hex,
        **{kind: repo.list_ids(kind) for kind in ("base", "dump", "artifacts", "secrets")},
        "wal": wal,
        "status": json.loads(status_path.read_text()) if status_path.exists() else None,
    }


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    results: list[dict[str, Any]] = []
    kinds = [args.kind] if args.kind else ["base", "dump", "secrets"]
    for kind in kinds:
        ids = [repo.resolve_id(kind, args.backup_id)] if args.backup_id else repo.list_ids(kind)
        results.extend(pg.verify_set(repo, kind, backup_id) for backup_id in ids)
    out: dict[str, Any] = {"verified": results}
    if args.wal:
        base_ids = repo.list_ids("base")
        if not base_ids:
            raise pg.BackupError("нет базового бэкапа: нечем проверять цепочку WAL")
        oldest = repo.read_manifest("base", base_ids[0])
        out["wal"] = pg.verify_wal(repo, oldest["system_identifier"], since_segment=oldest["start_wal_segment"])
        if out["wal"]["gaps"]:
            raise pg.BackupError(f"разрывы в архиве WAL: {out['wal']['gaps']}")
    return out


def cmd_restore_pg(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    target_time = datetime.fromisoformat(args.target_time) if args.target_time else None
    return pg.restore_base(
        repo,
        args.backup_id,
        data_volume=args.data_volume,
        wal_volume=args.wal_volume,
        target_time=target_time,
        image=args.pg_image,
    )


def cmd_post_restore(args: argparse.Namespace) -> dict[str, Any]:
    return pg.post_restore(_target(args))


def cmd_restore_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    sink = _store(args)
    if isinstance(sink, art.S3Source):
        sink.ensure_bucket()
    return art.restore_artifacts(repo, args.backup_id, sink)


def cmd_verify_artifacts(args: argparse.Namespace) -> dict[str, Any]:
    keys = None
    if args.snapshot:
        keys = art.snapshot_keys(_repo(args), args.snapshot)
    report = art.verify_storage(_store(args), _rows(args), snapshot_keys=keys)
    if report["missing"] or report["mismatched"]:
        raise art.BackupIntegrityError("хранилище не совпадает с artifacts.sha256", report)
    return report


def cmd_restore_secrets(args: argparse.Namespace) -> dict[str, Any]:
    return restore_secrets(_repo(args), args.backup_id, args.to)


def cmd_prune(args: argparse.Namespace) -> dict[str, Any]:
    """Оставить N последних наборов каждого вида; WAL — начиная с сегмента старейшего оставшегося base."""
    repo = _repo(args)
    removed: dict[str, list[str]] = {}
    with repo.lock():
        for kind, keep in (("base", args.keep_base), ("dump", args.keep_dumps), ("secrets", args.keep_secrets)):
            ids = repo.list_ids(kind)
            doomed = ids[:-keep] if keep > 0 else []
            for backup_id in doomed:
                shutil.rmtree(repo.set_dir(kind, backup_id))
            removed[kind] = doomed
        bases = repo.list_ids("base")
        wal_removed: list[str] = []
        if bases:
            oldest = repo.read_manifest("base", bases[0])
            keep_sysids = {repo.read_manifest("base", b)["system_identifier"] for b in bases}
            for path in sorted((repo.root / "wal").glob("*/*.enc")):
                name, sysid = path.name.removesuffix(".enc"), path.parent.name
                too_old = sysid == oldest["system_identifier"] and name[8:] < oldest["start_wal_segment"][8:]
                if sysid not in keep_sysids or (pg.SEGMENT_RE.fullmatch(name) and too_old):
                    path.unlink()
                    wal_removed.append(f"{sysid}/{name}")
        removed["wal"] = wal_removed
        snaps = repo.list_ids("artifacts")
        doomed_snaps = snaps[: -args.keep_artifact_snapshots] if args.keep_artifact_snapshots > 0 else []
        for backup_id in doomed_snaps:
            shutil.rmtree(repo.set_dir("artifacts", backup_id))
        removed["artifacts"] = doomed_snaps
        live: set[str] = set()
        for backup_id in repo.list_ids("artifacts"):
            live |= {o["sha256"] for o in repo.read_manifest("artifacts", backup_id)["objects"].values()}
        dropped = 0
        for path in (repo.root / "artifacts" / "objects").glob("*/*.enc"):
            if path.name.removesuffix(".enc") not in live:
                path.unlink()
                dropped += 1
        removed["artifact_objects"] = [str(dropped)]
    return {"removed": removed}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.backup", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("keygen", help="Создать ключ бэкапа (32 байта, base64, 0600)")
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(handler=cmd_keygen)

    p = sub.add_parser("init", help="Создать репозиторий бэкапов для ключа")
    _add_common(p)
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("pg-base", help="Физический бэкап PostgreSQL (pg_basebackup)")
    _add_common(p)
    _add_pg(p)
    p.set_defaults(handler=cmd_pg_base)

    p = sub.add_parser("pg-wal-sync", help="Архив WAL из тома в репозиторий")
    _add_common(p)
    p.add_argument("--wal-volume", required=True, help="том архива WAL (compose: aicodingdev_pgwal)")
    p.add_argument("--pg-image", default=POSTGRES_IMAGE)
    p.add_argument("--delete-synced", action="store_true", help="удалить из тома перенесённые сегменты")
    p.set_defaults(handler=cmd_wal_sync)

    p = sub.add_parser("pg-dump", help="Логический дамп (pg_dump -Fc)")
    _add_common(p)
    _add_pg(p)
    p.set_defaults(handler=cmd_pg_dump)

    p = sub.add_parser("artifacts", help="Зеркало артефактов с проверкой по artifacts.sha256")
    _add_common(p)
    _add_store(p)
    _add_rows_source(p)
    p.add_argument("--full", action="store_true", help="перечитать все объекты (иначе — только новые ключи)")
    p.set_defaults(handler=cmd_artifacts)

    p = sub.add_parser("secrets", help="Зашифрованная копия каталога секретов")
    _add_common(p)
    p.add_argument("--dir", type=Path, required=True)
    p.set_defaults(handler=cmd_secrets)

    p = sub.add_parser("list", help="Наборы в репозитории и статус")
    _add_common(p)
    p.set_defaults(handler=cmd_list)

    p = sub.add_parser("verify", help="Расшифровать и сверить наборы с манифестами (+ архив WAL)")
    _add_common(p)
    p.add_argument("--kind", choices=["base", "dump", "secrets"])
    p.add_argument("--backup-id")
    p.add_argument("--wal", action="store_true")
    p.set_defaults(handler=cmd_verify)

    p = sub.add_parser("restore-pg", help="Базовый бэкап + архив WAL в пустые тома, recovery-настройки")
    _add_common(p)
    p.add_argument("--backup-id", default="latest")
    p.add_argument("--data-volume", required=True)
    p.add_argument("--wal-volume", required=True, help="том для расшифрованного архива WAL (restore_command)")
    p.add_argument("--target-time", help="PITR: ISO-время (UTC), иначе — до конца архива")
    p.add_argument("--pg-image", default=POSTGRES_IMAGE)
    p.set_defaults(handler=cmd_restore_pg)

    p = sub.add_parser("post-restore", help="После promote: снять recovery-настройки, сдвинуть fencing token")
    _add_pg(p)
    p.set_defaults(handler=cmd_post_restore)

    p = sub.add_parser("restore-artifacts", help="Объекты снимка → хранилище")
    _add_common(p)
    _add_store(p)
    p.add_argument("--backup-id", default="latest")
    p.set_defaults(handler=cmd_restore_artifacts)

    p = sub.add_parser("verify-artifacts", help="Хранилище против artifacts.sha256 (после восстановления)")
    _add_common(p)
    _add_store(p)
    p.add_argument("--snapshot", help="id восстановленного снимка (latest): строки новее снимка — потеря в RPO")
    _add_rows_source(p)
    p.set_defaults(handler=cmd_verify_artifacts)

    p = sub.add_parser("restore-secrets", help="Секреты → новый каталог (0700/0600, без перезаписи)")
    _add_common(p)
    p.add_argument("--backup-id", default="latest")
    p.add_argument("--to", type=Path, required=True)
    p.set_defaults(handler=cmd_restore_secrets)

    p = sub.add_parser("prune", help="Удалить старые наборы и ненужные сегменты WAL")
    _add_common(p)
    p.add_argument("--keep-base", type=int, default=7)
    p.add_argument("--keep-dumps", type=int, default=7)
    p.add_argument("--keep-secrets", type=int, default=30)
    p.add_argument("--keep-artifact-snapshots", type=int, default=14)
    p.set_defaults(handler=cmd_prune)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    component = COMPONENT_OF.get(args.command)
    try:
        result = handler(args)
    except (pg.BackupError, BackupCryptoError, RepositoryError, DockerError, art.BackupIntegrityError, OSError) as exc:
        detail = getattr(exc, "report", None)
        print(json.dumps({"ok": False, "error": str(exc), "detail": detail}, ensure_ascii=False, indent=2))
        if component and args.repo and args.key_file:
            with contextlib.suppress(RepositoryError, BackupCryptoError, OSError):
                _repo(args).record_status(component, ok=False, error=str(exc), extra_status_file=args.status_file)
        return 1
    if component:
        _repo(args).record_status(component, ok=True, extra_status_file=args.status_file)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
