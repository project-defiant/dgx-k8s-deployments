# Local Forgejo security rebuild

This image keeps the official Forgejo 15.0.10 LTS rootless runtime and entrypoint,
updates its installed Alpine packages using its existing signed repositories,
and replaces **both** Go executables with builds using Go 1.26.10. It does not
change passwords, bootstrap an administrator, import an image or deploy a pod.

The official **release-source** archive supplies vendored Go modules and generated
production frontend/bindata. Its reviewed SHA256 covers those assets and vendored
sources; the plain Git archive is unsuitable. The two Go builds run without
network access and use vendored dependencies. Node/npm are not installed because
the frontend is already generated in that official bundle.

Builds are native Linux ARM64 or AMD64. Image digests, source/toolchain archive
hashes and tool versions are recorded. Signed Alpine repository updates are
intentionally refreshed at build time: resulting image digests can differ across
build dates. Installed runtime packages and Go binary/lock provenance are retained
under `/usr/share/thedefiant-build/`. These records are provenance, not an SBOM or
a substitute for scanning the **actual exported image**.

The runtime build refuses libexpat older than 2.9.0-r0. If the configured stable
Alpine repositories do not yet contain that fix, the build fails; it never adds
edge/testing repositories or suppresses the CVE. The final image must pass the
repository CVE gate and service smoke tests before deployment. Compiling this
image does not establish that the host or all upstream code is trustworthy.

```sh
python3 scripts/build-forgejo.py --plan
python3 scripts/build-forgejo.py --ensure
python3 scripts/build-forgejo.py --verify
# Explicit refresh after new Alpine fixes or when auditing cached image updates:
python3 scripts/build-forgejo.py --build
```

`--ensure` reuses a verified artifact only while its committed input fingerprint
matches. A corrupt cached artifact fails closed. `--verify` does not contact Docker
or the network. An optional `--input-cache /path/to/public-archives` reads previously
downloaded source/Go files and verifies the same committed hashes.

Outputs live in `.dev-tools/forgejo/<arm64|amd64>/`: `image.oci.tar`, `record.json`,
`metadata.json`, `values.yaml` and a private `build.log`. The generated values file
is JSON (also valid YAML), pins the actual OCI leaf digest and requires
`image.pullPolicy: Never`. Import and deployment are separate reviewed operations;
the helper never imports images into Kubernetes or talks to its API.

Docker Buildx uses the existing default driver with `--load`, retaining its unique
local Docker tag for smoke tests. `docker image save` is converted into an OCI
archive using Python standard-library code: config/platform/user, every uncompressed
layer diff-ID and final OCI blobs are checked. No archive paths are extracted to
the host. The helper does not create or reconfigure a builder, start privileged
containers, or install host toolchains.
The first build downloads public source/toolchain archives and build/runtime
packages; subsequent Go compilation itself is offline. Review the current private
CVE report after `--ensure`: cached builds cannot anticipate newly published
advisories, and a successful previous scan is not a permanent security verdict.
