# syntax=docker/dockerfile:1
# OSV-Scanner для verification-гейта SCA (P6-04).
#
# Официальный образ ghcr.io/google/osv-scanner недоступен из окружения сборки (blob-хранилище ghcr
# закрыто egress-политикой), поэтому образ собирается из официального релизного бинарника Google
# (GitHub Releases), закреплённого по sha256 из `osv-scanner_SHA256SUMS` релиза v2.7.0, поверх
# distroless static (nonroot), закреплённого по digest. Сборка:
#   docker build -f infrastructure/docker/verification/osv-scanner.Dockerfile \
#     -t aicd/osv-scanner:v2.7.0 infrastructure/docker/verification
# Итоговый image ID фиксируется в aicd_verification/security_images.py и в evidence.
FROM gcr.io/distroless/static-debian12@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
ADD --chmod=0555 --checksum=sha256:160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b \
    https://github.com/google/osv-scanner/releases/download/v2.7.0/osv-scanner_linux_amd64 /usr/local/bin/osv-scanner
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/osv-scanner"]
