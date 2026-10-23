# SPDX-License-Identifier: GPL-3.0-or-later
"""Device model presets.

Android derives ro.product.{brand,manufacturer,model,device,name} (and from
them Build.* and the build fingerprint) from the first non-empty
ro.product.<source>.* in the order "waydroid,product,odm,vendor,system_ext,
system". Setting ro.product.waydroid.* in the instance's vendor/waydroid.prop
therefore changes what apps see as the device.
"""
import zlib
from collections import OrderedDict

FIELDS = ("brand", "manufacturer", "model", "device", "name")
# Partitions whose own ro.<p>.build.* repeat the build's identity
PARTITIONS = ("system", "system_ext", "vendor", "odm", "product", "vendor_dlkm", "bootimage")
# The image's build.prop props that name its Waydroid device
IMAGE_PROPS = ("ro.build.version.release", "ro.build.id", "ro.build.version.incremental", "ro.lineage.device",
               "ro.lineage.version", "ro.lineage.display.version", "ro.modversion")

# key -> (label, kind, {field: value})
PRESETS = OrderedDict([
    ("waydroid", ("Waydroid (default)", "", {})),
    ("galaxy_s24_ultra", ("Samsung Galaxy S24 Ultra", "phone", {
        "brand": "samsung", "manufacturer": "samsung", "model": "SM-S928B", "device": "e3q", "name": "e3qxxx"})),
    ("galaxy_s23", ("Samsung Galaxy S23", "phone", {
        "brand": "samsung", "manufacturer": "samsung", "model": "SM-S911B", "device": "dm1q", "name": "dm1qxxx"})),
    ("galaxy_tab_s9", ("Samsung Galaxy Tab S9", "tablet", {
        "brand": "samsung", "manufacturer": "samsung", "model": "SM-X710", "device": "gts9wifi",
        "name": "gts9wifixx"})),
    ("pixel_8_pro", ("Google Pixel 8 Pro", "phone", {
        "brand": "google", "manufacturer": "Google", "model": "Pixel 8 Pro", "device": "husky", "name": "husky"})),
    ("pixel_7", ("Google Pixel 7", "phone", {
        "brand": "google", "manufacturer": "Google", "model": "Pixel 7", "device": "panther", "name": "panther"})),
    ("xiaomi_14", ("Xiaomi 14", "phone", {
        "brand": "Xiaomi", "manufacturer": "Xiaomi", "model": "23127PN0CG", "device": "houji",
        "name": "houji_global"})),
    ("oneplus_12", ("OnePlus 12", "phone", {
        "brand": "OnePlus", "manufacturer": "OnePlus", "model": "CPH2581", "device": "OP595DL1",
        "name": "CPH2581"})),
    ("rog_phone_8", ("ASUS ROG Phone 8", "phone", {
        "brand": "asus", "manufacturer": "asus", "model": "ASUS_AI2401", "device": "ASUS_AI2401",
        "name": "WW_AI2401"})),
    ("custom", ("Custom", "", {})),
])


def label(key):
    return PRESETS.get(key, (key,))[0]


def props_for(key, custom_props=None):
    """ro.product.waydroid.* and build/hardware props for a preset ('custom' takes them from custom_props)."""
    src = custom_props or {}
    if key == "custom":
        out = {"ro.product.waydroid." + f: src["ro.product.waydroid." + f]
               for f in FIELDS if src.get("ro.product.waydroid." + f)}
        dev = out.get("ro.product.waydroid.device", "")
    else:
        values = PRESETS.get(key, PRESETS["pixel_8_pro"])[2] or PRESETS["pixel_8_pro"][2]
        out = {"ro.product.waydroid." + f: v for f, v in values.items()}
        dev = values["device"]
    if dev:
        for k in ("ro.build.product", "ro.product.board", "ro.hardware", "ro.bootloader"):
            if not src.get(k):
                out[k] = dev
    if not src.get("ro.build.tags"):
        out["ro.build.tags"] = "release-keys"
    return out


def build_props(dev_props, image):
    """What a production build reports instead of the image's userdebug one: ro.build.* and each
    partition's type, tags, fingerprint and incremental, derived from props_for() (dev_props)
    and the image's own IMAGE_PROPS (image). The image's real build id stays."""
    num = str(10000000 + zlib.crc32(image.get("ro.build.version.incremental", "").encode()) % 90000000)
    out = {"ro.build.type": "user", "ro.build.user": "android-build", "ro.build.host": "abfarm",
           "ro.build.version.incremental": num, "ro.build.display.id": image.get("ro.build.id")}
    dev = dev_props.get("ro.build.product")
    for k in ("ro.lineage.device", "ro.lineage.version", "ro.lineage.display.version", "ro.modversion"):
        if dev and "waydroid_x86_64" in image.get(k, ""):
            out[k] = image[k].replace("waydroid_x86_64", dev)
    brand, name = (dev_props.get("ro.product.waydroid." + f) for f in ("brand", "name"))
    release, build_id = image.get("ro.build.version.release"), image.get("ro.build.id")
    if dev and brand and name and release and build_id:
        out["ro.build.flavor"] = dev + "-user"
        out["ro.build.description"] = "{}-user {} {} {} release-keys".format(dev, release, build_id, num)
        out["ro.build.fingerprint"] = "{}/{}/{}:{}/{}/{}:user/release-keys".format(
            brand, name, dev, release, build_id, num)
        for part in PARTITIONS:
            out["ro.{}.build.fingerprint".format(part)] = out["ro.build.fingerprint"]
    for part in PARTITIONS:
        out["ro.{}.build.type".format(part)] = "user"
        out["ro.{}.build.tags".format(part)] = "release-keys"
        out["ro.{}.build.version.incremental".format(part)] = num
    return {k: v for k, v in out.items() if v}
