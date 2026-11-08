"""Административная CLI платформы `aicd` (P1-09; MODULE_BOUNDARIES §1 apps/cli).

Команды:
  aicd admin create-owner --email E --name N --workspace-slug S --workspace-name W
      Создаёт первого пользователя и workspace с ролью owner. Пароль читается из stdin
      (или интерактивно без эха) — дефолтных паролей нет (ADR-0009).
  aicd db ensure-partitions [--months N]
      Создаёт помесячные партиции audit_events вперёд (подключение роли aicd_migrator).
  aicd db sync-agent-profiles
      Синхронизирует справочник agent_profiles с версионированными профилями ролей (P4-02;
      подключение роли aicd_migrator: aicd_app/aicd_worker имеют только SELECT).
  aicd maintenance retention [--reason R] [--every SECONDS]
      Retention/архивирование (DATABASE_DESIGN §20) под ролью aicd_maintenance
      (AICD_MAINTENANCE_DATABASE_URL(_FILE)); сессия и каждое действие аудируются (§19.6).
      Объекты артефактов удаляются через хранилище из настроек (AICD_ARTIFACT_*). `--every` — расписание
      без внешнего cron: проход сразу и затем каждые SECONDS до SIGTERM/SIGINT (ошибка прохода не
      останавливает цикл; код выхода — последнего прохода).
  aicd maintenance orphan-objects [--grace-hours H] [--dry-run] [--reason R]
      Удаляет объекты хранилища артефактов (FS/S3) без строки `artifacts` старше H часов (по умолчанию 24);
      `--dry-run` — только подсчёт. Роль aicd_maintenance, сессия и пакеты аудируются.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import signal
import sys
from datetime import timedelta

from sqlalchemy import text

from aicd_identity import api as identity
from aicd_identity.passwords import WeakPasswordError
from aicd_orchestration.agents.profiles import sync_profiles
from aicd_platform.artifact_gc import sweep_orphan_objects
from aicd_platform.artifacts import FileSystemArtifactStore, S3ArtifactStore, build_artifact_store
from aicd_platform.config import get_settings
from aicd_platform.db import DbContext, TcpKeepalive, apply_context, create_engine, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7
from aicd_platform.retention import build_archive_sink, run_retention
from aicd_projects import api as projects


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Пароль владельца: ")
    if getpass.getpass("Повторите пароль: ") != first:
        raise SystemExit("Пароли не совпадают")
    return first


async def _create_owner(args: argparse.Namespace) -> int:
    settings = get_settings()
    password = _read_password(args.password_stdin)
    engine = create_engine(settings.require_database_url(), pool_size=1, keepalive=TcpKeepalive.from_settings(settings))
    try:
        async with transaction(engine, DbContext(actor_type="system")) as conn:
            user_id = await identity.create_user(conn, email=args.email, display_name=args.name, password=password)
            workspace_id = uuid7()
            await apply_context(conn, DbContext(user_id=user_id, workspace_id=workspace_id))
            await projects.create_workspace(
                conn, workspace_id=workspace_id, name=args.workspace_name, slug=args.workspace_slug, owner_id=user_id
            )
    except ProblemError as err:
        print(f"Ошибка: {err.detail or err.title}", file=sys.stderr)
        return 1
    except WeakPasswordError as err:
        print(f"Ошибка: {err}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"Создан владелец {args.email} (user_id={user_id}) и workspace {args.workspace_slug} ({workspace_id})")
    return 0


async def _ensure_partitions(args: argparse.Namespace) -> int:
    url = os.environ.get("AICD_MIGRATOR_DATABASE_URL")
    if not url:
        print("Задайте AICD_MIGRATOR_DATABASE_URL (роль aicd_migrator)", file=sys.stderr)
        return 2
    engine = create_engine(url, pool_size=1, keepalive=TcpKeepalive.from_settings(get_settings()))
    try:
        async with engine.begin() as conn:
            created = (
                await conn.execute(
                    text("SELECT maint_ensure_monthly_partitions('audit_events', current_date, :m)"), {"m": args.months}
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    print(f"Создано партиций: {created}")
    return 0


async def _sync_agent_profiles(args: argparse.Namespace) -> int:
    url = os.environ.get("AICD_MIGRATOR_DATABASE_URL")
    if not url:
        print("Задайте AICD_MIGRATOR_DATABASE_URL (роль aicd_migrator)", file=sys.stderr)
        return 2
    engine = create_engine(url, pool_size=1, keepalive=TcpKeepalive.from_settings(get_settings()))
    try:
        async with engine.begin() as conn:
            results = await sync_profiles(conn)
    finally:
        await engine.dispose()
    for r in results:
        print(f"{r.role}@{r.version}: {r.action}")
    return 0


async def _retention_pass(args: argparse.Namespace) -> int:
    settings = get_settings()
    try:
        url = settings.require_maintenance_database_url()
        sink = build_archive_sink(settings)
    except RuntimeError as err:
        print(f"Ошибка: {err}", file=sys.stderr)
        return 2
    engine = create_engine(url, pool_size=1, keepalive=TcpKeepalive.from_settings(settings))
    try:
        store = build_artifact_store(settings)
        deleter = store if isinstance(store, FileSystemArtifactStore | S3ArtifactStore) else None
        report = await run_retention(engine, settings, sink=sink, artifacts=deleter, reason=args.reason)
    finally:
        await engine.dispose()
    print(json.dumps({"maintenance_run_id": str(report.session_id), **report.summary()}, ensure_ascii=False))
    return 0 if not report.warnings else 3


async def _retention(args: argparse.Namespace) -> int:
    if not args.every:
        return await _retention_pass(args)
    if args.every < 60:
        print("Ошибка: --every должен быть не меньше 60 секунд", file=sys.stderr)
        return 2
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    code = 0
    while not stop.is_set():
        try:
            code = await _retention_pass(args)
        except Exception as exc:  # noqa: BLE001 — сбой прохода (БД недоступна и т. п.) не останавливает расписание
            print(f"retention: проход завершился ошибкой {type(exc).__name__}: {exc}", file=sys.stderr)
            code = 1
        if code == 2:
            return code  # ошибка конфигурации — повтор бессмыслен
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=args.every)
    return code


async def _orphan_objects(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.grace_hours < 1:
        print("Ошибка: --grace-hours должен быть не меньше 1", file=sys.stderr)
        return 2
    try:
        url = settings.require_maintenance_database_url()
    except RuntimeError as err:
        print(f"Ошибка: {err}", file=sys.stderr)
        return 2
    store = build_artifact_store(settings)
    if not isinstance(store, FileSystemArtifactStore | S3ArtifactStore):  # pragma: no cover — других хранилищ нет
        print("Ошибка: хранилище не поддерживает перечисление объектов", file=sys.stderr)
        return 2
    engine = create_engine(url, pool_size=1, keepalive=TcpKeepalive.from_settings(settings))
    try:
        report = await sweep_orphan_objects(
            engine, store, grace=timedelta(hours=args.grace_hours), dry_run=args.dry_run, reason=args.reason
        )
    finally:
        await engine.dispose()
    print(json.dumps({"maintenance_run_id": str(report.session_id), **report.summary()}, ensure_ascii=False))
    return 0 if not report.failed else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicd", description="Администрирование AiCodingDev")
    sub = parser.add_subparsers(dest="group", required=True)

    admin = sub.add_parser("admin", help="Учётные записи")
    admin_sub = admin.add_subparsers(dest="command", required=True)
    owner = admin_sub.add_parser("create-owner", help="Создать первого владельца и workspace")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", required=True)
    owner.add_argument("--workspace-slug", required=True)
    owner.add_argument("--workspace-name", required=True)
    owner.add_argument("--password-stdin", action="store_true", help="Прочитать пароль из первой строки stdin")
    owner.set_defaults(handler=_create_owner)

    db = sub.add_parser("db", help="Обслуживание БД")
    db_sub = db.add_subparsers(dest="command", required=True)
    parts = db_sub.add_parser("ensure-partitions", help="Создать партиции audit_events вперёд")
    parts.add_argument("--months", type=int, default=3)
    parts.set_defaults(handler=_ensure_partitions)
    sync = db_sub.add_parser("sync-agent-profiles", help="Синхронизировать agent_profiles с профилями ролей")
    sync.set_defaults(handler=_sync_agent_profiles)

    maint = sub.add_parser("maintenance", help="Обслуживание под ролью aicd_maintenance (§19.6)")
    maint_sub = maint.add_subparsers(dest="command", required=True)
    retention = maint_sub.add_parser("retention", help="Партиции вперёд, архив и удаление по срокам хранения")
    retention.add_argument("--reason", default="scheduled retention", help="Причина (пишется в аудит)")
    retention.add_argument("--every", type=float, default=0, metavar="SECONDS",
                           help="Повторять проход каждые SECONDS (≥ 60) до SIGTERM; по умолчанию — один проход")  # fmt: skip
    retention.set_defaults(handler=_retention)
    orphans = maint_sub.add_parser("orphan-objects", help="Удалить объекты хранилища артефактов без строки artifacts")
    orphans.add_argument("--grace-hours", type=float, default=24.0, help="Не трогать объекты моложе (часов)")
    orphans.add_argument("--dry-run", action="store_true", help="Только подсчитать сирот, не удалять")
    orphans.add_argument("--reason", default="orphan artifact objects sweep", help="Причина (пишется в аудит)")
    orphans.set_defaults(handler=_orphan_objects)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = asyncio.run(args.handler(args))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
