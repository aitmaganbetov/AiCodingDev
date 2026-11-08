"""Сборка релиза: образы → `docker save` → SBOM (CycloneDX, syft) → подписи и SLSA-provenance (cosign, ключ).

    uv run python -m scripts.release.pipeline --version 0.1.0-rc1 --out dist/release/0.1.0-rc1 \\
        --key-dir ~/aicd-release-keys --password-file ~/aicd-release-keys/password [--generate-key] \\
        [--ca-bundle /path/ca.pem] [--no-git-in-backend]

Результат (каталог `--out`): `images/<имя>.tar` (+ `.sigstore.json`), `sbom/*.cdx.json` (+ подписи),
`provenance/<имя>.provenance.json` (предикат SLSA v1), `attestations/<имя>.{provenance,sbom}.sigstore.json`
(DSSE in-toto, подписанные), `release-manifest.json`, `SHA256SUMS` (+ подпись), `cosign.pub`.
Инструменты — образы syft/cosign из `aicd_verification.tools.images` (по digest), сеть контейнеров — none.
Реестры не используются, ничего не публикуется.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aicd_verification.tools.images import COSIGN, SYFT
from scripts.release.common import (
    BUILD_TYPE,
    IMAGES,
    OFFLINE_SIGN,
    ROOT,
    SLSA_PREDICATE_TYPE,
    ReleaseError,
    cosign,
    must,
    run,
    sha256_file,
    syft,
)

FROM_RE = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_state() -> dict[str, Any]:
    commit = must(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()
    dirty = [line for line in must(["git", "-C", str(ROOT), "status", "--porcelain"]).splitlines() if line.strip()]
    remote = run(["git", "-C", str(ROOT), "remote", "get-url", "origin"]).stdout.strip()
    return {"commit": commit, "dirty": bool(dirty), "dirty_paths": len(dirty), "remote": remote or None}


def base_images(dockerfile: Path) -> list[dict[str, Any]]:
    deps = []
    for ref in FROM_RE.findall(dockerfile.read_text(encoding="utf-8")):
        if "@sha256:" not in ref:
            raise ReleaseError(f"{dockerfile.name}: базовый образ {ref} не закреплён по digest (NFR-SEC-05)")
        name, digest = ref.split("@sha256:", 1)
        deps.append({"uri": f"docker://{name}", "digest": {"sha256": digest}})
    return deps


def ensure_keys(key_dir: Path, password: str, generate: bool) -> None:
    if (key_dir / "cosign.key").is_file() and (key_dir / "cosign.pub").is_file():
        return
    if not generate:
        raise ReleaseError(f"нет {key_dir}/cosign.key — создайте ключ (--generate-key) или укажите --key-dir")
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    proc = cosign(key_dir, ["generate-key-pair"], env={"COSIGN_PASSWORD": password})
    if proc.returncode != 0:
        raise ReleaseError(f"cosign generate-key-pair: {proc.stderr[-800:]}")
    (key_dir / "cosign.key").chmod(0o600)


def build_image(name: str, version: str, git: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    dockerfile = ROOT / IMAGES[name]
    tag = f"aicodingdev/{name}:{version}"
    build_args: dict[str, str] = {}
    if name == "backend" and args.no_git_in_backend:
        build_args["AICD_INSTALL_GIT"] = "0"
    cmd = ["docker", "build", "-f", str(dockerfile), "-t", tag]
    for key, value in build_args.items():
        cmd += ["--build-arg", f"{key}={value}"]
    if args.ca_bundle:
        cmd += ["--secret", f"id=ca_bundle,src={args.ca_bundle}"]
    labels = {
        "org.opencontainers.image.title": f"aicodingdev-{name}",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.revision": git["commit"],
        "org.opencontainers.image.licenses": "GPL-3.0-only",
    }
    for key, value in labels.items():
        cmd += ["--label", f"{key}={value}"]
    started = now()
    proc = run([*cmd, str(ROOT)], timeout=3600)
    (args.out / "logs").mkdir(exist_ok=True)
    (args.out / "logs" / f"build-{name}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise ReleaseError(f"docker build {name} → {proc.returncode}; лог: logs/build-{name}.log")
    image_id = must(["docker", "image", "inspect", "--format", "{{.Id}}", tag]).strip()
    tar = args.out / "images" / f"{name}.tar"
    tar.parent.mkdir(exist_ok=True)
    must(["docker", "save", "-o", str(tar), tag], timeout=1800)
    return {
        "name": name,
        "tag": tag,
        "dockerfile": IMAGES[name],
        "build_args": build_args,
        "image_id": image_id,
        "tar": f"images/{name}.tar",
        "tar_sha256": sha256_file(tar),
        "base_images": base_images(dockerfile),
        "releasable": not build_args.get("AICD_INSTALL_GIT") == "0",
        "started_on": started,
        "finished_on": now(),
    }


def provenance(image: dict[str, Any], git: dict[str, Any], version: str, invocation: str) -> dict[str, Any]:
    deps = list(image["base_images"])
    for lock in ("uv.lock", "apps/web/pnpm-lock.yaml"):
        deps.append({"uri": f"git+file:{lock}", "digest": {"sha256": sha256_file(ROOT / lock)}})
    docker_version = run(["docker", "version", "--format", "{{.Server.Version}}"]).stdout.strip()
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {
                "source": {"uri": git["remote"] or "git+file:.", "digest": {"gitCommit": git["commit"]},
                           "dirty": git["dirty"], "dirty_paths": git["dirty_paths"]},  # fmt: skip
                "dockerfile": image["dockerfile"],
                "context": ".",
                "version": version,
                "build_args": image["build_args"],
            },
            "internalParameters": {"docker_server": docker_version, "releasable": image["releasable"]},
            "resolvedDependencies": deps,
        },
        "runDetails": {
            "builder": {"id": f"https://aicodingdev.local/release/scripts/release/pipeline.py@{git['commit']}"},
            "metadata": {"invocationId": invocation, "startedOn": image["started_on"], "finishedOn": image["finished_on"]},
            "byproducts": [{"name": "image_id", "digest": {"sha256": image["image_id"].removeprefix("sha256:")}},
                           {"name": "image_tag", "uri": f"docker://{image['tag']}"}],  # fmt: skip
        },
    }


def sign(out: Path, key_dir: Path, password: str, rel: str, bundle: str) -> None:
    proc = cosign(out, ["sign-blob", "--key", "/keys/cosign.key", "--bundle", bundle, *OFFLINE_SIGN, rel],
                  env={"COSIGN_PASSWORD": password}, mounts={key_dir: "/keys"})  # fmt: skip
    if proc.returncode != 0:
        raise ReleaseError(f"cosign sign-blob {rel}: {proc.stderr[-800:]}")


def attest(out: Path, key_dir: Path, password: str, rel: str, predicate: str, kind: str, bundle: str) -> None:
    args = ["attest-blob", "--key", "/keys/cosign.key", "--predicate", predicate, "--type", kind, "--bundle", bundle]
    proc = cosign(out, [*args, *OFFLINE_SIGN, rel], env={"COSIGN_PASSWORD": password}, mounts={key_dir: "/keys"})
    if proc.returncode != 0:
        raise ReleaseError(f"cosign attest-blob {rel} ({kind}): {proc.stderr[-800:]}")


def source_sboms(out: Path) -> list[str]:
    """SBOM исходников: backend — pyproject.toml + uv.lock, web — package.json + pnpm-lock.yaml."""
    produced = []
    with tempfile.TemporaryDirectory(prefix="aicd-sbom-") as tmp:
        for name, files in (("backend-source", ("pyproject.toml", "uv.lock")),
                            ("web-source", ("apps/web/package.json", "apps/web/pnpm-lock.yaml"))):  # fmt: skip
            src = Path(tmp) / name
            src.mkdir()
            for rel in files:
                shutil.copy(ROOT / rel, src / Path(rel).name)
            src.chmod(0o755)
            for f in src.iterdir():
                f.chmod(0o644)
            syft(out, "dir:/src", f"sbom/{name}.cdx.json", mounts={src: "/src"})
            produced.append(f"sbom/{name}.cdx.json")
    return produced


def components(path: Path) -> int:
    return len(json.loads(path.read_text(encoding="utf-8")).get("components", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--key-dir", type=Path, required=True, help="каталог cosign.key/cosign.pub (вне репозитория)")
    parser.add_argument("--password-file", type=Path, required=True, help="пароль закрытого ключа cosign")
    parser.add_argument("--generate-key", action="store_true", help="создать пару ключей, если её нет")
    parser.add_argument("--ca-bundle", type=Path, help="CA корпоративного прокси для сборки (в образ не попадает)")
    parser.add_argument("--no-git-in-backend", action="store_true",
                        help="AICD_INSTALL_GIT=0: только для проверки конвейера без доступа к deb.debian.org; releasable=false")  # fmt: skip
    parser.add_argument("--images", default="backend,web")
    args = parser.parse_args(argv)
    if not VERSION_RE.fullmatch(args.version):
        raise SystemExit("некорректная версия")
    args.out = args.out.resolve()
    if args.out.exists() and any(args.out.iterdir()):
        raise SystemExit(f"{args.out} не пуст: каталог релиза неизменяем, укажите новый")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "sbom").mkdir()
    (args.out / "provenance").mkdir()
    (args.out / "attestations").mkdir()
    password = args.password_file.read_text(encoding="utf-8").strip()
    ensure_keys(args.key_dir.resolve(), password, args.generate_key)
    key_dir = args.key_dir.resolve()
    git = git_state()
    invocation = str(uuid.uuid4())
    started = now()
    images = []
    for name in [n.strip() for n in args.images.split(",") if n.strip()]:
        if name not in IMAGES:
            raise SystemExit(f"неизвестный образ {name}")
        print(f"[release] build {name}", flush=True)
        image = build_image(name, args.version, git, args)
        syft(args.out, f"docker-archive:/w/{image['tar']}", f"sbom/{name}-image.cdx.json")
        image["sbom"] = f"sbom/{name}-image.cdx.json"
        image["sbom_components"] = components(args.out / image["sbom"])
        prov_rel = f"provenance/{name}.provenance.json"
        (args.out / prov_rel).write_text(json.dumps(provenance(image, git, args.version, invocation), indent=2))
        print(f"[release] sign/attest {name}", flush=True)
        sign(args.out, key_dir, password, image["tar"], f"{image['tar']}.sigstore.json")
        attest(args.out, key_dir, password, image["tar"], prov_rel, "slsaprovenance1",
               f"attestations/{name}.provenance.sigstore.json")  # fmt: skip
        attest(args.out, key_dir, password, image["tar"], image["sbom"], "cyclonedx",
               f"attestations/{name}.sbom.sigstore.json")  # fmt: skip
        image["signature"] = f"{image['tar']}.sigstore.json"
        image["provenance"] = prov_rel
        image["attestations"] = {"slsaprovenance1": f"attestations/{name}.provenance.sigstore.json",
                                 "cyclonedx": f"attestations/{name}.sbom.sigstore.json"}  # fmt: skip
        images.append(image)
    sboms = source_sboms(args.out) + [img["sbom"] for img in images]
    for rel in sboms:
        sign(args.out, key_dir, password, rel, f"{rel}.sigstore.json")
    shutil.copy(key_dir / "cosign.pub", args.out / "cosign.pub")
    manifest = {
        "format": "aicd-release-manifest/1",
        "version": args.version,
        "source": git,
        "started_on": started,
        "finished_on": now(),
        "invocation_id": invocation,
        "predicate_type": SLSA_PREDICATE_TYPE,
        "tools": {"syft": SYFT.ref, "cosign": COSIGN.ref},
        "images": images,
        "sboms": {rel: {"components": components(args.out / rel), "signature": f"{rel}.sigstore.json"} for rel in sboms},
        "releasable": all(img["releasable"] for img in images) and not git["dirty"],
        "public_key_sha256": sha256_file(key_dir / "cosign.pub"),
    }
    (args.out / "release-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    sums = []
    for path in sorted(p for p in args.out.rglob("*") if p.is_file()):
        rel = path.relative_to(args.out).as_posix()
        if rel.startswith("SHA256SUMS") or rel.startswith("logs/"):
            continue
        sums.append(f"{sha256_file(path)}  {rel}")
    (args.out / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    sign(args.out, key_dir, password, "SHA256SUMS", "SHA256SUMS.sigstore.json")
    print(json.dumps({"ok": True, "out": str(args.out), "releasable": manifest["releasable"],
                      "images": {i["name"]: i["image_id"] for i in images}}, indent=2))  # fmt: skip
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ReleaseError as exc:
        print(f"[release] ERROR {exc}", file=sys.stderr)
        sys.exit(1)
