# SPDX-License-Identifier: GPL-3.0-or-later
"""Device model presets.

Android derives ro.product.{brand,manufacturer,model,device,name} (and from
them Build.* and the build fingerprint) from the first non-empty
ro.product.<source>.* in the order "waydroid,product,odm,vendor,system_ext,
system". Setting ro.product.waydroid.* in the instance's vendor/waydroid.prop
therefore changes what apps see as the device.
"""
from collections import OrderedDict

FIELDS = ("brand", "manufacturer", "model", "device", "name")

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
    """ro.product.waydroid.* props for a preset ('custom' takes them from custom_props)."""
    if key == "custom":
        src = custom_props or {}
        return {"ro.product.waydroid." + f: src["ro.product.waydroid." + f]
                for f in FIELDS if src.get("ro.product.waydroid." + f)}
    values = PRESETS.get(key, PRESETS["waydroid"])[2]
    return {"ro.product.waydroid." + f: v for f, v in values.items()}
