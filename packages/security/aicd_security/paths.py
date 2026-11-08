"""Безопасное разрешение путей внутри allowlist-корней (FR-PRJ-02, ТЗ §20 path traversal).

Правила:
* вход — путь пользователя (абсолютный или относительный к корню);
* результат обязан лежать внутри одного из настроенных корней после `realpath`
  (символические ссылки разрешаются, выход за корень через symlink запрещён);
* запрещены NUL-байты, управляющие символы, компоненты `..` до нормализации;
* ссылка в БД — `<имя_корня>/<относительный путь>` без ведущего `/` и `..`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """Путь вне разрешённых корней или содержит недопустимые элементы."""


@dataclass(frozen=True)
class ResolvedPath:
    root_name: str
    absolute: Path
    relative: PurePosixPath

    @property
    def ref(self) -> str:
        rel = self.relative.as_posix()
        return self.root_name if rel in ("", ".") else f"{self.root_name}/{rel}"


def _check_raw(raw: str) -> None:
    if not raw or len(raw) > 4096:
        raise UnsafePathError("пустой или слишком длинный путь")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in raw):
        raise UnsafePathError("управляющие символы в пути")
    if ".." in PurePosixPath(raw).parts:
        raise UnsafePathError("компонент '..' запрещён")


def resolve_in_roots(raw: str, roots: dict[str, Path]) -> ResolvedPath:
    """Разрешает путь пользователя в один из корней. Бросает UnsafePathError.

    Принимаются абсолютный путь или ссылка `<имя_корня>/<относительный путь>`.
    Проверка выполняется по реальному пути (после разрешения symlink).
    """
    _check_raw(raw)
    real = Path(os.path.realpath(raw)) if raw.startswith("/") else resolve_ref(raw, roots)
    for name, root in roots.items():
        root_real = Path(os.path.realpath(root))
        if real == root_real or real.is_relative_to(root_real):
            return ResolvedPath(name, real, PurePosixPath(real.relative_to(root_real).as_posix()))
    raise UnsafePathError("путь вне разрешённых корней")


def resolve_ref(ref: str, roots: dict[str, Path]) -> Path:
    """Обратное преобразование ссылки `<корень>/<относительный путь>` в абсолютный путь."""
    _check_raw(ref)
    if ref.startswith("/"):
        raise UnsafePathError("ссылка не может быть абсолютной")
    name, _, rel = ref.partition("/")
    root = roots.get(name)
    if root is None:
        raise UnsafePathError("неизвестный корень")
    root_real = Path(os.path.realpath(root))
    real = Path(os.path.realpath(root_real / rel)) if rel else root_real
    if not (real == root_real or real.is_relative_to(root_real)):
        raise UnsafePathError("ссылка выходит за пределы корня")
    return real
