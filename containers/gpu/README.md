# Patched single-Spark NVIDIA device plugin

The upstream v0.20.1 image failed the image gate. This build retains NVIDIA's
source commit and updates the compiler to Go 1.27.3 and x/net to 0.60.0. The
runtime uses the same reviewed Ubuntu-based image as Ollama, sharing its layers
and avoiding the vulnerable upstream runtime packages. Only the plugin is built;
GPU sharing, MPS and feature discovery are not enabled.

`inputs.lock.json` pins source and compiler URLs/hashes and the runtime digest.
Place their verified archives under `.dev-tools/gpu-inputs/source.tar.gz` and
`go.tar.gz`, then run `make gpu-image-build`. Downloads must match the locked
hashes; the builder checks them again inside the build container. Source module
checksums are verified by Go's checksum database. The final scanner must inventory
the plugin with the expected patched compiler and x/net, plus OS packages and
zero unapproved HIGH/CRITICAL findings.

Artifacts are private under `.dev-tools/gpu/arm64`. `make gpu-image-import`
verifies provenance/archive hashes, freshly scans actual OCI bytes and requests
sudo only for fixed K3s import/tag commands. It never starts a workload.

The image default is UID1000. The reviewed DaemonSet explicitly selects UID0,
with all capabilities dropped and a read-only root, only to use the exact kubelet
device-plugin socket directory. The CLI locks the complete manifest bytes and
requires exactly one advertised GPU after rollout. Rebuilding requires reviewing
the new digest in that manifest and updating its checksum binding.

See [GPU operations](../../docs/platform/gpu.md) for `make ollama-up` and rollback.
