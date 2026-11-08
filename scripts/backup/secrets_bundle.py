"""Секреты и конфигурация инсталляции: зашифрованный tar каталога секретов (SC-DATA-03).

Копируются только обычные файлы каталога (без подкаталогов и ссылок). Учётные данные CLI (тома
`aicd-cred-<provider>`, каталоги `.codex`/`.claude`) в бэкап не попадают никогда (SC-CLI-03): после
восстановления оператор заново входит в CLI по подписке (`docs/runbooks/cli-auth.md`).
Восстановление пишет файлы `0600` в каталог `0700` и не перезаписывает существующие.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
from pathlib import Path
from typing import Any

from scripts.backup.crypto import decrypt_file, encrypt_file
from scripts.backup.repo import Repository, RepositoryError, iso, new_backup_id, utc_now

FORBIDDEN = re.compile(r"(^aicd-cred-|^\.codex$|^\.claude$|auth\.json$|credentials)", re.IGNORECASE)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def backup_secrets(repo: Repository, directory: Path) -> dict[str, Any]:
    files = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            if FORBIDDEN.search(path.name) or not NAME_RE.fullmatch(path.name):
                raise RepositoryError(f"{path.name}: файл не допускается в бэкап секретов (учётные данные CLI?)")
            data = path.read_bytes()
            info = tarfile.TarInfo(path.name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
            files.append({"name": path.name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    if not files:
        raise RepositoryError(f"{directory}: нет файлов секретов")
    backup_id = new_backup_id()
    out = repo.set_dir("secrets", backup_id) / "secrets.tar.enc"
    stats = encrypt_file(repo.key, f"secrets/{backup_id}/secrets.tar", iter([buffer.getvalue()]), out)
    manifest = {
        "created_at": iso(utc_now()),
        "source_dir_name": directory.name,
        "entries": files,
        "files": [{"name": "secrets.tar", "path": out.name, **stats.__dict__}],
    }
    repo.write_manifest("secrets", backup_id, manifest)
    return {"backup_id": backup_id, "files": len(files)}


def restore_secrets(repo: Repository, backup_id: str, target: Path) -> dict[str, Any]:
    backup_id = repo.resolve_id("secrets", backup_id)
    manifest = repo.read_manifest("secrets", backup_id)
    entry = manifest["files"][0]
    data = b"".join(
        decrypt_file(
            repo.key, f"secrets/{backup_id}/{entry['name']}", repo.set_dir("secrets", backup_id) / entry["path"]
        )
    )
    expected = {e["name"]: e["sha256"] for e in manifest["entries"]}
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    written = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            if not member.isfile() or member.name not in expected or not NAME_RE.fullmatch(member.name):
                raise RepositoryError(f"неожиданный элемент архива секретов: {member.name!r}")
            fh = tar.extractfile(member)
            content = fh.read() if fh else b""
            if hashlib.sha256(content).hexdigest() != expected[member.name]:
                raise RepositoryError(f"{member.name}: sha256 не совпадает с манифестом")
            dest = target / member.name
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # существующие не перезаписываются
            with os.fdopen(fd, "wb") as out:
                out.write(content)
            written.append(member.name)
    return {"backup_id": backup_id, "restored": written}
