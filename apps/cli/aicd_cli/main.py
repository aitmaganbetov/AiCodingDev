"""Административная CLI платформы `aicd` (P1-09; MODULE_BOUNDARIES §1 apps/cli).

Команды:
  aicd admin create-owner --email E --name N --workspace-slug S --workspace-name W
      Создаёт первого пользователя и workspace с ролью owner. Пароль читается из stdin
      (или интерактивно без эха) — дефолтных паролей нет (ADR-0009).
  aicd db ensure-partitions [--months N]
      Создаёт помесячные партиции audit_events вперёд (подключение роли aicd_migrator).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import text

from aicd_identity import api as identity
from aicd_identity.passwords import WeakPasswordError
from aicd_platform.config import get_settings
from aicd_platform.db import DbContext, apply_context, create_engine, transaction
from aicd_platform.errors import ProblemError
from aicd_platform.ids import uuid7
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
    engine = create_engine(settings.require_database_url(), pool_size=1)
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
    engine = create_engine(url, pool_size=1)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = asyncio.run(args.handler(args))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
