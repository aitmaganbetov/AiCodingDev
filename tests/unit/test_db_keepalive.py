"""P9-03: TCP keepalive / tcp_user_timeout соединений PostgreSQL (обнаружение «чёрной дыры» сети)."""

from __future__ import annotations

import os
import socket

import pytest

from aicd_platform.config import Settings
from aicd_platform.db import TcpKeepalive, connect_args, create_engine


def test_default_connect_args_enable_keepalive_and_user_timeout() -> None:
    args = connect_args()
    assert args["options"] == "-c timezone=UTC -c statement_timeout=15000"
    assert args["keepalives"] == 1
    assert (args["keepalives_idle"], args["keepalives_interval"], args["keepalives_count"]) == (30, 10, 3)
    assert args["tcp_user_timeout"] == 60_000


def test_keepalive_from_settings_and_disabled_user_timeout() -> None:
    settings = Settings(
        db_keepalives_idle_seconds=5, db_keepalives_interval_seconds=2, db_keepalives_count=4, db_tcp_user_timeout_ms=0
    )
    keepalive = TcpKeepalive.from_settings(settings)
    assert keepalive == TcpKeepalive(5, 2, 4, 0)
    args = connect_args(keepalive)
    assert (args["keepalives_idle"], args["keepalives_interval"], args["keepalives_count"]) == (5, 2, 4)
    assert "tcp_user_timeout" not in args, "0 — tcp_user_timeout не задаётся"


@pytest.mark.db
async def test_engine_connections_get_socket_options() -> None:
    """Через `create_engine` параметры доходят до сокета libpq (Linux): SO_KEEPALIVE, TCP_KEEPIDLE/INTVL/CNT,
    TCP_USER_TIMEOUT — у каждого соединения пула."""
    url = os.environ.get("AICD_TEST_DATABASE_ADMIN_URL")
    if not url or not hasattr(socket, "TCP_USER_TIMEOUT"):
        pytest.skip("нужны AICD_TEST_DATABASE_ADMIN_URL и Linux")
    engine = create_engine(url, pool_size=1, keepalive=TcpKeepalive(11, 4, 5, 12_345))
    try:
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            fd = raw.driver_connection.pgconn.socket  # type: ignore[union-attr]
            sock = socket.socket(fileno=os.dup(fd))
            try:
                assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 11
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 4
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 5
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT) == 12_345
            finally:
                sock.close()
    finally:
        await engine.dispose()
