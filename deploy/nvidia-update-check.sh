#!/usr/bin/env bash
# nvidia-update-check.sh -- host-side companion to stackd's dashboard banner.
#
# 2026-09-12 incident: unattended-upgrades silently bumped nvidia-driver-610-open
# (and every libnvidia-*-610 package with it) while stackd's engines were running.
# The loaded kernel module stayed at the old version until a reboot, so nvidia-smi
# and every new CUDA container spawn started failing with an NVML driver/library
# version mismatch -- invisibly, because nothing surfaced it until a model swap
# crash-looped. The fix for THAT incident was blacklisting nvidia in
# unattended-upgrades (see /etc/apt/apt.conf.d/50unattended-upgrades) -- but a
# blacklist just trades "silently broken" for "silently stale". Someone still has
# to notice a driver update exists and reboot for it deliberately.
#
# stackd's own container can't check for this itself: it talks to Docker through
# a scoped socket-proxy, is not privileged, and has no host apt/dpkg visibility
# by design (see docker-compose.yml's isolation comments). So this runs on the
# HOST as a systemd --user timer (see systemd/stackd-nvidia-check.{service,timer}),
# and drops a small JSON file into stackd's own data volume -- already mounted
# into the container at /data -- for the dashboard to read. No new container
# privileges, same shape as HERDR-STACK's herd-act.timer.
#
# `apt list --upgradable` reads already-cached state (/var/lib/dpkg/status +
# /var/lib/apt/lists) -- it does NOT run `apt-get update` itself, so this piggy-
# backs on whatever already refreshed those lists (unattended-upgrades' own daily
# `apt-get update` still runs even for blacklisted packages). Needs no root.
#
# usage: nvidia-update-check.sh <path-to-stackd-data-volume>
set -euo pipefail

out_dir="${1:?usage: nvidia-update-check.sh <path-to-stackd-data-volume>}"
out_file="$out_dir/nvidia_update.json"

upgradable="$(apt list --upgradable 2>/dev/null | grep -E '^(nvidia-|lib(nvidia|xnvctrl))' || true)"

if [ -z "$upgradable" ]; then
  # nothing pending -- remove any stale banner from a previous check
  rm -f "$out_file"
  exit 0
fi

# Pick the driver metapackage's line if present (the one users actually act on);
# otherwise fall back to the first upgradable nvidia-ish line so something is
# always shown rather than silently picking nothing.
line="$(printf '%s\n' "$upgradable" | grep '^nvidia-driver-' | head -1)"
[ -n "$line" ] || line="$(printf '%s\n' "$upgradable" | head -1)"

# apt list --upgradable line shape:  pkg/archive new_ver arch [upgradable from: old_ver]
pkg="${line%%/*}"
new_ver="$(printf '%s\n' "$line" | awk '{print $2}')"

tmp="$(mktemp "$out_dir/.nvidia_update.XXXXXX")"
printf '{"package":"%s","version":"%s","checked_at":%s}' \
  "$pkg" "$new_ver" "$(date +%s)" > "$tmp"
mv "$tmp" "$out_file"
