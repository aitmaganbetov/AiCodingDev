# Supply chain релиза — проверка (P9-08, NFR-SEC-05)

**Итог: PASS** · 2026-11-08T18:16:09+00:00 · версия `0.1.0-rc2`, коммит `38c9e731453bfcc2de4c5f482e1ffeb53dcbc581` (dirty=True), releasable=False

Команды: `uv run python -m scripts.release.pipeline …` → `uv run python -m scripts.release.verify --tamper-test …` (отчёт сгенерирован скриптом).

| Образ | Тег | image_id | Архив sha256 | Компонентов SBOM | releasable |
|---|---|---|---|---|---|
| backend | `aicodingdev/backend:0.1.0-rc2` | `sha256:0e071e6910ac…` | `2b4372dc112d5b3a…` | 3555 | False |
| web | `aicodingdev/web:0.1.0-rc2` | `sha256:d864c2b1b8c2…` | `b9105876e6aec245…` | 1305 | True |

## Проверки

| Проверка | Итог | Детали |
|---|---|---|
| SHA256SUMS: подпись ключом релиза | PASS | Verified OK |
| SHA256SUMS: все файлы релиза совпадают | PASS |  |
| cosign.pub релиза = доверенный ключ | PASS | 04fffdda885e4559 |
| коммит релиза = ожидаемый | PASS | 38c9e731453bfcc2de4c5f482e1ffeb53dcbc581 |
| backend: подпись архива образа | PASS | Verified OK |
| backend: SLSA provenance (подпись, subject = архив образа) | PASS | Verified OK |
| backend: provenance согласован (тип, subject, commit, image_id) | PASS | commit 38c9e731453b, dirty=True, releasable=False |
| backend: SBOM-аттестация CycloneDX образа | PASS | Verified OK |
| web: подпись архива образа | PASS | Verified OK |
| web: SLSA provenance (подпись, subject = архив образа) | PASS | Verified OK |
| web: provenance согласован (тип, subject, commit, image_id) | PASS | commit 38c9e731453b, dirty=True, releasable=True |
| web: SBOM-аттестация CycloneDX образа | PASS | Verified OK |
| SBOM sbom/backend-source.cdx.json: подпись, CycloneDX, 132 компонентов | PASS | Verified OK |
| SBOM sbom/web-source.cdx.json: подпись, CycloneDX, 590 компонентов | PASS | Verified OK |
| SBOM sbom/backend-image.cdx.json: подпись, CycloneDX, 3555 компонентов | PASS | Verified OK |
| SBOM sbom/web-image.cdx.json: подпись, CycloneDX, 1305 компонентов | PASS | Verified OK |
| tamper: подмена байта в архиве образа → FAIL | PASS | отказали: ['SHA256SUMS: все файлы релиза совпадают', 'backend: подпись архива образа', 'backend: SLSA provenance (подпись, subject = архив образа)', 'backend: SBOM-аттестация CycloneDX образа'] |
| tamper: изменение SBOM → FAIL | PASS | отказали: ['SHA256SUMS: все файлы релиза совпадают', 'SBOM sbom/backend-source.cdx.json: подпись, CycloneDX, 132 компонентов'] |
| tamper: изменение SHA256SUMS → FAIL | PASS | отказали: ['SHA256SUMS: подпись ключом релиза', 'SHA256SUMS: все файлы релиза совпадают'] |
| tamper: provenance другого образа → FAIL | PASS | отказали: ['SHA256SUMS: все файлы релиза совпадают', 'backend: SLSA provenance (подпись, subject = архив образа)'] |
| tamper: чужой открытый ключ → FAIL | PASS | отказали: ['SHA256SUMS: подпись ключом релиза', 'cosign.pub релиза = доверенный ключ', 'backend: подпись архива образа'] |

Инструменты: `anchore/syft:v1.56.0@sha256:697ae864af1299cfa43c54b161cea3dd65c6d54de82e3dba66ea3dccb3acc313`, `gcr.io/projectsigstore/cosign:v3.1.4@sha256:0f06154836d2de34f887a5c273506d52ca43edaa1d9e53e6510fa78a48f69c2d`.
