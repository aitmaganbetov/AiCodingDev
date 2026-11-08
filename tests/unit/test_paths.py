import os
from pathlib import Path

import pytest

from aicd_security.paths import UnsafePathError, resolve_in_roots, resolve_ref


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "repos"
    (root / "proj" / "sub").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", root / "escape")
    return {"main": root}


def test_absolute_and_ref_forms(roots: dict[str, Path]) -> None:
    root = roots["main"]
    r = resolve_in_roots(str(root / "proj"), roots)
    assert r.ref == "main/proj"
    assert resolve_in_roots("main/proj/sub", roots).absolute == (root / "proj" / "sub").resolve()
    assert resolve_ref("main/proj", roots) == (root / "proj").resolve()


@pytest.mark.parametrize(
    "raw",
    ["main/../../etc", "/etc", "main/escape", "other/proj", "main/proj\x00x", "", "main/proj/\n"],
)
def test_traversal_and_escapes_rejected(roots: dict[str, Path], raw: str) -> None:
    with pytest.raises(UnsafePathError):
        resolve_in_roots(raw, roots)


def test_absolute_symlink_escape_rejected(roots: dict[str, Path]) -> None:
    with pytest.raises(UnsafePathError):
        resolve_in_roots(str(roots["main"] / "escape"), roots)
