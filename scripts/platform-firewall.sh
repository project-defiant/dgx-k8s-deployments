#!/usr/bin/env bash
# Own only TD-SPARK-IN and its INPUT jump; never flush shared chains.
set -euo pipefail
case "${1:-}" in
  --apply|--remove) action=$1;;
  *) echo 'Usage: platform-firewall.sh --apply|--remove' >&2; exit 2;;
esac
[[ $EUID == 0 ]] || { echo 'Firewall changes require root' >&2; exit 1; }
for tool in iptables ip6tables; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
# Validate both families before touching either one. Never adopt a foreign chain.
expected_rules() {
  echo '-A TD-SPARK-IN -i lo -j RETURN'
  [[ $1 != iptables ]] || echo '-A TD-SPARK-IN -s 10.84.0.0/16 -i cni0 -j RETURN'
  echo '-A TD-SPARK-IN -p tcp -m multiport --dports 6443,10250 -j DROP'
  echo '-A TD-SPARK-IN -p udp -m udp --dport 8472 -j DROP'
  echo '-A TD-SPARK-IN -j RETURN'
}
for tool in iptables ip6tables; do
  if rules=$("$tool" -w -S TD-SPARK-IN 2>/dev/null); then
    actual=$(printf '%s\n' "$rules" | sed '/^-N TD-SPARK-IN$/d')
    [[ $actual == "$(expected_rules "$tool")" ]] || { echo "Unexpected $tool TD-SPARK-IN rules; refusing modification" >&2; exit 1; }
  fi
done
for tool in iptables ip6tables; do
  if [[ $action == --remove ]]; then
    while "$tool" -w -C INPUT -j TD-SPARK-IN 2>/dev/null; do "$tool" -w -D INPUT -j TD-SPARK-IN; done
    if "$tool" -w -S TD-SPARK-IN >/dev/null 2>&1; then
      # This private chain has no unrelated rules by contract.
      "$tool" -w -F TD-SPARK-IN
      "$tool" -w -X TD-SPARK-IN
    fi
    continue
  fi
  "$tool" -w -S TD-SPARK-IN >/dev/null 2>&1 || "$tool" -w -N TD-SPARK-IN
  add() { "$tool" -w -C TD-SPARK-IN "$@" 2>/dev/null || "$tool" -w -A TD-SPARK-IN "$@"; }
  add -i lo -j RETURN
  # Require both pod source AND actual local bridge interface; spoofed LAN sources fail.
  if [[ $tool == iptables ]]; then
    add -s 10.84.0.0/16 -i cni0 -j RETURN
  fi
  add -p tcp -m multiport --dports 6443,10250 -j DROP
  add -p udp -m udp --dport 8472 -j DROP
  add -j RETURN
  "$tool" -w -C INPUT -j TD-SPARK-IN 2>/dev/null || "$tool" -w -I INPUT 1 -j TD-SPARK-IN
  # Refuse a displaced jump: unrelated ACCEPT rules must not bypass restrictions.
  first=$("$tool" -w -S INPUT | awk '$1 == "-A" {print; exit}')
  [[ $first == '-A INPUT -j TD-SPARK-IN' ]] || { echo "TD-SPARK-IN must be first in $tool INPUT; review competing firewall manager" >&2; exit 1; }
done
