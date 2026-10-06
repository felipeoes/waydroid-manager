# SPDX-License-Identifier: GPL-3.0-or-later
"""How a device renders (its gpu setting resolved for this host and Android version):
- "nvidia": on an NVIDIA GPU with its proprietary driver, through Venus (daemon/nvidia.py)
- "gpu": on another host GPU, the one picked or stock Waydroid's pick
- "software": on the CPU, stock Waydroid's way (gralloc.default, SwiftShader)
- "vkms": on the CPU, our way for versions without gralloc.default (see VKMS_PROPS)
"""
import collections
import glob
import os
import re

from . import catalog

# The gpu setting: auto, software, or one of the host's GPUs by its PCI address (stable across
# reboots, unlike render node numbers)
PCI_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
VENDORS = {"10de": "NVIDIA", "1002": "AMD", "8086": "Intel"}
PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids")
Gpu = collections.namedtuple("Gpu", "pci label driver node")

# Android 17 on the CPU (it has no gralloc.default mapper): generic minigbm allocates in our
# hidden vkms device, Pastel (Vulkan) draws, ANGLE runs GLES on it. override_props keeps the
# image's waydroid-init from switching to gralloc.default.
VKMS_PROPS = {"ro.hardware.gralloc": "minigbm", "ro.hardware.egl": "angle", "ro.hardware.vulkan": "pastel",
              "ro.waydroid.override_props": "0"}
OTHER_GPU_PROPS = {"ro.hardware.gralloc": "minigbm_gbm_mesa", "ro.waydroid.override_props": "0"}
SOFTWARE_PROPS = {"ro.hardware.gralloc": "default", "ro.hardware.egl": "swiftshader", "gralloc.gbm.device": ""}
# Venus over the socket the session's renderer listens on, bound at /dev/venus. minigbm still
# opens a DRM device before the wrapper takes over: our vkms device stands in.
NVIDIA_PROPS = {
    "ro.hardware.egl": "angle", "ro.hardware.vulkan": "virtio",
    "mesa.vn.debug": "vtest", "mesa.vtest.socket.name": "/dev/venus/venus.sock",
    "debug.hwui.renderer": "skiagl", "debug.renderengine.backend": "skiaglthreaded",
    # no mutable-format swapchains: Mesa asks gralloc for such buffers with CPU_WRITE_RARELY
    # (to force LINEAR), and NVIDIA can't render into LINEAR ones, so games' windows stay black
    "debug.angle.feature_overrides_disabled": "supportsYUVSamplerConversion:supportsSwapchainMutableFormat",
    "persist.waydroid.use_subsurface": "false",
    "ro.surface_flinger.vsync_event_phase_offset_ns": "0", "ro.surface_flinger.vsync_sf_event_phase_offset_ns": "0",
    "ro.surface_flinger.max_frame_buffer_acquired_buffers": "3",
    "ro.surface_flinger.has_wide_color_display": "false", "ro.surface_flinger.use_color_management": "false",
}


def _driver(card):
    return os.path.basename(os.path.realpath("/sys/class/drm/{}/device/driver".format(card)))


def host_gpu():
    """The render node stock Waydroid would pick: one not driven by NVIDIA's driver (nor vgem's
    faux device), or None."""
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        if _driver(os.path.basename(node)) not in ("nvidia", "faux_driver"):
            return node
    return None


def nvidia_display():
    """Does a GPU on NVIDIA's driver show the desktop (drive a connected output)? Then buffers
    from another GPU can't be shown (GNOME on NVIDIA can't import them)."""
    for status in glob.glob("/sys/class/drm/card*-*/status"):
        card = os.path.basename(os.path.dirname(status)).split("-")[0]
        with open(status) as f:
            if f.read().strip() == "connected" and _driver(card) == "nvidia":
                return True
    return False


def _pci_name(vendor, device):
    """The model from pci.ids, its marketing name when it has one ("GB206 [GeForce RTX 5060 Ti]"
    -> "GeForce RTX 5060 Ti"), or None."""
    for path in PCI_IDS:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                in_vendor = False
                for line in f:
                    if not line.startswith("\t"):
                        in_vendor = line.startswith(vendor + "  ")
                    elif in_vendor and line.startswith("\t" + device + "  "):
                        name = line.split("  ", 1)[1].strip()
                        m = re.search(r"\[(.+)\]", name)
                        return m.group(1) if m else name
        except OSError:
            continue
    return None


def gpus():
    """The host's GPUs with a render node, in PCI order: "GPU 0: NVIDIA GeForce RTX 5060 Ti", ..."""
    found = []
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        dev = "/sys/class/drm/{}/device".format(os.path.basename(node))
        pci = os.path.basename(os.path.realpath(dev))
        if not PCI_RE.match(pci):
            continue                    # vgem and other virtual devices
        try:
            with open(dev + "/vendor") as f:
                vendor = f.read().strip()[2:]
            with open(dev + "/device") as f:
                device = f.read().strip()[2:]
        except OSError:
            continue
        name = _nvidia_model(pci) or _pci_name(vendor, device) or device
        vendor = VENDORS.get(vendor, vendor)
        if not name.startswith(vendor):
            name = vendor + " " + name
        found.append((pci, name, _driver(os.path.basename(node)), node))
    found.sort()
    return [Gpu(pci, "GPU {}: {}".format(i, name), driver, node) for i, (pci, name, driver, node) in enumerate(found)]


def _nvidia_model(pci):
    """NVIDIA's driver knows its cards' names before pci.ids does."""
    try:
        with open("/proc/driver/nvidia/gpus/{}/information".format(pci)) as f:
            for line in f:
                if line.startswith("Model:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def mode(android, setting):
    """(mode, render node of a picked GPU or None) of a device of this Android version (None:
    stock Waydroid's own, on Android 10) with this gpu setting, on this host. The daemon
    refuses what can't run (NVIDIA without a build or driver); ValueError: the picked GPU is gone."""
    known = android in catalog.VERSIONS
    if setting not in ("auto", "software"):
        picked = next((g for g in gpus() if g.pci == setting), None)
        if picked is None:
            raise ValueError("the GPU at {} isn't there anymore: pick another in Graphics".format(setting))
        return ("nvidia", None) if picked.driver == "nvidia" else ("gpu", picked.node)
    if setting == "auto" and known and catalog.get(android, "nvidia") \
            and os.path.exists("/dev/nvidiactl") and nvidia_display():
        return "nvidia", None
    if setting == "auto" and host_gpu() and not nvidia_display():
        return "gpu", None
    return ("vkms" if known and catalog.get(android, "software", True) == "vkms" else "software"), None
