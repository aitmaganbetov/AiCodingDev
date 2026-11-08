"""P1-15: S3-совместимый адаптер (MinIO). Требует AICD_TEST_S3_ENDPOINT/_ACCESS_KEY/_SECRET_KEY."""

from __future__ import annotations

import io
import os
import secrets
import urllib.request

import pytest

from aicd_platform.artifacts import ArtifactIntegrityError, S3ArtifactStore
from aicd_platform.ids import uuid7

pytestmark = pytest.mark.s3

ENDPOINT = os.environ.get("AICD_TEST_S3_ENDPOINT")


@pytest.fixture(scope="module")
def store() -> S3ArtifactStore:
    if not ENDPOINT:
        pytest.skip("AICD_TEST_S3_ENDPOINT не задан")
    s = S3ArtifactStore(
        endpoint_url=ENDPOINT,
        bucket=f"aicd-test-{secrets.token_hex(3)}",
        access_key=os.environ["AICD_TEST_S3_ACCESS_KEY"],
        secret_key=os.environ["AICD_TEST_S3_SECRET_KEY"],
    )
    s.ensure_bucket()
    return s


def test_put_get_presign(store: S3ArtifactStore) -> None:
    ws, aid = uuid7(), uuid7()
    data = secrets.token_bytes(3 * 1024 * 1024 + 7)  # больше одного чанка
    stored = store.put(ws, aid, io.BytesIO(data))
    assert stored.storage_ref.startswith(f"s3://{store._bucket}/{ws}/")
    assert stored.size_bytes == len(data) and store.exists(stored.storage_ref) and store.healthy()
    assert b"".join(store.open(stored.storage_ref, expected_sha256=stored.sha256)) == data
    url = store.presigned_get_url(stored.storage_ref, ttl_seconds=60, filename="report.txt")
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 — локальный MinIO
        assert resp.read() == data
    with pytest.raises(FileExistsError):
        store.put(ws, aid, io.BytesIO(b"x"))


def test_integrity_mismatch_detected(store: S3ArtifactStore) -> None:
    stored = store.put(uuid7(), uuid7(), io.BytesIO(b"payload"))
    with pytest.raises(ArtifactIntegrityError):
        b"".join(store.open(stored.storage_ref, expected_sha256="0" * 64))


def test_foreign_bucket_ref_rejected(store: S3ArtifactStore) -> None:
    with pytest.raises(ValueError, match="bucket"):
        store.exists("s3://other-bucket/x")
