# SPDX-License-Identifier: GPL-3.0-or-later
"""The Android versions a device can run, where their images come from, and what each image
needs to run in a Waydroid Manager container (docs/spike-findings.md has the why of each quirk).

Every version comes with Google Play: in the image itself, or added as a layer ("gapps").
"""

_OFFICIAL = ("https://ota.waydro.id/system/lineage/waydroid_x86_64/GAPPS.json",
             "https://ota.waydro.id/vendor/waydroid_x86_64/MAINLINE.json")


def _atv(channel):
    base = "https://waydroid-atv.github.io/ota/" + channel
    return (base + "/system/lineage/waydroid_x86_64/GAPPS.json", base + "/vendor/waydroid_x86_64/MAINLINE.json")


# MindTheGapps 14.0.0 also serves Android 15: 15.0.0's GSF hides the gservices provider
MTG14 = ("https://github.com/s1204IT/MindTheGappsBuilder/releases/download/20250330/"
         "MindTheGapps-14.0.0-x86_64-20250330.zip",
         "ea6738b4908c1290447f610f365e84d46ca894c9a65eef90b83030d769990d7f")

# key -> settings of that version:
#   sdk          API level both images must report
#   ota          (system channel, vendor channel, version): newest build of that version
#   zip          (url, sha256, built): one pinned zip holding system.img and vendor.img
#   gb           approximate download size, for display
#   gapps        "image" (built in) | "mtg14" (MindTheGapps layer) | "gms_apex" (unpack the image's GMS APEX)
#   provision    the image has no setup wizard of its own: mark the device set up after first boot
#   software     how it renders on the CPU: stock Waydroid's way (gralloc.default, the default),
#                "vkms" (see gpu.VKMS_PROPS): 17 has no gralloc.default mapper, and 14's hwcomposer
#                can't show gralloc.default buffers; or False: it can't (15's hwcomposer shows
#                none of the CPU-mappable buffers its image's grallocs make)
#   nvidia       guest build for the NVIDIA path (see daemon/nvidia.py), or None
#   props        Android properties the image always needs; nvidia_props on the NVIDIA path,
#                software_props on the vkms path
#   loop         apexd needs loop devices in the container
#   videodev     ueventd needs /sys/class/video4linux on the host
#   experimental shown as such: a single maintainer's build or a pre-release
VERSIONS = {
    "11": dict(sdk=30, ota=_OFFICIAL + ("18.1",), gb=1.1),
    "13": dict(sdk=33, ota=_OFFICIAL + ("20.0",), gb=1.4, nvidia="full13"),
    "14": dict(sdk=34, gb=1.1, gapps="mtg14", provision=True, software="vkms", nvidia="venus", loop=True,
               zip=("https://github.com/WayDroid-ATV/waydroid-builds/releases/download/20260125/"
                    "lineage-21.0-20260125-UNOFFICIAL-waydroid_x86_64.zip",
                    "68019da4e0629d9cb1ba5abb0ec385091a2172e441aec78768f7e473d6f581eb", "20260125"),
               props={"gralloc.override": "0", "ro.hardware.gralloc": "minigbm_gbm_mesa"},
               software_props={"ro.hardware.gralloc": "minigbm_generic"}),
    "15": dict(sdk=35, gb=1.2, gapps="mtg14", software=False, nvidia="venus", experimental=True, loop=True,
               zip=("https://huggingface.co/datasets/Minhmc2077/My_Binary_Build/resolve/main/"
                    "LineageOS-22.2-WayDroidx86_64-Vanilla.zip",
                    "3febfdb12f7a930315272df102b9907ffc65fc21570e5b6d005bbd4de7f6685c", "20261005"),
               props={"ro.gralloc.override": "0", "ro.hardware.gralloc": "minigbm_gbm_mesa", "qemu.hw.mainkeys": "1"}),
    "16": dict(sdk=36, ota=_atv("a16-qpr2") + ("23.2",), gb=1.6, nvidia="venus", loop=True,
               props={"qemu.hw.mainkeys": "1"}, nvidia_props={"ro.waydroid.override_props": "0"}),
    "17": dict(sdk=37, ota=_atv("a17") + ("24.0",), gb=1.7, nvidia="venus", experimental=True,
               gapps="gms_apex", loop=True, videodev=True, software="vkms",
               props={"qemu.hw.mainkeys": "1"},
               nvidia_props={"ro.waydroid.override_props": "0", "debug.hwui.renderer": "skiavk"}),
}
DEFAULT = "13"


def get(key, field, default=None):
    return VERSIONS[key].get(field, default)


def key_for_sdk(sdk):
    """The version an image of this API level is, or None (e.g. stock Waydroid on Android 10)."""
    for k, v in VERSIONS.items():
        if v["sdk"] == sdk:
            return k
    return None


def label(key):
    return "Android {}{}".format(key, " (experimental)" if get(key, "experimental") else "")
