# Reviewed security exceptions

Scanner warnings are not globally suppressed. Detailed reports remain private
under `.dev-tools/reports`; the gate rejects unapproved HIGH/CRITICAL findings
and every detected secret. Lower-severity policy warnings remain visible for
review. The current docs and Forgejo images require no image vulnerability
exceptions. The old docs-image assessment is retained below for review.

## Legacy writable GPU container

- **Rule:** Trivy KSV-0014, HIGH writable root filesystem.
- **Scope:** only `gemma4-31b.yaml`, container and Deployment named
  `gemma4-31b-vllm`, with the matching writable-rootfs message. No other resource,
  rule or CRITICAL finding is covered.
- **Owner:** repository maintainer.
- **Review/expiry:** 2026-11-23; the gate fails closed after this date.
- **Reason:** the existing GPU image uses a root Hugging Face cache and its
  CUDA/cache startup behavior has not been validated under read-only/non-root
  settings. It is manual opt-in and is not applied by the platform docs workflow.
- **Controls:** immutable verified ARM64 digest, internal ClusterIP, no service
  account token mount, dropped capabilities, blocked privilege escalation,
  RuntimeDefault seccomp and no hostPath mount.
- **Remaining risk:** root/non-root compatibility, writable image filesystem,
  unified-memory budget and full large-image vulnerability inventory are still
  unverified. Private access is not authentication if a wider route is added.
- **Removal:** validate a supported non-root configuration with explicit writable
  cache/tmp paths, scan its actual image, and prove inference startup before
  enabling the restrictive settings and removing the exception.

This exception is a time-bounded compatibility decision, not a clean-security
claim or permission to enable an untested GPU workload.

## Docs-only Brotli non-reachability assessment

**Historical image assessment:** the current gateway uses the official
`stable-alpine-slim` ARM64 image pinned at
`sha256:fe21747ebdea229a1dd6415362bf3ad626b70ec36ccffc19a9a3eb7af54a4f9e`.
Its scan passes without this exception. The old full image also acquired
CVE-2026-92392 in curl/libcurl; those findings are not waived. The narrow old-image
assessment remains recorded for review and never covers the new image.

- **Finding:** CVE-2026-111497, `brotli-libs` version `1.2.0-r1`, HIGH only.
- **Image:** only `docker.io/nginxinc/nginx-unprivileged@sha256:4d831b13e3a78d153ba888f43a784a519944cb8f65a0fad07219a42e5db59456`.
- **Owner:** repository maintainer; assessed 2026-10-24; expires **2026-11-07**.
- **Justification:** vulnerable code is not in the docs request execution path.
  Inspection found no Brotli nginx build flag, no loaded Brotli library in the
  running nginx process and no decoder/module configuration. The active app is
  nginx, not a general-purpose Brotli client. The shared-host proxy configuration
  forwards bytes and does not add Brotli decoding.
- **Invalidation:** the gate binds this assessment to exact deployment/config
  templates, nginx configuration and production overrides, and checks rendered
  command/arguments. Changing executable behavior/configuration requires review.
  HTML link/content changes alone do not silently alter the executable profile.
- **Configuration review (2026-10-24):** raising only the Forgejo host request
  body limit to 40 GiB preserves streaming and introduces no Brotli module,
  decoder or executable change. The exact configuration hash was renewed;
  image, finding scope and expiry remain unchanged.
- **Gateway review (2026-10-25):** the Ollama hostname uses a fixed private
  upstream, forwards bytes with buffering disabled and introduces no Brotli
  module/decoder. Only the configuration hash was renewed; the exact image,
  finding scope and expiry remain unchanged.
- **Local-client review (2026-10-25):** the `/ollama/` prefix uses a built-in
  URI map to the same fixed Ollama upstream. It preserves raw paths/queries and
  streaming, without decompression or executable changes. The configuration hash
  was renewed; image, finding scope and expiry remain unchanged.
- **Scope:** only full docs-manifest checks with that verified profile can use
  the exception. A standalone image scan, other image/package/version/CVE,
  CRITICAL severity, or expired assessment still fails. The original finding is
  retained in the report, not removed or globally ignored.
- **Removal:** update to a fixed official image when available and rerun runtime
  checks. This is not a claim that the package is patched or safe for other uses.

References: [Debian advisory](https://lists.debian.org/debian-security-announce/2026/msg00498.html),
[upstream advisory](https://github.com/advisories/GHSA-9v8h-qqgm-4m35).

## Lower-severity scanner policy warnings

Trivy's default registry policy may flag the reviewed official nginx image until
a project-specific registry policy is provided. Its exact digest is independently
verified and the automatic image gate requires package inventory plus no reported
HIGH/CRITICAL CVEs. Quota rules can also flag non-quota objects despite the
namespace ResourceQuota being present; schema/render tests and live quota checks
are separate evidence. Keep the reports visible rather than adding a global ignore.
