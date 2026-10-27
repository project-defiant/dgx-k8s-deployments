# Full upstream application and local embedding/vector capabilities are retained.
ARG UPSTREAM_IMAGE
ARG PYTHON_IMAGE
FROM ${UPSTREAM_IMAGE} AS upstream
FROM ${PYTHON_IMAGE} AS runtime
USER 0:0
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       ca-certificates git curl jq pandoc ffmpeg libsm6 libxext6 libmariadb3 libgomp1 libatomic1 libgl1 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 webui && useradd --uid 1000 --gid 1000 --create-home webui
# Never copy the old Python executable or its libpython: retain the patched base interpreter.
COPY --from=upstream /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=upstream --chown=1000:1000 /app /app
COPY patches.txt /tmp/patches.txt
RUN python3 -m pip install --no-cache-dir --only-binary=:all: --upgrade -r /tmp/patches.txt \
    && python3 -m pip check \
    && python3 -c 'import chromadb, sentence_transformers, transformers, torch, torchvision, torchaudio, av, pypdf, unstructured, uvicorn, fastapi' \
    && python3 -m pip freeze --all > /usr/local/share/webui-python-packages.txt \
    && find /usr/local/lib/python3.11/site-packages -type f -name '*.so*' -print0 \
       | xargs -0 -r sh -c 'for f do if ldd "$f" 2>/dev/null | grep -q "not found"; then printf "Unresolved shared libraries: %s\n" "$f" >&2; exit 1; fi; done' sh \
    && printf '#!/bin/sh\nexec python3 -m uvicorn "$@"\n' > /usr/local/bin/uvicorn \
    && printf '#!/bin/sh\nexec python3 -m gunicorn "$@"\n' > /usr/local/bin/gunicorn \
    && chmod 0755 /usr/local/bin/uvicorn /usr/local/bin/gunicorn \
    && rm /tmp/patches.txt
# Preserve the pinned full upstream model identities, cache locations and startup flags.
ENV ENV=prod PORT=8080 HOME=/home/webui PYTHONUNBUFFERED=1 DOCKER=true \
    USE_OLLAMA_DOCKER=false USE_CUDA_DOCKER=false USE_SLIM_DOCKER=false \
    USE_CUDA_DOCKER_VER=cu128 \
    USE_EMBEDDING_MODEL_DOCKER=sentence-transformers/all-MiniLM-L6-v2 \
    USE_RERANKING_MODEL_DOCKER="" \
    USE_AUXILIARY_EMBEDDING_MODEL_DOCKER=TaylorAI/bge-micro-v2 \
    OLLAMA_BASE_URL=/ollama OPENAI_API_BASE_URL="" OPENAI_API_KEY="" \
    DATA_DIR=/app/backend/data \
    SCARF_NO_ANALYTICS=true DO_NOT_TRACK=true ANONYMIZED_TELEMETRY=false \
    RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2 \
    RAG_RERANKING_MODEL="" AUXILIARY_EMBEDDING_MODEL=TaylorAI/bge-micro-v2 \
    SENTENCE_TRANSFORMERS_HOME=/app/backend/data/cache/embedding/models \
    HF_HOME=/app/backend/data/cache/embedding/models \
    TIKTOKEN_ENCODING_NAME=cl100k_base TIKTOKEN_CACHE_DIR=/app/backend/data/cache/tiktoken \
    WHISPER_MODEL=base WHISPER_MODEL_DIR=/app/backend/data/cache/whisper/models \
    WEBUI_BUILD_HASH=91ea4e3421d1d8069f13d4a463fcb5986d94ea60
WORKDIR /app/backend
USER 1000:1000
EXPOSE 8080
# Keep upstream secret-generation, migrations and startup behavior. Console wrappers
# invoke the fresh interpreter rather than copying executables from the old image.
CMD ["bash", "start.sh"]
