# Native builds only. scripts/build-forgejo.py supplies reviewed digest/hash pins.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE} AS patched-runtime
USER 0:0
# Stay on the base image's configured signed Alpine repositories; never mix edge.
RUN apk upgrade --no-cache \
    && expat="$(apk info -v | sed -n 's/^libexpat-//p')" \
    && test -n "$expat" \
    && test "$(apk version -t "$expat" 2.9.0-r0)" != '<' \
    && mkdir -p /usr/share/thedefiant-build \
    && apk info -v > /usr/share/thedefiant-build/runtime-packages.txt

FROM patched-runtime AS builder
USER 0:0
RUN apk add --no-cache build-base git
ARG SOURCE_SHA256
ARG GO_SHA256
COPY source.tar.gz go.tar.gz /tmp/
RUN printf '%s  /tmp/source.tar.gz\n%s  /tmp/go.tar.gz\n' "$SOURCE_SHA256" "$GO_SHA256" | sha256sum -c - \
    && tar -xzf /tmp/go.tar.gz -C /usr/local \
    && mkdir -p /build/source /build/output \
    && tar -xzf /tmp/source.tar.gz --strip-components=1 -C /build/source \
    && rm /tmp/source.tar.gz /tmp/go.tar.gz
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOTOOLCHAIN=local CGO_ENABLED=1 GOMAXPROCS=4 GOPROXY=off TMPDIR=/tmp
WORKDIR /build/source
# The official release-source bundle contains vendored modules and generated
# production frontend/bindata. Never use the plain Git archive in this build.
RUN test -f VERSION && test -f vendor/modules.txt \
    && test -f modules/public/bindata.go \
    && test -f public/assets/js/index.js \
    && test -f public/assets/css/index.css \
    && test "$(go env GOVERSION)" = go1.26.10
RUN --network=none go build -mod=vendor -trimpath -p=4 \
      -o /build/output/environment-to-ini ./contrib/environment-to-ini/environment-to-ini.go \
    && make GOFLAGS='-mod=vendor -trimpath -p=4' RELEASE_VERSION=15.0.10 \
      TAGS='bindata timetzdata sqlite sqlite_unlock_notify' \
      FORGEJO_GENERATE_SKIP_HASH=true go-check static-executable \
    && cp gitea /build/output/gitea \
    && go version -m /build/output/gitea > /build/output/forgejo-go-build.txt \
    && go version -m /build/output/environment-to-ini > /build/output/environment-to-ini-go-build.txt \
    && sha256sum go.mod go.sum package-lock.json vendor/modules.txt > /build/output/source-locks.sha256 \
    && sha256sum /build/output/gitea /build/output/environment-to-ini > /build/output/binaries.sha256

FROM patched-runtime AS runtime
LABEL org.opencontainers.image.version="15.0.10-thedefiant.1" \
      org.opencontainers.image.source="https://codeberg.org/forgejo/forgejo" \
      org.opencontainers.image.description="Local Forgejo LTS security rebuild: Go 1.26.10 and signed Alpine package updates"
COPY --from=builder --chown=0:0 /build/output/gitea /app/gitea/gitea
COPY --from=builder --chown=0:0 /build/output/environment-to-ini /usr/local/bin/environment-to-ini
COPY --from=builder --chown=0:0 /build/output/*.txt /build/output/*.sha256 /usr/share/thedefiant-build/
USER 1000:1000
# Inherit the official rootless entrypoint, UID/GID, work directory and volumes.
