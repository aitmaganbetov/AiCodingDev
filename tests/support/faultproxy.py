"""TCP-прокси с внедрением сетевых сбоев для chaos-тестов P9-03 (аналог toxiproxy, без зависимостей).

Прокси слушает свой порт на 127.0.0.1 и пересылает байты на `upstream`. Сбои:

* `cut()` — оборвать все текущие соединения (RST): разрыв соединений PostgreSQL/S3/Redis посреди запроса;
* `down()` / `up()` — «сервис недоступен»: новые соединения принимаются и сразу сбрасываются, текущие рвутся
  (рестарт/failover сервера с точки зрения клиента); `up()` — снова пропускать;
* `latency` — задержка перед пересылкой каждого блока (медленная сеть).

Общие контейнеры PostgreSQL/Redis/S3 не трогаются: сбой виден только клиентам, подключённым через прокси.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
import time
from collections.abc import Iterator


class FaultProxy:
    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.latency = 0.0
        self.accepted = 0
        self._down = False
        self._lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(128)
        self.port = int(self._listener.getsockname()[1])
        self._stopped = threading.Event()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    # --- управление сбоями ------------------------------------------------------------------------

    def cut(self) -> int:
        """Оборвать все текущие соединения (RST). Возвращает число оборванных сокетов."""
        with self._lock:
            conns, self._conns = list(self._conns), set()
        for sock in conns:
            _reset(sock)
        return len(conns)

    def down(self) -> None:
        self._down = True
        self.cut()

    def up(self) -> None:
        self._down = False

    @contextlib.contextmanager
    def outage(self) -> Iterator[None]:
        self.down()
        try:
            yield
        finally:
            self.up()

    def close(self) -> None:
        self._stopped.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        self.cut()

    # --- пересылка --------------------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            if self._down:
                _reset(client)
                continue
            try:
                server = socket.create_connection(self.upstream, timeout=5)
            except OSError:
                _reset(client)
                continue
            server.settimeout(None)
            self.accepted += 1
            with self._lock:
                self._conns.update((client, server))
            threading.Thread(target=self._pipe, args=(client, server), daemon=True).start()
            threading.Thread(target=self._pipe, args=(server, client), daemon=True).start()

    def _pipe(self, src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                if self.latency:
                    time.sleep(self.latency)
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (src, dst):
                with self._lock:
                    self._conns.discard(sock)
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(OSError):
                    sock.close()


def _reset(sock: socket.socket) -> None:
    """Обрыв соединения как при падении сервера/сети: SO_LINGER 0 (RST при закрытии); `shutdown` будит поток
    пересылки, заблокированный в `recv` (иначе на Linux `close` из другого потока не освобождает сокет и пир
    продолжает ждать ответа)."""
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()
