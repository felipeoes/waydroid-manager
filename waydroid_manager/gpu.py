# SPDX-License-Identifier: GPL-3.0-or-later
"""How a device renders (its gpu setting resolved for this host and Android version):
- "nvidia": on an NVIDIA GPU with its proprietary driver, through Venus (daemon/nvidia.py)
- "gpu": on the host GPU stock Waydroid picks (not NVIDIA's)
- "software": on the CPU, stock Waydroid's way (gralloc.default, SwiftShader)
- "vkms": on the CPU, our way for versions without gralloc.default (see VKMS_PROPS)
"""
import glob
import os

from . import catalog

GPU_MODES = ("auto", "nvidia", "software")

# Android 17 on the CPU (it has no gralloc.default mapper): generic minigbm allocates in our
# hidden vkms device, Pastel (Vulkan) draws, ANGLE runs GLES on it. override_props keeps the
# image's waydroid-init from switching to gralloc.default.
VKMS_PROPS = {"ro.hardware.gralloc": "minigbm", "ro.hardware.egl": "angle", "ro.hardware.vulkan": "pastel",
              "ro.waydroid.override_props": "0"}
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


def mode(android, setting):
    """The rendering of a device of this Android version (None: stock Waydroid's own, on Android
    10) with this gpu setting, on this host. The daemon refuses what can't run (nvidia without
    a build or driver)."""
    known = android in catalog.VERSIONS
    if setting == "nvidia" or setting == "auto" and known and catalog.get(android, "nvidia") \
            and os.path.exists("/dev/nvidiactl") and nvidia_display():
        return "nvidia"
    if setting == "auto" and host_gpu() and not nvidia_display():
        return "gpu"
    return "vkms" if known and catalog.get(android, "software", True) == "vkms" else "software"
