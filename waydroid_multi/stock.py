# SPDX-License-Identifier: GPL-3.0-or-later
"""Access to the stock Waydroid installation.

waydroid-multi reuses a handful of *stateless* helpers from the stock
``tools`` package (LXC device-node generation, prop generation, mount
helpers, binderfs allocation, binder interface wrappers). Every call gets a
per-instance ``args`` namespace, so nothing here touches stock paths.

Everything that is version-dependent lives in this module.
"""
import argparse
import configparser
import logging
import os
import shutil
import sys

from . import paths

SUPPORTED = ((1, 5), (1, 6))

_tools = None


def stock_dir():
    exe = shutil.which("waydroid")
    if exe:
        return os.path.dirname(os.path.realpath(exe))
    return "/usr/lib/waydroid"


def tools():
    """Import and return the stock ``tools`` package."""
    global _tools
    if _tools is not None:
        return _tools
    sdir = stock_dir()
    if not os.path.isfile(os.path.join(sdir, "tools", "__init__.py")):
        raise RuntimeError("stock Waydroid not found (looked in {})".format(sdir))
    if sys.path[0] != sdir:
        sys.path.insert(0, sdir)
    import tools as t  # noqa: E402
    import tools.config  # noqa: E402,F401
    import tools.helpers  # noqa: E402,F401
    # stock run helpers call logging.verbose()
    t.helpers.logging.add_verbose_log_level()
    real = os.path.realpath(t.__file__)
    if not real.startswith(os.path.realpath(sdir) + os.sep):
        raise RuntimeError("imported 'tools' from {}, expected stock Waydroid in {}".format(real, sdir))
    _tools = t
    return t


def version():
    return tools().config.version


def version_tuple():
    try:
        return tuple(int(x) for x in version().split(".")[:2])
    except ValueError:
        return (0, 0)


def check_version():
    """Return a warning string if the stock version is untested, else None."""
    v = version_tuple()
    if v not in SUPPORTED:
        return "stock Waydroid {} has not been tested with waydroid-multi".format(version())
    return None


def make_args(work, config, **extra):
    """Build an ``args`` namespace the stock helpers accept."""
    a = argparse.Namespace()
    a.cache = {}
    a.work = work
    a.config = config
    a.log = os.path.join(work, "helpers.log")
    a.sudo_timer = False
    a.timeout = 1800
    a.details_to_stdout = False
    a.verbose = False
    a.quiet = True
    for k, v in extra.items():
        setattr(a, k, v)
    return a


def load_stock_cfg():
    cfg = configparser.ConfigParser(interpolation=None)
    if os.path.isfile(paths.STOCK_CFG):
        cfg.read(paths.STOCK_CFG)
    if "waydroid" not in cfg:
        cfg["waydroid"] = {}
    if "properties" not in cfg:
        cfg["properties"] = {}
    return cfg


def stock_images_path(cfg=None):
    cfg = cfg or load_stock_cfg()
    return cfg["waydroid"].get("images_path", paths.STOCK_WORK + "/images")


def lxc_snippets():
    """Return the stock LXC config snippets in the order set_lxc_config uses."""
    import subprocess
    cfgdir = os.path.join(tools().config.tools_src, "data", "configs")
    out = subprocess.run(["lxc-info", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    lxc_ver = int(out.split(".")[0])
    snippets = [os.path.join(cfgdir, "config_base")]
    if lxc_ver <= 2:
        snippets.append(os.path.join(cfgdir, "config_1"))
    else:
        for ver in range(3, 5):
            p = os.path.join(cfgdir, "config_{}".format(ver))
            if lxc_ver >= ver and os.path.exists(p):
                snippets.append(p)
    texts = []
    for p in snippets:
        with open(p) as f:
            texts.append(f.read())
    return texts


def seccomp_profile():
    return os.path.join(tools().config.tools_src, "data", "configs", "waydroid.seccomp")


def android_env():
    return dict(tools().helpers.lxc.ANDROID_ENV)


# Mapping from Android API level to (binder rpc protocol, servicemanager protocol).
# Mirrors tools/helpers/protocol.py of upstream main (aidl5/aidl6 for API 35/36),
# but only uses protocols the installed libgbinder knows about.
def protocols_for_sdk(sdk, known=("aidl", "aidl2", "aidl3", "aidl4")):
    if sdk < 28:
        rpc, sm = "aidl", "aidl"
    elif sdk < 30:
        rpc, sm = "aidl2", "aidl2"
    elif sdk < 31:
        rpc, sm = "aidl3", "aidl3"
    elif sdk < 33:
        rpc, sm = "aidl4", "aidl3"
    elif sdk < 35:
        rpc, sm = "aidl3", "aidl3"
    elif sdk < 36:
        rpc, sm = "aidl3", "aidl5"
    else:
        rpc, sm = "aidl3", "aidl6"
    if sm not in known:
        logging.warning("servicemanager protocol %s unknown to libgbinder, falling back to aidl3", sm)
        sm = "aidl3"
    return rpc, sm


def known_sm_protocols():
    """Servicemanager protocols supported by the installed libgbinder."""
    known = ["aidl", "aidl2", "aidl3", "aidl4"]
    for lib in ("/usr/lib/x86_64-linux-gnu/libgbinder.so.1", "/usr/lib/libgbinder.so.1",
                "/usr/lib64/libgbinder.so.1", "/usr/lib/aarch64-linux-gnu/libgbinder.so.1"):
        if os.path.exists(lib):
            try:
                with open(lib, "rb") as f:
                    blob = f.read()
                for p in ("aidl5", "aidl6"):
                    if p.encode() + b"\0" in blob:
                        known.append(p)
            except OSError:
                pass
            break
    return tuple(known)


def read_prop_file(path, key):
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                k, _, v = line.partition("=")
                if k == key:
                    return v
    except OSError:
        pass
    return ""
