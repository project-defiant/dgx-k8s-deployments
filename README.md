# DGX Kubernetes Deployments

Kubernetes manifests for running LLMs on Kubernetes.

## Prerequisites

- `kubectl` installed and configured
- `minikube` with Docker driver and GPU support
- `HF_TOKEN` environment variable set with your HuggingFace token

## Quick Start

```bash
# Start minikube with GPU support
make minikube-start

# Deploy the Gemma 4 31B model
make deploy
```

## Available Make Targets

| Command | Description |
|---------|-------------|
| `make minikube-status` | Check minikube status |
| `make minikube-start` | Start minikube with Docker driver and GPU support |
| `make deploy` | Deploy Gemma 4 31B model |

## Customization

Edit `gemma4-31b.yaml` to adjust:
- GPU memory utilization (`--gpu-memory-utilization`)
- Max model length (`--max-model-len`)
- Replicas
- Resource requests/limits

## Accessing the Model

Once deployed, get the service IP:

```bash
kubectl get svc gemma4-31b-vllm
```

The model will be accessible at `http://<SERVICE_IP>:80/v1/models`.
