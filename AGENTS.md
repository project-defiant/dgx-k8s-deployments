# AGENTS.md — DGX Kubernetes Deployments

This file provides project-specific context for AI agents working on this repository.

---

## Project Type
Kubernetes infrastructure for GPU-accelerated LLM serving using vLLM.

## Key Conventions

- **Model naming**: Use `vllm--${model-name}` pattern for deployment targets (e.g., `deploy-vllm--gemma-4-31b`)
- **GPU resource**: Always request `nvidia.com/gpu: 1` in resource limits/requests
- **RuntimeClass**: Use `nvidia` for GPU-enabled containers
- **Port convention**: vLLM uses port 8000; Service port 80 remains internal (ClusterIP)
- **HF cache**: Mount `/root/.cache/huggingface` via PVC (30Gi+ recommended)

## Repository Structure

```
gemma4-31b.yaml     # Kubernetes manifests for Gemma 4 31B
Makefile            # Deployment automation targets
README.md           # Project documentation
AGENTS.md           # This file
```

## Agent Guidelines

- Run `make dev` before starting an update session. It installs/verifies pinned
  tools, configures worktree-local prek hooks and runs the baseline checks.
- Resolve failed checks before continuing. `make check` repeats the full gate;
  do not bypass hooks, silently skip missing tools, or add blanket scanner ignores.
- Keep credentials, kubeconfigs, host inventory and recovery evidence outside
  public source. Use placeholders in examples, never real tokens or machine IPs.
- Run untrusted pull requests only on disposable hosted CI runners without host
  credentials. Never run the root bootstrap or deploy workloads in PR CI.
- Security/root-executed changes need independent review; preserve operational
  data and existing access. Running checks never authorizes a host deployment.

- When reviewing this repository, always verify:
  - GPU resource requests match available cluster capacity
  - PVC size is sufficient for model weights + cache
  - Reasoning/tool-call parsers match model capabilities
  - Async scheduling is enabled for high-throughput workloads

## Questions the Agent Should Be Ready to Answer

- What commands deploy/destroy the Gemma 4 31B service?
- What ports does the vLLM server listen on?
- How is the HuggingFace token provisioned?
- What RuntimeClass is required for GPU support?
- How much GPU memory is allocated for this model?
