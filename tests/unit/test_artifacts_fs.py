import io
from pathlib import Path

import pytest

from aicd_platform.artifacts import ArtifactIntegrityError, FileSystemArtifactStore
from aicd_platform.ids import uuid7


def test_put_open_verify(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    ws, aid = uuid7(), uuid7()
    stored = store.put(ws, aid, io.BytesIO(b"hello" * 1000))
    assert stored.storage_ref.startswith(f"file://artifacts/{ws}/")
    assert stored.size_bytes == 5000
    assert b"".join(store.open(stored.storage_ref, expected_sha256=stored.sha256)) == b"hello" * 1000
    assert store.exists(stored.storage_ref) and store.healthy()


def test_immutable_and_integrity(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    ws, aid = uuid7(), uuid7()
    stored = store.put(ws, aid, io.BytesIO(b"data"))
    with pytest.raises(FileExistsError):
        store.put(ws, aid, io.BytesIO(b"other"))
    path = tmp_path / stored.storage_ref.removeprefix("file://artifacts/")
    path.chmod(0o600)
    path.write_bytes(b"evil")  # подмена содержимого в хранилище
    with pytest.raises(ArtifactIntegrityError):
        b"".join(store.open(stored.storage_ref, expected_sha256=stored.sha256))


@pytest.mark.parametrize("ref", ["file://artifacts/../../etc/passwd", "file://artifacts/x", "s3://b/k"])
def test_bad_refs(tmp_path: Path, ref: str) -> None:
    with pytest.raises(ValueError, match=r"ссылк|ключ"):
        FileSystemArtifactStore(tmp_path).open(ref)
