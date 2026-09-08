"""Enumerate the GPUs actually present on the host via PCI sysfs, so a
device's render node is resolved fresh on every config load instead of
trusting a static env value that silently goes stale.

`/dev/dri/renderDNN` numbers (like CUDA device indices) are assigned in PCI
probe order — they can shift across a reboot, or any PCI topology change
(a new card, a moved slot), even though the actual hardware didn't move.
Matching by PCI vendor + display-controller class instead is stable: it
only breaks if the physical card itself is swapped for a different vendor.

Ordering across multiple GPUs of the SAME vendor is just PCI bus address,
low to high — there's no way to know which physical card a human calls
"the first one" beyond that, so this only promises to be *consistent*
(the same card gets the same index every time, run to run) not "correct"
in some deeper sense. That's enough to keep igpu0/igpu1/... pointed at the
same physical cards across reboots.
"""

from __future__ import annotations

import glob
import os

_VENDOR_NAMES = {"0x1002": "amd", "0x10de": "nvidia", "0x8086": "intel"}
_DISPLAY_CLASS_PREFIX = "0x03"   # VGA (0x0300) / 3D (0x0302) / display (0x0380) controllers


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def enumerate_gpus(sys_class_drm: str = "/sys/class/drm") -> list[dict]:
    """One entry per physical display-controller PCI device found under
    `sys_class_drm`, sorted by PCI address: {pci, vendor, card_node,
    render_node}. `render_node` is None for a card with no render node
    exposed (rare — handled, not assumed away)."""
    found: dict[str, dict] = {}
    for drm_dir in sorted(glob.glob(os.path.join(sys_class_drm, "card[0-9]*"))):
        dev_dir = os.path.join(drm_dir, "device")
        try:
            vendor = _read(os.path.join(dev_dir, "vendor"))
            cls = _read(os.path.join(dev_dir, "class"))
            pci = os.path.basename(os.path.realpath(dev_dir))
        except OSError:
            continue
        if not cls.startswith(_DISPLAY_CLASS_PREFIX):
            continue
        entry = found.setdefault(pci, {
            "pci": pci, "vendor": _VENDOR_NAMES.get(vendor, vendor),
            "card_node": None, "render_node": None,
        })
        entry["card_node"] = f"/dev/dri/{os.path.basename(drm_dir)}"
        render = glob.glob(os.path.join(dev_dir, "drm", "renderD*"))
        if render:
            entry["render_node"] = f"/dev/dri/{os.path.basename(render[0])}"
    return sorted(found.values(), key=lambda e: e["pci"])


def render_nodes_by_vendor(vendor: str, sys_class_drm: str = "/sys/class/drm") -> list[str]:
    """Render nodes for one vendor ('amd' / 'nvidia' / ...), PCI-address
    order — index 0 is whichever `<vendor>0` device slot (igpu0, cuda0, ...)
    gets index 0 in devices.yaml, index 1 is the next of that vendor, etc."""
    return [g["render_node"] for g in enumerate_gpus(sys_class_drm)
            if g["vendor"] == vendor and g["render_node"]]


def amdgpu_device_dirs(sys_class_drm: str = "/sys/class/drm") -> list[str]:
    """`cardN/device` paths of cards driven by `amdgpu`, in card-number order.

    Same selection rule `telemetry._igpu0_sysfs` uses (driver symlink, not PCI
    vendor: an NVIDIA card is `nvidia`, an Intel one `i915`), and the same
    `card[0-9]+` filter that skips the `cardN-DP-*` connector dirs. Shared by
    the live sample and the offline fit check so the two can never disagree
    about WHICH card the iGPU's budget refers to (see `gtt_window_gib`)."""
    import re
    out = []
    for drm_dir in sorted(glob.glob(os.path.join(sys_class_drm, "card[0-9]*"))):
        if not re.fullmatch(r"card[0-9]+", os.path.basename(drm_dir)):
            continue  # skip the cardN-DP-* connector dirs
        dev_dir = os.path.join(drm_dir, "device")
        try:
            drv = os.path.basename(os.path.realpath(os.path.join(dev_dir, "driver")))
        except OSError:
            continue
        if drv == "amdgpu":
            out.append(dev_dir)
    return out


def gtt_window_gib(*, index: int = 0, sys_class_drm: str = "/sys/class/drm") -> float | None:
    """The GTT window amdgpu actually exposes for `igpu<index>`, in GiB — the
    ceiling the driver will honour for host-RAM-backed claims, or None if it
    can't be measured.

    Why this exists and not just `IGPU_VRAM_BUDGET_GIB`: that env var is a
    config *wish*, and nothing rejects 90 on a driver that exposes 62.2 GiB.
    The window is a DRIVER limit rather than silicon, though: `mem_info_gtt_total`
    is exactly `ttm.pages_limit` in bytes (66812620800 = 16311675 pages × 4 KiB),
    and the kernel auto-sets that to half of MemTotal. So a budget above half of
    RAM is raised at boot, not in config: `ttm.pages_limit=<pages>` plus
    `ttm.page_pool_size=<pages>` on the kernel command line (`amdttm.*` on the
    kernels where TTM is split out), then `update-grub` + reboot. Until that
    happens this probe keeps clamping, which is the point.

    Only GTT is a claim on host RAM — the stolen window is boot-carved and never
    shows up in MemTotal (see telemetry._igpu0_sysfs) — so GTT total is the number
    the pool math must clamp against. None (no amdgpu card, no /sys access,
    container without /dev/dri, CI) means UNKNOWN, not zero: the caller must
    then fall back to the configured budget rather than clamping a device to
    0 GiB it is plainly using.
    """
    dirs = amdgpu_device_dirs(sys_class_drm)
    if len(dirs) <= index:
        return None
    try:
        raw = _read(os.path.join(dirs[index], "mem_info_gtt_total"))
    except OSError:
        return None
    try:
        gib = int(raw) / (1024 ** 3)
    except (TypeError, ValueError):
        return None
    return round(gib, 1) if gib > 0 else None
