"""Образы инструментов security/supply-chain/perf gates, закреплённые по digest (P6-04/06/07).

Ссылка `repository:tag@sha256:digest` — тег информативен, Docker использует digest. `latest` не
используется. Digest — индекс (multi-arch manifest list) из реестра, полученный `docker pull` при
закреплении (2026-11-08); обновление — только сменой этого модуля (ревью + прогон
`tests/security/test_security_gates_docker.py`).

OSV-Scanner: официальный `ghcr.io/google/osv-scanner` недоступен из окружения сборки (blob-хранилище
ghcr закрыто egress-политикой), поэтому образ собирается платформой из официального релизного
бинарника Google, закреплённого по sha256 (`infrastructure/docker/verification/osv-scanner.Dockerfile`).
Его digest — локальный (manifest собранного образа); для профиля server образ публикуется во
внутренний реестр, и здесь фиксируется digest оттуда (OPEN_QUESTIONS).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PinnedImage:
    tool: str
    repository: str
    tag: str
    digest: str  # sha256:…
    source: str  # откуда получен образ (официальный реестр / сборка платформы)

    @property
    def ref(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"


SEMGREP = PinnedImage(
    "semgrep",
    "semgrep/semgrep",
    "1.182.1",
    "sha256:451b3d875c00dac7ed5db5293e584a07de11449a409c10b3c2d2652f429630b6",
    "docker.io (official)",
)
GITLEAKS = PinnedImage(
    "gitleaks",
    "zricethezav/gitleaks",
    "v8.30.1",
    "sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f",
    "docker.io (official)",
)
TRIVY = PinnedImage(
    "trivy",
    "aquasec/trivy",
    "0.76.1",
    "sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c",
    "docker.io (official)",
)
OSV_SCANNER = PinnedImage(
    "osv-scanner",
    "aicd/osv-scanner",
    "v2.7.0",
    "sha256:7aad519192a4821a37d24604c6f1f86ec938cb24e243ea09dbc0ce3e0b7ccbf1",
    "platform build: official release binary v2.7.0 (sha256 160c6253…218b) on distroless static",
)
CHECKOV = PinnedImage(
    "checkov",
    "bridgecrew/checkov",
    "3.4.0",
    "sha256:ece5efac4443084ca273072909985dce988a81e8cc94faa305aeda0df27c68f9",
    "docker.io (official)",
)
CONFTEST = PinnedImage(
    "conftest",
    "openpolicyagent/conftest",
    "v0.71.1",
    "sha256:0e77cdf7c1fcde035f3c579e4aaa6664ee2a8be5777eaa9af9f520cdd02280c0",
    "docker.io (official; OPA 1.21.1 embedded)",
)
SYFT = PinnedImage(
    "syft",
    "anchore/syft",
    "v1.56.0",
    "sha256:697ae864af1299cfa43c54b161cea3dd65c6d54de82e3dba66ea3dccb3acc313",
    "docker.io (official)",
)
COSIGN = PinnedImage(
    "cosign",
    "gcr.io/projectsigstore/cosign",
    "v3.1.4",
    "sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d",
    "gcr.io (official)",
)
PYTHON_BENCH = PinnedImage(
    "python",
    "python",
    "3.12-slim-bookworm",
    "sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df",
    "docker.io (official)",
)

PINNED_IMAGES: dict[str, PinnedImage] = {
    image.tool: image
    for image in (SEMGREP, GITLEAKS, TRIVY, OSV_SCANNER, CHECKOV, CONFTEST, SYFT, COSIGN, PYTHON_BENCH)
}
