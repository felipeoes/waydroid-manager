# SPDX-License-Identifier: GPL-3.0-or-later
"""Rendering on an NVIDIA GPU with its proprietary driver (gpu mode "nvidia").

Android's Venus Vulkan driver sends Vulkan over a unix socket to a patched virglrenderer that
runs on the host as the user, one per device (session/main.py starts it). ANGLE runs GLES on
Vulkan, and gralloc allocates through libgbm_mesa_wrapper.so. The binaries are
quinovax/waydroid-nvidia's (docs/spike-findings.md):
- host: virgl_test_server, virgl_render_server, libvirglrenderer.so.1
- guest "venus": vulkan.virtio.so (x86, x86_64) and libgbm_mesa_wrapper.so, for 14 to 17,
  whose own ANGLE and hwcomposer work
- guest "full13": also their ANGLE and hwcomposer, for 11 and 13
"""
import os
import tarfile

from .. import paths
from . import layers

_BASE = "https://github.com/quinovax/waydroid-nvidia/releases/download/v0.1.2/"
HOST = (_BASE + "waydroid-nvidia-host-x86_64-v0.1.2.tar.gz",
        "15eb7656e0e111c3412c09c7bbbbc9e74dcc1446f7a8cf835aa754c52740b8bd")
VENUS = (_BASE + "waydroid-nvidia-guest-android-x86_64-v0.1.2.tar.gz",
         "313e6d583f4d8a067757ccee49c7d7eeada4e3d656950b19d1a524c1a3943ec6")
PREBUILTS = (_BASE + "waydroid-nvidia-guest-prebuilts-v0.1.2.tar.gz",
             "4881536d77079208d061f43ca204b95d574018346e956ff3c960e1e29e8a6066")
GUEST = {"venus": [VENUS], "full13": [VENUS, PREBUILTS]}
GRALLOC = {"venus": "minigbm_gbm_mesa", "full13": "gbm"}


def _unpack(archive, tmp, place):
    """Extract the members place(name) maps to a path under tmp (None: skip)."""
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            dst = place(os.path.normpath(m.name))
            if m.isfile() and dst:
                m.name = dst
                t.extract(m, tmp, filter="data")


def _host_place(name):
    """bin/ for the programs (layers.set_modes makes them executable), lib/ for the library."""
    if name in ("virgl_test_server", "virgl_render_server"):
        return "bin/" + name
    return "lib/" + name if name == "libvirglrenderer.so.1" else None


def _layer(name, pin, place):
    url, sha = pin
    d = os.path.join(paths.STATE_DIR, "nvidia", "{}-{}".format(name, sha[:12]))
    return layers.layer(d, url, sha, lambda a, tmp: _unpack(a, tmp, place), "NVIDIA support")


def host_dir():
    """The renderer's directory: bin/virgl_test_server, bin/virgl_render_server, lib/."""
    return _layer("host", HOST, _host_place)


def guest_layers(kind):
    """Layers holding vendor/... for the guest build kind (catalog "nvidia"). Only vendor/:
    the prebuilts' surfaceflinger is left out, the image's own works."""
    return [_layer("guest", pin, lambda n: n if n.startswith("vendor/") else None)
            for pin in GUEST[kind]]


def available():
    """NVIDIA's proprietary driver is loaded."""
    return os.path.exists("/dev/nvidiactl")
