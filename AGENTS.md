# AGENTS.md — DGX Kubernetes Deployments

This file provides project-specific context for AI agents working on this repository.

---

## Project Type
Kubernetes infrastructure for GPU-accelerated LLM serving using vLLM.

## Key Conventions

- **Model naming**: Use `vllm--${model-name}` pattern for deployment targets (e.g., `deploy-vllm--gemma-4-31b`)
- **GPU resource**: Always request `nvidia.com/gpu: 1` in resource limits/requests
- **RuntimeClass**: Use `nvidia` for GPU-enabled containers
- **Port convention**: vLLM uses port 8000, service exposes externally on port 80
- **HF cache**: Mount `/root/.cache/huggingface` via PVC (30Gi+ recommended)

## Repository Structure

```
gemma4-31b.yaml     # Kubernetes manifests for Gemma 4 31B
Makefile            # Deployment automation targets
README.md           # Project documentation
AGENTS.md           # This file
```

## Agent Guidelines

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
