#!/usr/bin/env bash
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
action=${1:-help}
if [[ $action == help ]]; then
  echo 'Usage: platform-service.sh {render|install} USER KUBECTL ADMINCONFIG [dev|prod] [PORT]'
  exit 0
fi
[[ $action == render || $action == install ]] || { echo 'Unknown action' >&2; exit 2; }
[[ $# -ge 4 && $# -le 6 ]] || { echo 'USER, KUBECTL and KUBECONFIG are required' >&2; exit 2; }
owner=$2
kubectl=$3
kubeconfig=$4
environment=${5:-dev}
port=${6:-8088}
if [[ $owner == root ]]; then
  echo 'Run make docs-service-install as your normal login user, without sudo. The Make target requests sudo internally.' >&2
  exit 2
fi
[[ $owner =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || { echo 'Select a valid non-root Linux username' >&2; exit 2; }
for path in "$kubectl" "$kubeconfig"; do
  [[ $path =~ ^/[A-Za-z0-9_./-]+$ && $path != *'/../'* && $path != */.. ]] || { echo 'Paths must be absolute without spaces or systemd specifiers' >&2; exit 2; }
done
[[ $environment == dev || $environment == prod ]] || { echo 'Environment must be dev or prod' >&2; exit 2; }
if [[ ! $port =~ ^[1-9][0-9]{3,4}$ ]] || ((port < 1024 || port > 65535)); then
  echo 'Port must be between 1024 and 65535' >&2; exit 2
fi
group=$(id -gn "$owner")
render() {
  sed -e "s|@USER@|$owner|g" -e "s|@GROUP@|$group|g" \
    -e "s|@KUBECTL@|$kubectl|g" -e "s|@KUBECONFIG@|$kubeconfig|g" \
    -e "s|@ENV@|$environment|g" -e "s|@PORT@|$port|g" \
    "$repo/platform/systemd/spark-docs-access.service.in"
}
if [[ $action == render ]]; then
  render
  cat "$repo/platform/systemd/spark-docs-credentials.service.in" "$repo/platform/systemd/spark-docs-credentials.timer.in"
  sed "s|@ENV@|$environment|g" "$repo/platform/rbac/docs-forwarder.yaml.in"
  exit 0
fi
[[ $EUID == 0 ]] || { echo 'Installation requires sudo/root' >&2; exit 1; }
[[ -x $kubectl && -f $kubeconfig ]] || { echo 'kubectl and the dedicated kubeconfig must exist' >&2; exit 1; }
server=$(runuser -u "$owner" -- "$kubectl" --kubeconfig "$kubeconfig" --context spark-platform config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
[[ $server == https://127.0.0.1:6443 ]] || { echo 'Refusing unexpected Kubernetes API endpoint' >&2; exit 1; }
runuser -u "$owner" -- "$kubectl" --kubeconfig "$kubeconfig" --context spark-platform --request-timeout=10s get node spark-platform -o name >/dev/null
# Root never executes the user-selected kubectl or reads that user's kubeconfig.
# Validate every parent as well as the fixed privileged executable/config.
/usr/bin/python3 -I - <<'CHECK'
from pathlib import Path
for value in ['/usr/local/bin/k3s', '/etc/rancher/k3s/spark-platform.yaml',
              '/etc/systemd/system', '/etc', '/var/lib']:
    path = Path(value)
    for item in [path, *path.parents]:
        info = item.lstat()
        if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit('Refusing untrusted root executable/configuration path')
CHECK
for name in spark-docs-access.service spark-docs-credentials.service spark-docs-credentials.timer; do
  unit=/etc/systemd/system/$name
  if [[ -L $unit ]] || { [[ -e $unit ]] && ! head -1 "$unit" | grep -Fxq '# Managed by TheDefiant platform-service.sh'; }; then
    echo 'Refusing to overwrite an unmanaged unit' >&2; exit 1
  fi
  for base in /etc/systemd/system /run/systemd/system; do
    [[ ! -e $base/$name.d ]] || { echo 'Existing unit overrides require review' >&2; exit 1; }
  done
done
/usr/bin/python3 -I "$repo/platform/systemd/check-install.py" "$(id -u "$owner")" "$environment"
stage=$(mktemp -d)
update=''
trap 'rm -rf -- "$stage"; if [[ -n $update ]]; then rm -rf -- "$update"; fi' EXIT
chmod 0700 "$stage"
render > "$stage/spark-docs-access.service"
sed "s|@ENV@|$environment|g" "$repo/platform/rbac/docs-forwarder.yaml.in" > "$stage/rbac.yaml"
# Fail closed on upgrades: after validation, never leave the old admin forward running.
# Stop only units whose existing on-disk managed marker was checked above.
for name in spark-docs-credentials.timer spark-docs-credentials.service spark-docs-access.service; do
  if [[ -f /etc/systemd/system/$name ]]; then
    systemctl stop "$name"
  fi
done
/usr/local/bin/k3s kubectl --kubeconfig /etc/rancher/k3s/spark-platform.yaml \
  --context default --server=https://127.0.0.1:6443 --request-timeout=30s apply -f "$stage/rbac.yaml"
install -d -m 0700 /etc/spark-docs-access
install -d -m 0711 /var/lib/spark-docs-access
update=$(mktemp -d /etc/spark-docs-access/.update.XXXXXX)
install -m 0700 "$repo/platform/systemd/refresh-credentials.py" "$update/refresh-credentials.py"
printf '{"managed":"TheDefiant platform-service.sh","environment":"%s","uid":%s}\n' "$environment" "$(id -u "$owner")" > "$stage/config.json"
install -m 0600 "$stage/config.json" "$update/config.json"
mv -f -- "$update/refresh-credentials.py" /etc/spark-docs-access/refresh-credentials.py
mv -f -- "$update/config.json" /etc/spark-docs-access/config.json
rmdir -- "$update"
update=''
install -m 0644 "$stage/spark-docs-access.service" /etc/systemd/system/spark-docs-access.service
for name in spark-docs-credentials.service spark-docs-credentials.timer; do
  install -m 0644 "$repo/platform/systemd/$name.in" "/etc/systemd/system/$name"
done
systemctl daemon-reload
systemctl enable spark-docs-access.service spark-docs-credentials.timer
systemctl start spark-docs-credentials.service
systemctl restart spark-docs-access.service
systemctl start spark-docs-credentials.timer
echo "Enabled scoped docs-$environment access on 127.0.0.1:$port with automatic credential renewal."
