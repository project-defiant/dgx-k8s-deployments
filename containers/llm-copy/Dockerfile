# Data-copy jobs need Python's standard library, never third-party package installers.
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE} AS runtime
USER 0:0
RUN apk upgrade --no-cache \
    && python3 -m pip uninstall -y msgpack urllib3 setuptools wheel pip \
    && python3 -c 'import tarfile, sqlite3, json, hashlib; assert callable(hashlib.file_digest); assert sqlite3.connect(":memory:").execute("select 1").fetchone() == (1,)' \
    && python3 -c 'import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ("msgpack", "urllib3", "setuptools", "wheel", "pip"))'
USER 1000:1000
CMD ["python3", "-c", "import tarfile,sqlite3,json,hashlib; print('stdlib copy helper ready')"]
