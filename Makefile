SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
ENV ?= dev
DOCS_PORT ?= 8088
export ENV DOCS_PORT
export PATH := $(CURDIR)/.dev-tools/bin:$(PATH)

.PHONY: help dev check preflight bootstrap-check render validate test deploy-docs status access rollback dry-run minikube-status minikube-start deploy deploy-vllm--gemma-4-31b deploy-vllm-gemma-4-31b destroy-vllm--gemma-4-31b destroy-vllm-gemma-4-31b

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "%-35s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev: ## Required before updates: install/verify tools, install hooks and run checks
	python3 scripts/dev-tools.py --hooks
	python3 scripts/build-forgejo.py --ensure
	python3 scripts/checks.py all

check: ## Run all security, manifest and regression checks
	python3 scripts/checks.py all

.PHONY: secrets-encryption-status
secrets-encryption-status: ## Inspect existing K3s Secrets-at-rest encryption (sudo)
	sudo /usr/local/bin/k3s secrets-encrypt status

preflight: ## Inspect the host without changing it
	./scripts/platform-preflight.sh

bootstrap-check: ## Validate bootstrap prerequisites without installing
	./scripts/platform-bootstrap.sh --validate

render: ## Render the docs manifests (ENV=dev|prod)
	./scripts/platform.sh render "$$ENV"

validate: ## Lint and render both environments
	./scripts/platform.sh validate dev
	./scripts/platform.sh validate prod

test: ## Run CLI safety and chart tests without a cluster
	python3 -m unittest discover -s tests -v
	@for script in scripts/*.sh; do bash -n "$$script"; done

deploy-docs: ## Deploy docs to the dedicated K3s context (ENV=dev|prod)
	./scripts/platform.sh deploy "$$ENV"

.PHONY: deploy-forgejo forgejo-status deploy-services render-forgejo build-forgejo-image forgejo-image-import verify-forgejo-runtime
build-forgejo-image: ## Rebuild the patched native Forgejo image with current OS fixes
	python3 scripts/build-forgejo.py --build

forgejo-image-import: ## Import the verified patched image into K3s (requests sudo)
	python3 scripts/forgejo-artifact.py import

verify-forgejo-runtime: ## Test the verified image in disposable Docker resources
	.venv/bin/python scripts/verify-forgejo-runtime.py

deploy-forgejo: ## Deploy the private persistent Forgejo service
	./scripts/platform.sh forgejo-deploy

forgejo-status: ## Inspect Forgejo workloads and retained storage
	./scripts/platform.sh forgejo-status

render-forgejo: ## Render Forgejo manifests without cluster changes
	./scripts/platform.sh forgejo-render

deploy-services: ## Deploy Forgejo, then the shared docs/Forgejo gateway
	$(MAKE) deploy-forgejo
	$(MAKE) deploy-docs

status: ## Inspect the new cluster and docs
	./scripts/platform.sh status "$$ENV"

access: ## Open a localhost-only docs port-forward
	./scripts/platform.sh access "$$ENV"

.PHONY: docs-service-render docs-service-install docs-service-status docs-service-stop docs-service-logs

docs-service-render: ## Preview the systemd unit without installing
	bash ./scripts/platform-service.sh render "$$(id -un)" "$$(command -v kubectl)" "$${PLATFORM_KUBECONFIG:-$$HOME/.kube/spark-platform.yaml}" "$$ENV" "$$DOCS_PORT"

docs-service-install: ## Install/start docs forwarding at boot (sudo required)
	sudo bash ./scripts/platform-service.sh install "$$(id -un)" "$$(command -v kubectl)" "$${PLATFORM_KUBECONFIG:-$$HOME/.kube/spark-platform.yaml}" "$$ENV" "$$DOCS_PORT"

docs-service-status: ## Show the forwarding service status
	systemctl status spark-docs-access.service spark-docs-credentials.timer --no-pager

docs-service-stop: ## Stop forwarding and disable automatic startup
	sudo systemctl disable --now spark-docs-access.service spark-docs-credentials.timer

docs-service-logs: ## Show recent forwarding logs
	sudo journalctl -u spark-docs-access.service -u spark-docs-credentials.service -n 50 --no-pager

rollback: ## Roll back docs (set PLATFORM_REVISION)
	./scripts/platform.sh rollback "$$ENV"

dry-run: ## Validate manifests against K3s (namespace must exist)
	./scripts/platform.sh dry-run "$$ENV"

minikube-status: ## Inspect the legacy cluster
	minikube status

minikube-start: ## Start the legacy cluster explicitly; failures remain visible
	minikube start --driver docker --container-runtime docker --gpus all

deploy: deploy-vllm--gemma-4-31b ## Deploy the legacy Gemma model to Minikube

deploy-vllm-gemma-4-31b: deploy-vllm--gemma-4-31b

deploy-vllm--gemma-4-31b: ## Deploy Gemma to the explicit Minikube context
	@test -n "$${HF_TOKEN:-}" || { echo 'HF_TOKEN is required' >&2; exit 1; }
	@printf '%s' "$$HF_TOKEN" | kubectl --context minikube create secret generic hf-secret --from-file=HF_TOKEN=/dev/stdin --dry-run=client -o yaml | kubectl --context minikube apply -f -
	kubectl --context minikube apply -f gemma4-31b.yaml

destroy-vllm-gemma-4-31b: destroy-vllm--gemma-4-31b

destroy-vllm--gemma-4-31b: ## Delete legacy model resources, including its PVC
	kubectl --context minikube delete -f gemma4-31b.yaml
