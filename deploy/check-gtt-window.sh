#!/bin/sh
# Post-reboot proof that the GTT page cap took effect and that stackd sees it.
# Run: sh deploy/check-gtt-window.sh
TARGET_PAGES=32505856            # x 4 KiB = 124.00 GiB
echo "== 1. kernel command line (must carry ttm.pages_limit) =="
CMDLINE=$(cat /proc/cmdline)
echo "$CMDLINE"
case "$CMDLINE" in
  *ttm.pages_limit=*) echo "  -> present in this boot" ;;
  *) echo "  -> MISSING: booted from an older config (or a recovery entry, which reads GRUB_CMDLINE_LINUX)" ;;
esac

echo
echo "== 2. module parameters as applied =="
for m in ttm amdttm; do
  for p in pages_limit page_pool_size; do
    f="/sys/module/$m/parameters/$p"
    [ -e "$f" ] && echo "  $m.$p = $(cat $f)"
  done
done
[ -d /sys/module/amdttm ] || echo "  (amdttm is not a module on this kernel -> those two args are inert here)"

echo
echo "== 3. what amdgpu exposes (this is what stackd probes) =="
for f in /sys/class/drm/card*/device/mem_info_gtt_total; do
  [ -e "$f" ] || continue
  B=$(cat "$f")
  echo "  $f = $B bytes = $(awk -v b="$B" 'BEGIN{printf "%.1f", b/1073741824}') GiB  (target $((TARGET_PAGES * 4096)))"
done
(dmesg 2>/dev/null || journalctl -k -b --no-pager) | grep -i 'GTT memory ready' | tail -2

echo
echo "== 4. what stackd books =="
docker exec stackd stackctl status 2>&1 | sed -n '1,12p'
docker exec stackd stackctl validate 2>&1 | grep -E 'profile |host_unified|! ' | head -14

echo
echo "PASS = mem_info_gtt_total ~= ${TARGET_PAGES} pages in bytes (133143986176 = 124.0 GiB),"
echo "       budget_effective 90.0 on igpu0, and NO 'igpu budget clamped' flag."
echo "If stackd still says 62.2 after a correct boot, wait <=5 min or restart the daemon:"
echo "fit.py caches the measured window for _WINDOW_TTL_S = 300 s."
