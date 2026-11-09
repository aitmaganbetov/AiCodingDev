"""Проверка релиза: подписи, provenance (SLSA v1), SBOM-аттестации, SHA256SUMS; `--tamper-test` — отказ при подмене.

    uv run python -m scripts.release.verify --release dist/release/X --public-key <доверенный cosign.pub> \\
        [--expect-commit SHA] [--tamper-test] [--report reports/release/SUPPLY_CHAIN_VERIFY.md]

Открытый ключ берётся из доверенного источника (не из каталога релиза: `cosign.pub` релиза только сверяется по
sha256 с доверенным). Проверка офлайн: Rekor/Fulcio не используются (`--insecure-ignore-tlog=true`), доверие —
только ключу. `--tamper-test` копирует релиз во временный каталог и убеждается, что каждая подмена (байт образа,
SBOM, SHA256SUMS, provenance другого образа, чужой ключ) даёт FAIL.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.release.common import OFFLINE_VERIFY, ROOT, SLSA_PREDICATE_TYPE, cosign, sha256_file


@dataclass
class Result:
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)


def _key_mount(public_key: Path) -> dict[Path, str]:
    return {public_key.resolve().parent: "/keys"}


def verify_blob(release: Path, public_key: Path, rel: str, bundle: str) -> tuple[bool, str]:
    proc = cosign(release, ["verify-blob", "--key", f"/keys/{public_key.name}", "--bundle", bundle, *OFFLINE_VERIFY, rel],
                  mounts=_key_mount(public_key))  # fmt: skip
    return proc.returncode == 0, (proc.stderr.strip().splitlines() or [""])[-1][:300]


def verify_attestation(release: Path, public_key: Path, rel: str, bundle: str, kind: str) -> tuple[bool, str]:
    args = ["verify-blob-attestation", "--key", f"/keys/{public_key.name}", "--bundle", bundle, "--type", kind]
    proc = cosign(release, [*args, *OFFLINE_VERIFY, rel], mounts=_key_mount(public_key))
    return proc.returncode == 0, (proc.stderr.strip().splitlines() or [""])[-1][:300]


def statement(release: Path, bundle: str) -> dict[str, Any]:
    """In-toto statement из DSSE-конверта Sigstore bundle (подпись уже проверена cosign)."""
    doc = json.loads((release / bundle).read_text(encoding="utf-8"))
    envelope = doc.get("dsseEnvelope") or {}
    payload: dict[str, Any] = json.loads(base64.b64decode(envelope.get("payload", "")))
    return payload


def verify_release(release: Path, public_key: Path, expect_commit: str | None = None) -> Result:
    result = Result()
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    ok, detail = verify_blob(release, public_key, "SHA256SUMS", "SHA256SUMS.sigstore.json")
    result.add("SHA256SUMS: подпись ключом релиза", ok, detail)
    bad = []
    for line in (release / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        path = release / rel
        if not path.is_file() or sha256_file(path) != digest:
            bad.append(rel)
    result.add("SHA256SUMS: все файлы релиза совпадают", not bad, f"несовпадения: {bad}" if bad else "")
    trusted = sha256_file(public_key)
    result.add("cosign.pub релиза = доверенный ключ", sha256_file(release / "cosign.pub") == trusted
               and manifest.get("public_key_sha256") == trusted, trusted[:16])  # fmt: skip
    commit = manifest["source"]["commit"]
    if expect_commit:
        result.add("коммит релиза = ожидаемый", commit == expect_commit, commit)
    for image in manifest["images"]:
        name, tar = image["name"], image["tar"]
        ok, detail = verify_blob(release, public_key, tar, image["signature"])
        result.add(f"{name}: подпись архива образа", ok, detail)
        bundle = image["attestations"]["slsaprovenance1"]
        ok, detail = verify_attestation(release, public_key, tar, bundle, "slsaprovenance1")
        result.add(f"{name}: SLSA provenance (подпись, subject = архив образа)", ok, detail)
        if ok:
            st = statement(release, bundle)
            predicate = st.get("predicate", {})
            subjects = {s.get("digest", {}).get("sha256") for s in st.get("subject", [])}
            source = predicate.get("buildDefinition", {}).get("externalParameters", {}).get("source", {})
            byproducts = {b.get("name"): b for b in predicate.get("runDetails", {}).get("byproducts", [])}
            consistent = (
                st.get("predicateType") == SLSA_PREDICATE_TYPE
                and sha256_file(release / tar) in subjects
                and source.get("digest", {}).get("gitCommit") == commit
                and byproducts.get("image_id", {}).get("digest", {}).get("sha256") == image["image_id"].removeprefix("sha256:")
            )  # fmt: skip
            result.add(f"{name}: provenance согласован (тип, subject, commit, image_id)", consistent,
                       f"commit {commit[:12]}, dirty={source.get('dirty')}, releasable={predicate.get('buildDefinition', {}).get('internalParameters', {}).get('releasable')}")  # fmt: skip
        ok, detail = verify_attestation(release, public_key, tar, image["attestations"]["cyclonedx"], "cyclonedx")
        result.add(f"{name}: SBOM-аттестация CycloneDX образа", ok, detail)
    for rel, meta in manifest["sboms"].items():
        ok, detail = verify_blob(release, public_key, rel, meta["signature"])
        sbom = json.loads((release / rel).read_text(encoding="utf-8"))
        result.add(f"SBOM {rel}: подпись, CycloneDX, {len(sbom.get('components', []))} компонентов",
                   ok and sbom.get("bomFormat") == "CycloneDX" and bool(sbom.get("components")), detail)  # fmt: skip
    return result


def _flip_byte(path: Path, offset: int) -> None:
    with path.open("r+b") as fh:
        fh.seek(offset)
        byte = fh.read(1)
        fh.seek(offset)
        fh.write(bytes([byte[0] ^ 0x01]))


def tamper_tests(release: Path, public_key: Path, other_key: Path) -> list[tuple[str, bool, str]]:
    """Каждая подмена обязана дать FAIL проверки (ok=True в результате теста — проверка отказала)."""
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    first = manifest["images"][0]
    cases: list[tuple[str, Any]] = []

    def flip_image(d: Path) -> None:
        _flip_byte(d / first["tar"], (d / first["tar"]).stat().st_size // 2)

    def edit_sbom(d: Path) -> None:
        rel = next(iter(manifest["sboms"]))
        doc = json.loads((d / rel).read_text())
        doc["components"][0]["name"] = "evil-" + str(doc["components"][0]["name"])
        (d / rel).write_text(json.dumps(doc))

    def edit_sums(d: Path) -> None:
        text = (d / "SHA256SUMS").read_text()
        (d / "SHA256SUMS").write_text(("0" * 64) + text[64:])

    def swap_provenance(d: Path) -> None:
        if len(manifest["images"]) < 2:
            _flip_byte(d / first["attestations"]["slsaprovenance1"], 200)
            return
        second = manifest["images"][1]
        shutil.copy(d / second["attestations"]["slsaprovenance1"], d / first["attestations"]["slsaprovenance1"])

    cases = [("подмена байта в архиве образа", flip_image), ("изменение SBOM", edit_sbom),
             ("изменение SHA256SUMS", edit_sums), ("provenance другого образа", swap_provenance)]  # fmt: skip
    out = []
    for name, mutate in cases:
        with tempfile.TemporaryDirectory(prefix="aicd-tamper-") as tmp:
            copy = Path(tmp) / "release"
            shutil.copytree(release, copy)
            mutate(copy)
            res = verify_release(copy, public_key)
            failed = [c[0] for c in res.checks if not c[1]]
            out.append((f"tamper: {name} → FAIL", not res.ok, f"отказали: {failed[:4]}"))
    res = verify_release(release, other_key)
    out.append(("tamper: чужой открытый ключ → FAIL", not res.ok,
                f"отказали: {[c[0] for c in res.checks if not c[1]][:3]}"))  # fmt: skip
    return out


def other_public_key(workdir: Path) -> Path:
    """Посторонняя пара ключей для негативной проверки (одноразовая, удаляется с временным каталогом)."""
    keys = workdir / "other-key"
    keys.mkdir(mode=0o700)
    proc = cosign(keys, ["generate-key-pair"], env={"COSIGN_PASSWORD": "tamper-test-only"})
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-500:])
    return keys / "cosign.pub"


def render(release: Path, result: Result, tamper: list[tuple[str, bool, str]]) -> str:
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))
    overall = result.ok and all(ok for _, ok, _ in tamper)
    lines = [
        "# Supply chain релиза — проверка (P9-08, NFR-SEC-05)",
        "",
        f"**Итог: {'PASS' if overall else 'FAIL'}** · {datetime.now(UTC).isoformat(timespec='seconds')} · "
        f"версия `{manifest['version']}`, коммит `{manifest['source']['commit']}` (dirty={manifest['source']['dirty']}), "
        f"releasable={manifest['releasable']}",
        "",
        "Команды: `uv run python -m scripts.release.pipeline …` → `uv run python -m scripts.release.verify --tamper-test …` "
        "(отчёт сгенерирован скриптом).",
        "",
        "| Образ | Тег | image_id | Архив sha256 | Компонентов SBOM | releasable |",
        "|---|---|---|---|---|---|",
    ]
    for img in manifest["images"]:
        lines.append(f"| {img['name']} | `{img['tag']}` | `{img['image_id'][:19]}…` | `{img['tar_sha256'][:16]}…` "
                     f"| {img['sbom_components']} | {img['releasable']} |")  # fmt: skip
    lines += ["", "## Проверки", "", "| Проверка | Итог | Детали |", "|---|---|---|"]
    for name, ok, detail in [*result.checks, *tamper]:
        lines.append(f"| {name} | {'PASS' if ok else '**FAIL**'} | {detail.replace('|', '/')} |")
    lines += ["", f"Инструменты: `{manifest['tools']['syft']}`, `{manifest['tools']['cosign']}`.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--expect-commit")
    parser.add_argument("--tamper-test", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    release = args.release.resolve()
    result = verify_release(release, args.public_key.resolve(), args.expect_commit)
    tamper: list[tuple[str, bool, str]] = []
    if args.tamper_test:
        with tempfile.TemporaryDirectory(prefix="aicd-otherkey-") as tmp:
            tamper = tamper_tests(release, args.public_key.resolve(), other_public_key(Path(tmp)))
    for name, ok, detail in [*result.checks, *tamper]:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if args.report:
        report = args.report if args.report.is_absolute() else ROOT / args.report
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(render(release, result, tamper), encoding="utf-8")
    overall = result.ok and all(ok for _, ok, _ in tamper)
    print("PASS" if overall else "FAIL")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
