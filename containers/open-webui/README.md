# Full Open WebUI security-rebuild candidate

This is a **candidate build**, not permission to migrate or deploy. It retains the
full pinned upstream application/frontend, Python packages, ChromaDB, local embedding
models and upstream `start.sh`. It does not substitute the slim image, remove Chroma,
change embedding model identities, hide package metadata or ignore vulnerability rules.

The fresh Python 3.11 Debian Trixie base retains its interpreter and standard library.
Only the upstream Python site-packages and `/app` are copied. Runtime Debian libraries
and ffmpeg/pandoc are installed without compilers or development headers. Exact Python
upgrade versions come from the upstream image's scan report. Binary-only package
installation, dependency checks, full embedding/audio imports and unresolved-library
checks must pass. Minimal console wrappers invoke the new interpreter for upstream
startup commands; old Python executables are never copied.

```sh
.venv/bin/python scripts/build-webui.py --plan
.venv/bin/python scripts/build-webui.py --build
.venv/bin/python scripts/build-webui.py --verify
```

Only reviewed native architecture digests are accepted. The current inputs cover
ARM64; AMD64 requires its own reviewed upstream and Python image digests. BuildKit,
Docker and the repository's pinned Trivy tool must already be available. Building
does not import an image into K3s, mount live WebUI data or change a deployment.

Artifacts/logs stay under ignored, private `.dev-tools/open-webui/<architecture>`.
The builder reuses the existing verified Docker-to-OCI conversion. It requires
actual OS and full Python embedding/vector package inventory and **zero HIGH or
CRITICAL findings** before publishing an OCI record or Helm overlay. There is no
scan-bypass or ignore-unfixed switch. A failed candidate leaves its private report
and uniquely tagged local Docker image for investigation, with no approved record.
`--verify` checks the inputs/helper fingerprint, archive bytes, OCI blobs, scan bytes,
scan result and generated values; a successful rebuild changes the immutable digest.

Known unresolved upstream Chroma advisories are recorded in `inputs.lock.json` for
visibility, **not as exceptions**. The source scan reports no fixed release for
CVE-2026-45829, CVE-2026-45833, CVE-2026-45830 and CVE-2026-45831. Transformers also
needs rechecking against CVE-2026-80047. Replacing Debian and upgrading packages
with known fixes does not establish that these blockers disappear. A strict build
may therefore remain blocked even if compilation succeeds.

A successful image scan is still insufficient for migration: test restored copies
of the existing SQLite database, secret key, Chroma collections, document ingestion,
local embeddings and model connectivity in an isolated environment first. Never let
the newer release migrate the only copy of existing data.

Debian security updates and transitive wheel resolution are intentionally refreshed
at build time. This is not a claim of bit-for-bit reproducibility. Review the resulting
package inventory and scan; trust an approved final artifact digest, not a mutable tag.

## Current build blocker

The first actual candidate build stopped at dependency resolution: `unstructured
0.24.0` requires `filelock>=3.12.0,<4.0.0`, while the security upgrade requires
`filelock==4.2.1`. Upstream Unstructured's current main branch retains that upper
bound. The filelock advisory lists 4.2.1 as its patched release; a pre-3.20.4
downgrade would avoid one newly introduced feature, but is not a verified security
backport and is not selected here. The resolver failure remains enforced.

References: [Unstructured dependency metadata](https://github.com/Unstructured-IO/unstructured/blob/main/pyproject.toml),
[filelock security advisory](https://github.com/tox-dev/filelock/security/advisories/GHSA-v4x8-3wr5-3wq8).

Do not use `--no-deps`, edit installed dependency metadata, remove document ingestion,
or ignore dependency checks to bypass this conflict. A compatible maintained release
or an explicitly reviewed upstream-compatible patch is needed before rebuilding.
