#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version='v1.35.9+k3s1'
pinned_sha256='6176dd12c512e177af998e6035920f6283c5fdbd5adfdff85c103a7fdf2a00b5'
action=validate
preservation=false
network=false
for arg in "$@"; do
  case "$arg" in
    --validate|--dry-run) action=validate;;
    --install) action=install;;
    --preservation-reviewed) preservation=true;;
    --network-reviewed) network=true;;
    --help) echo 'Usage: platform-bootstrap.sh [--validate|--dry-run|--install --preservation-reviewed --network-reviewed]'; exit 0;;
    *) echo "Unknown argument: $arg" >&2; exit 2;;
  esac
done
if [[ $action == install ]]; then
  [[ $preservation == true && $network == true ]] || { echo 'Installation requires --preservation-reviewed and --network-reviewed' >&2; exit 1; }
  [[ $EUID == 0 ]] || { echo 'Installation requires sudo/root' >&2; exit 1; }
fi
"$repo/scripts/platform-preflight.sh" --validate
config=/etc/rancher/k3s/config.yaml
unit=/etc/systemd/system/k3s.service
binary=/usr/local/bin/k3s
# Refuse unmanaged state and systemd overrides before downloads or mutations.
marker=/etc/rancher/k3s/spark-platform-managed
for override in /etc/systemd/system/k3s.service.d /run/systemd/system/k3s.service.d /run/systemd/system/k3s.service /usr/lib/systemd/system/k3s.service /lib/systemd/system/k3s.service; do
  [[ ! -e $override ]] || { echo "Unmanaged systemd override requires review: $override" >&2; exit 1; }
done
if [[ -d /etc/rancher/k3s/config.yaml.d ]] && find /etc/rancher/k3s/config.yaml.d -type f -print -quit | read -r _; then
  echo 'Existing K3s config fragments require manual review' >&2; exit 1
fi
for pair in "$config|$repo/platform/k3s/config.yaml.example" "$unit|$repo/platform/k3s/k3s.service"; do
  target=${pair%%|*}; source=${pair#*|}
  if [[ -L $target ]] || { [[ -e $target ]] && ! cmp -s "$source" "$target"; }; then
    echo "Refusing existing unmanaged or changed configuration: $target" >&2; exit 1
  fi
done
if [[ -e /var/lib/rancher/k3s || -e $binary || -e $marker ]]; then
  [[ -f $config && -f $unit && -f $binary && -f $marker && ! -L $binary && ! -L $marker ]] || { echo 'Existing K3s state is not a complete managed installation' >&2; exit 1; }
  [[ $(cat "$marker") == "$version $pinned_sha256" ]] || { echo 'Managed installation marker differs' >&2; exit 1; }
  [[ $(sha256sum "$binary" | awk '{print $1}') == "$pinned_sha256" ]] || { echo 'Existing K3s binary differs from reviewed checksum' >&2; exit 1; }
fi
printf 'Pinned release: %s; dedicated kubeconfig: /etc/rancher/k3s/spark-platform.yaml\n' "$version"
if [[ $action == validate ]]; then
  echo 'Validation only; no downloads or host changes. Review preservation and network/firewall scope before --install.'
  exit 0
fi
umask 077
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
base="https://github.com/k3s-io/k3s/releases/download/$version"
curl --fail --location --proto '=https' --tlsv1.2 "$base/k3s-arm64" -o "$stage/k3s-arm64"
curl --fail --location --proto '=https' --tlsv1.2 "$base/sha256sum-arm64.txt" -o "$stage/checksums"
awk '$2 == "k3s-arm64" {print}' "$stage/checksums" > "$stage/selected"
[[ $(wc -l < "$stage/selected") == 1 ]] || { echo 'Missing or ambiguous official binary checksum' >&2; exit 1; }
[[ $(awk '{print $1}' "$stage/selected") == "$pinned_sha256" ]] || { echo 'Official checksum differs from reviewed pin' >&2; exit 1; }
(cd "$stage" && sha256sum --check selected)
if [[ -e $binary ]]; then
  expected=$(awk '{print $1}' "$stage/selected")
  actual=$(sha256sum "$binary" | awk '{print $1}')
  [[ $actual == "$expected" ]] || { echo 'Existing K3s binary differs from official pinned checksum; refusing overwrite' >&2; exit 1; }
else
  install -m 0755 "$stage/k3s-arm64" "$binary"
fi
if [[ -e /usr/local/sbin/spark-platform-firewall ]] && ! cmp -s "$repo/scripts/platform-firewall.sh" /usr/local/sbin/spark-platform-firewall; then
  echo 'Existing firewall helper differs; review required' >&2; exit 1
fi
install -m 0755 "$repo/scripts/platform-firewall.sh" /usr/local/sbin/spark-platform-firewall
install -d -m 0700 /etc/rancher/k3s
install -m 0600 "$repo/platform/k3s/config.yaml.example" "$config"
install -m 0644 "$repo/platform/k3s/k3s.service" "$unit"
printf '%s %s\n' "$version" "$pinned_sha256" > "$stage/managed"
install -m 0600 "$stage/managed" "$marker"
systemctl daemon-reload
/usr/local/sbin/spark-platform-firewall --apply
systemctl enable --now k3s.service
for ((attempt=0; attempt<90; attempt++)); do
  [[ -s /etc/rancher/k3s/spark-platform.yaml ]] && break
  sleep 2
done
[[ -s /etc/rancher/k3s/spark-platform.yaml ]] || { echo 'Kubeconfig not ready; inspect sudo journalctl -u k3s' >&2; exit 1; }
# K3s refreshes its generated file: create a separate named-context copy each rerun.
sed -e 's/default/spark-platform/g' \
  -e 's#server: https://0.0.0.0:6443#server: https://127.0.0.1:6443#' \
  /etc/rancher/k3s/spark-platform.yaml > "$stage/kubeconfig"
install -m 0600 "$stage/kubeconfig" /etc/rancher/k3s/spark-platform-context.yaml
echo 'Installed. Dedicated context spark-platform: /etc/rancher/k3s/spark-platform-context.yaml (root only).'
echo 'No default kubeconfig, Docker, Minikube or Tailscale configuration was changed.'
