#!/usr/bin/env bash
# Read-only and deliberately avoids kubeconfigs, tokens, environment and container inspect.
set -euo pipefail
case "${1:-}" in ''|--validate) ;; *) echo 'Usage: platform-preflight.sh [--validate]' >&2; exit 2;; esac
for tool in python3 ip ss systemctl curl sha256sum install cmp sed awk iptables ip6tables; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
[[ $(uname -m) == aarch64 ]] || { echo 'Requires ARM64 Linux' >&2; exit 1; }
[[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || { echo 'Requires cgroup v2' >&2; exit 1; }
python3 - <<'PY'
import ipaddress, json, os, shutil, subprocess
pods = ipaddress.ip_network('10.84.0.0/16')
services = ipaddress.ip_network('10.85.0.0/16')
routes = json.loads(subprocess.check_output(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
for route in routes:
    dest = route.get('dst', 'default')
    if dest == 'default':
        continue
    net = ipaddress.ip_network(dest, strict=False)
    if route.get('dev') in ('cni0', 'flannel.1') and os.path.isfile('/etc/systemd/system/k3s.service'):
        continue
    if any(net.overlaps(cidr) for cidr in (pods, services)):
        raise SystemExit(f'Cluster CIDR conflicts with route {net}; review network before installation')
free = shutil.disk_usage('/var/lib').free // (1024**3)
if free < 30:
    raise SystemExit('At least 30 GiB free required in /var/lib')
print(f'ARM64 / cgroup v2 / free storage {free} GiB / candidate CIDRs clear')
PY
printf 'Kernel: '; uname -r
awk -F= '/^(NAME|VERSION_ID)=/ {print}' /etc/os-release
awk '/MemTotal:/ {printf "Host RAM: %.1f GiB\n", $2/1024/1024}' /proc/meminfo
echo 'IPv4 routes (review LAN, Docker and Minikube overlap):'
ip -4 route show table main
echo 'Listening TCP sockets:'
ss -H -lnt
if command -v docker >/dev/null; then
  docker volume ls --format '{{.Name}}' 2>/dev/null || echo 'Docker volumes unverified (daemon/permission unavailable)'
fi
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || echo 'GPU unverified'
fi
printf 'K3s service state: '; systemctl is-active k3s.service || true
if command -v minikube >/dev/null; then minikube status --format='Minikube host: {{.Host}}' || true; fi
if command -v tailscale >/dev/null; then
  echo 'Tailscale binary present; confirm NVIDIA Sync in a second session before installation.'
fi
if [[ ! -f /etc/systemd/system/k3s.service ]] && ss -H -lnt '( sport = :6443 )' | awk 'END {exit (NR == 0)}'; then
  echo 'Port 6443 is already occupied' >&2; exit 1
fi
echo 'Preflight is read-only. Minikube data, firewall scope and backups require operator review.'
