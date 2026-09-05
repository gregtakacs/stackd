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
