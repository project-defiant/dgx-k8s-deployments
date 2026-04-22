.PHONY: $(shell sed -n -e '/^$$/ { n ; /^[^ .\#][^ ]*:/ { s/:.*$$// ; p ; } ; }' $(MAKEFILE_LIST))

.DEFAULT_GOAL := help

help: ## This is help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

minikube-status: ## Check minikube status
	minikube status

minikube-start: ## Start minikube with Docker driver and GPU support
	minikube start --driver docker --container-runtime docker --gpus all || echo "Minikube already running or failed to start"

deploy-vllm--gemma-4-31b: ## Deploy the Gemma 4 31B model via vLLM
	@echo "Creating HF_TOKEN secret..."
	kubectl create secret generic hf-secret --from-literal=HF_TOKEN=${HF_TOKEN} || true
	@echo "Applying RuntimeClass (first time only)..."
	@kubectl apply --save-config -f - <<'EOF'
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
EOF
	@echo "Applying Gemma 4 31B deployment..."
	kubectl apply -f gemma4-31b.yaml

destroy-vllm--gemma-4-31b: ## Destroy the Gemma 4 31B deployment
	@echo "Destroying Gemma 4 31B deployment..."
	kubectl delete -f gemma4-31b.yaml
