# SPDX-License-Identifier: GPL-3.0-or-later
"""How a device renders: on a host GPU, or on the CPU."""
import glob
import os

from . import catalog

GPU_MODES = ("auto", "software")

# Android 17 on the CPU (it has no gralloc.default mapper): generic minigbm allocates in our
# hidden vkms device, Pastel (Vulkan) draws, ANGLE runs GLES on it. override_props keeps the
# image's waydroid-init from switching to gralloc.default.
VKMS_PROPS = {"ro.hardware.gralloc": "minigbm", "ro.hardware.egl": "angle", "ro.hardware.vulkan": "pastel",
              "ro.waydroid.override_props": "0"}


def host_gpu():
    """The render node stock Waydroid would pick: one not driven by NVIDIA's driver (nor vgem's
    faux device), or None."""
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        driver = os.path.realpath("/sys/class/drm/{}/device/driver".format(os.path.basename(node)))
        if os.path.basename(driver) not in ("nvidia", "faux_driver"):
            return node
    return None


def on_vkms(android, setting):
    """Does a device of this Android version and gpu setting render on the CPU into our vkms
    device? Where that is the version's software path: when set to software, or no GPU is usable."""
    return (android in catalog.VERSIONS and catalog.get(android, "software", True) == "vkms"
            and (setting == "software" or host_gpu() is None))
