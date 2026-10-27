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

.PHONY: docs-status
docs-status: ## Inspect only docs resources (ENV=dev|prod)
	./scripts/platform.sh docs-status "$$ENV"

status: ## Inspect workloads, services and storage across all K3s namespaces
	./scripts/platform.sh status "$$ENV"

access: ## Open a localhost-only docs port-forward
	./scripts/platform.sh access "$$ENV"

.PHONY: llm-backup-plan llm-backup-render llm-backup-deploy llm-backup-status llm-backup-migrate llm-backup-start llm-backup-stop
llm-backup-prepare-storage: ## Prepare retained PVCs and isolated copy networking without app workloads
	.venv/bin/python scripts/llm-backup.py prepare-storage

llm-backup-plan: ## Describe retained model/UI storage and standby migration
	.venv/bin/python scripts/llm-backup.py plan

llm-backup-render: ## Render standby workloads using the reviewed local image overlay
	.venv/bin/python scripts/llm-backup.py render

llm-backup-deploy: ## Scan selected images and install zero-replica standby workloads
	.venv/bin/python scripts/llm-backup.py deploy

llm-backup-status: ## Inspect standby workloads and their retained PVCs
	.venv/bin/python scripts/llm-backup.py status

llm-backup-migrate: ## Copy existing Ollama/WebUI state into empty PVCs; stops originals
	.venv/bin/python scripts/llm-backup.py migrate

llm-backup-start: ## Start migrated standby services after GPU and source-state checks
	.venv/bin/python scripts/llm-backup.py start

llm-backup-stop: ## Stop standby services while retaining all data
	.venv/bin/python scripts/llm-backup.py stop

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

.PHONY: build-webui-image gpu-plan gpu-status gpu-deploy llm-backup-prepare-storage
build-webui-image: ## Build the full patched WebUI candidate; refuses dependency or security failures
	.venv/bin/python scripts/build-webui.py --build

gpu-plan: ## Describe the reviewed Kubernetes GPU addon
	.venv/bin/python scripts/platform-gpu.py plan

gpu-status: ## Check device-plugin readiness and one advertised GPU
	.venv/bin/python scripts/platform-gpu.py status

gpu-deploy: ## Validate and scan the GPU addon before deployment
	.venv/bin/python scripts/platform-gpu.py deploy

.PHONY: llm-copy-image-build llm-copy-image-import
llm-copy-image-build: ## Build and scan the minimal migration helper
	.venv/bin/python scripts/llm-copy-image.py --build

llm-copy-image-import: ## Rescan and import the verified copy helper into K3s (sudo)
	.venv/bin/python scripts/llm-copy-image.py --import

.PHONY: llm-backup-copy-data
llm-backup-copy-data: ## Import the verified helper and copy UI/models to empty PVCs; stops originals (interactive sudo)
	$(MAKE) llm-backup-prepare-storage
	$(MAKE) llm-copy-image-import
	.venv/bin/python scripts/llm-backup.py migrate webui
	.venv/bin/python scripts/llm-backup.py migrate ollama
	@echo 'Data copied; originals are stopped and retained. Application cutover remains gated.'
	@echo 'To resume the original services while standby is stopped: sudo systemctl start ollama; docker start open-webui'

.PHONY: gpu-image-build gpu-image-import deploy-ollama start-ollama ollama-access ollama-up
gpu-image-build: ## Build and scan the patched native NVIDIA device plugin
	.venv/bin/python scripts/gpu-image.py --build

gpu-image-import: ## Verify, scan and import the patched GPU image into K3s (sudo)
	.venv/bin/python scripts/gpu-image.py --import

deploy-ollama: ## Prepare only Ollama in K3s using the migrated PVC
	.venv/bin/python scripts/llm-backup.py deploy-ollama

start-ollama: ## Start Ollama alone and wait for K3s GPU readiness
	.venv/bin/python scripts/llm-backup.py start-ollama

ollama-access: ## Forward the K3s Ollama API to localhost:11434
	.venv/bin/python scripts/llm-backup.py access-ollama

ollama-up: ## Import the GPU image (sudo), enable GPU scheduling and start only Ollama
	$(MAKE) gpu-image-import
	$(MAKE) gpu-deploy
	$(MAKE) deploy-ollama
	sudo systemctl disable --now ollama
	$(MAKE) start-ollama

.PHONY: stop-ollama
stop-ollama: ## Stop only the K3s Ollama workload, retaining its PVC
	.venv/bin/python scripts/llm-backup.py stop-ollama

.PHONY: deploy-ollama-gateway
deploy-ollama-gateway: ## Route Ollama through the persistent private gateway on port 8088
	.venv/bin/python scripts/llm-backup.py refresh-network
	$(MAKE) deploy-docs

.PHONY: ollama-env
ollama-env: ## Print the local K3s Ollama client setting for the current shell
	@printf '%s\n' 'export OLLAMA_HOST=http://127.0.0.1:8088/ollama'

.PHONY: postgres-image-build postgres-image-import deploy-postgres postgres-status postgres-access postgres-connection postgres-password postgres-shell postgres-service-render postgres-service-install postgres-service-status postgres-up
postgres-image-build: ## Build and scan the patched native PostgreSQL 18 image
	.venv/bin/python scripts/postgres-image.py --build

postgres-image-import: ## Rescan and import the verified PostgreSQL image into K3s (sudo)
	.venv/bin/python scripts/postgres-image.py --import

deploy-postgres: ## Deploy the persistent private development PostgreSQL service
	.venv/bin/python scripts/postgres.py deploy

postgres-status: ## Inspect the development database and retained storage
	.venv/bin/python scripts/postgres.py status

postgres-access: ## Forward PostgreSQL to localhost:5432 until interrupted
	.venv/bin/python scripts/postgres.py access

postgres-connection: ## Show connection details and create a private pgpass file
	.venv/bin/python scripts/postgres.py connection

postgres-password: ## Explicitly display the development-role password
	.venv/bin/python scripts/postgres.py password

postgres-shell: ## Open psql as the non-superuser dev role inside the database pod
	.venv/bin/python scripts/postgres.py shell

postgres-service-render: ## Preview scoped boot-time localhost database access
	bash scripts/postgres-service.sh render "$$(id -un)" "$${PLATFORM_KUBECONFIG:-$$HOME/.kube/spark-platform.yaml}"

postgres-service-install: ## Install restricted localhost:5432 forwarding at boot (sudo)
	@test -f "$${PLATFORM_KUBECONFIG:-$$HOME/.kube/spark-platform.yaml}"
	sudo bash scripts/postgres-service.sh install "$$(id -un)" "$${PLATFORM_KUBECONFIG:-$$HOME/.kube/spark-platform.yaml}"

postgres-service-status: ## Inspect the persistent localhost forwarder and token renewal
	systemctl status spark-postgres-access.service spark-postgres-credentials.timer --no-pager

postgres-up: ## Import image, deploy PostgreSQL, and install boot-time localhost access (sudo)
	$(MAKE) postgres-image-import
	$(MAKE) deploy-postgres
	$(MAKE) postgres-service-install
	$(MAKE) postgres-connection

.PHONY: verify-postgres-runtime
verify-postgres-runtime: ## Test PostgreSQL auth and persistence with disposable data
	.venv/bin/python scripts/verify-postgres-runtime.py
