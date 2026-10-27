# Retain the official PostgreSQL entrypoint and binaries; run directly as postgres.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE} AS runtime
ARG PG_VERSION
USER 0:0
RUN apk upgrade --no-cache \
    && test "$(command -v gosu)" = /usr/local/bin/gosu \
    && rm /usr/local/bin/gosu \
    && test "$(id -u postgres)" = 70 \
    && test "$(id -g postgres)" = 70 \
    && sed -i 's/^postgres:x:70:70:/postgres:x:1000:1000:/' /etc/passwd \
    && sed -i 's/^postgres:x:70:/postgres:x:1000:/' /etc/group \
    && chown -R 1000:1000 /var/lib/postgresql /var/run/postgresql \
    && test "$(id -u postgres)" = 1000 \
    && test "$(id -g postgres)" = 1000 \
    && test "$(postgres --version)" = "postgres (PostgreSQL) ${PG_VERSION}"
USER 1000:1000
