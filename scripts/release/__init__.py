"""Конвейер релиза (P9-08, NFR-SEC-05): сборка образов, SBOM (CycloneDX), подпись и provenance (cosign, ключ).

* `uv run python -m scripts.release.pipeline --version X --out dist/release/X --key-dir <ключи>` — сборка;
* `uv run python -m scripts.release.verify --release dist/release/X --public-key cosign.pub [--tamper-test]`.

Ничего не публикуется в реестры и не развёртывается: результат — каталог релиза с архивами образов (`docker save`),
SBOM, подписями (Sigstore bundle) и SLSA-provenance. Подпись — ключом (офлайн: без Fulcio/Rekor), управление
ключом — docs/runbooks/release.md.
"""
