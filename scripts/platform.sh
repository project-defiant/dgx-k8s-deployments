#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
kubeconfig=${PLATFORM_KUBECONFIG:-${HOME}/.kube/spark-platform.yaml}
context=spark-platform
action=${1:-help}
environment=${2:-dev}
case "$environment" in dev|prod) ;; *) echo 'Environment must be dev or prod' >&2; exit 2;; esac
namespace="docs-$environment"
values=(-f "$repo/apps/docs/values.yaml")
[[ $environment != prod ]] || values+=(-f "$repo/apps/docs/values-prod.yaml")
kube=(kubectl --kubeconfig "$kubeconfig" --context "$context" --request-timeout=10s)
helm_args=(--kubeconfig "$kubeconfig" --kube-context "$context" --namespace "$namespace")
guard() {
  [[ -f $kubeconfig ]] || { echo "Missing dedicated kubeconfig: $kubeconfig" >&2; exit 1; }
  local server
  server=$("${kube[@]}" config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
  [[ $server == https://127.0.0.1:6443 ]] || { echo 'Refusing cluster: expected the dedicated loopback K3s endpoint' >&2; exit 1; }
  "${kube[@]}" get node spark-platform -o name >/dev/null
}
forgejo_values() {
  python3 "$repo/scripts/build-forgejo.py" --verify >/dev/null
  local architecture
  case "$(uname -m)" in aarch64) architecture=arm64;; x86_64) architecture=amd64;; *) echo 'Unsupported native architecture' >&2; exit 1;; esac
  forgejo_overlay="$repo/.dev-tools/forgejo/$architecture/values.yaml"
}
case "$action" in
  help)
    echo 'Usage: platform.sh {render|validate|deploy|status|docs-status|access|rollback|dry-run|forgejo-render|forgejo-validate|forgejo-deploy|forgejo-status} [dev|prod]'
    echo 'Set PLATFORM_KUBECONFIG to a dedicated mode-600 kubeconfig; context must be spark-platform.';;
  render) helm template docs "$repo/charts/private-app" -n "$namespace" "${values[@]}";;
  forgejo-render)
    forgejo_values
    helm template forgejo "$repo/charts/forgejo" -n forgejo -f "$repo/apps/forgejo/values.yaml" -f "$forgejo_overlay";;
  forgejo-validate)
    forgejo_values
    helm lint "$repo/charts/forgejo" -f "$repo/apps/forgejo/values.yaml" -f "$forgejo_overlay" --strict
    helm template forgejo "$repo/charts/forgejo" -n forgejo -f "$repo/apps/forgejo/values.yaml" -f "$forgejo_overlay" >/dev/null;;
  forgejo-deploy)
    guard
    forgejo_values
    "${kube[@]}" get node spark-platform -o json | python3 "$repo/scripts/forgejo-artifact.py" check-node
    helm upgrade --install forgejo "$repo/charts/forgejo" --kubeconfig "$kubeconfig" --kube-context "$context" \
      --namespace forgejo --create-namespace -f "$repo/apps/forgejo/values.yaml" -f "$forgejo_overlay" --wait --timeout 300s;;
  forgejo-status)
    guard
    "${kube[@]}" -n forgejo get deployments,pods,services,pvc,networkpolicy;;
  validate)
    helm lint "$repo/charts/private-app" "${values[@]}"
    helm template docs "$repo/charts/private-app" -n "$namespace" "${values[@]}" >/dev/null;;
  deploy)
    guard
    helm upgrade --install docs "$repo/charts/private-app" "${helm_args[@]}" "${values[@]}" --create-namespace --wait --timeout 180s;;
  status)
    guard
    "${kube[@]}" get nodes
    "${kube[@]}" get deployments,daemonsets,pods,services,pvc,resourcequota -A;;
  docs-status)
    guard
    "${kube[@]}" -n "$namespace" get deployments,pods,services,pvc,resourcequota;;
  access)
    guard
    port=${PLATFORM_LOCAL_PORT:-8088}
    if [[ ! $port =~ ^[1-9][0-9]{3,4}$ ]] || ((port < 1024 || port > 65535)); then
      echo 'Local port must be between 1024 and 65535' >&2; exit 2
    fi
    exec "${kube[@]}" -n "$namespace" port-forward --address 127.0.0.1 service/docs "$port:80";;
  rollback)
    guard
    revision=${PLATFORM_REVISION:?Set PLATFORM_REVISION to a known Helm revision}
    [[ $revision =~ ^[1-9][0-9]*$ ]] || { echo 'Revision must be a positive integer' >&2; exit 2; }
    helm rollback docs "$revision" "${helm_args[@]}" --wait --timeout 180s;;
  dry-run)
    guard
    helm template docs "$repo/charts/private-app" -n "$namespace" "${values[@]}" | "${kube[@]}" -n "$namespace" apply --dry-run=server -f -;;
  *) echo "Unknown action: $action" >&2; exit 2;;
esac
