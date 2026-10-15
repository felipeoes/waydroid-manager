# SPDX-License-Identifier: GPL-3.0-or-later
"""A 64-bit WebView for images whose 32-bit renderers die (catalog "webview64"): the image's own
webview.apk with use32bitAbi cleared and re-signed, as a layer of its image set.

The images' WebView prefers its 32-bit build (use32bitAbi), so its renderers run as i386. From
Android 16, 32-bit bionic calls shutdown() as its own syscall rather than through socketcall,
and Chromium's renderer sandbox allows that syscall on every architecture but i386: the
renderer is killed (SIGSYS) and takes the app with it. 64-bit renderers aren't affected.
"""
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import zipfile

from . import layers
from .util import CommandError, log

APK = "system/product/app/webview/webview.apk"
_USE32BITABI = 0x01010515           # android:use32bitAbi
_V2_BLOCK_ID = 0x7109871A
_RSA_PKCS1_SHA256 = 0x0103
_lock = threading.Lock()            # parallel starts of one image set share its layer


def layer(system_root, d):
    """The layer d with the patched WebView of the mounted system image, built on first use.
    Returns d, or None (no WebView of that kind, or it can't be built: then 32-bit it stays)."""
    with _lock:
        return d if os.path.isdir(d) else _build(system_root, d)


def _build(system_root, d):
    src = os.path.join(system_root, APK)
    if not os.path.isfile(src):
        return None
    tmp = d + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(os.path.dirname(os.path.join(tmp, APK)))
        if not patch_apk(src, os.path.join(tmp, APK)):
            log.info("the image's WebView isn't 32-bit-preferred; no WebView layer")
            return None
        layers.set_modes(tmp)
        os.rename(tmp, d)
        return d
    except (CommandError, OSError, ValueError, zipfile.BadZipFile) as e:
        log.warning("keeping the image's 32-bit WebView: %s", e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def clear_use32bitabi(axml):
    """The binary AndroidManifest.xml with every android:use32bitAbi="true" set to false, or
    None if it has none."""
    buf = bytearray(axml)
    names, off, changed = (), 8, False
    while off + 8 <= len(buf):
        typ, hsize, size = struct.unpack_from("<HHI", buf, off)
        if size < 8:
            raise ValueError("bad AndroidManifest.xml")
        if typ == 0x0180:           # resource map: attribute name string index -> resource id
            names = struct.unpack_from("<{}I".format((size - hsize) // 4), buf, off + hsize)
        elif typ == 0x0102:         # start element
            start, asize, count = struct.unpack_from("<HHH", buf, off + hsize + 8)
            for a in range(off + hsize + start, off + hsize + start + asize * count, asize):
                name, = struct.unpack_from("<I", buf, a + 4)
                # ns, name, rawValue, then Res_value: size, res0, dataType (0x12: boolean), data
                if name < len(names) and names[name] == _USE32BITABI and buf[a + 15] == 0x12 \
                        and struct.unpack_from("<I", buf, a + 16)[0]:
                    struct.pack_into("<I", buf, a + 16, 0)
                    changed = True
        off += size
    return bytes(buf) if changed else None


def patch_apk(src, dst):
    """Write src with use32bitAbi cleared, aligned and signed to dst. False if src doesn't set it."""
    with zipfile.ZipFile(src) as zin:
        manifest = clear_use32bitabi(zin.read("AndroidManifest.xml"))
        if manifest is None:
            return False
        with zipfile.ZipFile(dst, "w") as zout:
            for info in zin.infolist():
                # the old signature no longer matches; v2 below replaces it
                if info.filename.startswith("META-INF/") and \
                        info.filename.endswith((".SF", ".RSA", ".DSA", ".EC", "MANIFEST.MF")):
                    continue
                out = zipfile.ZipInfo(info.filename, info.date_time)
                out.compress_type, out.external_attr = info.compress_type, info.external_attr
                out.file_size = info.file_size
                if info.compress_type == zipfile.ZIP_STORED:
                    # native libraries are mapped straight from the APK: page-aligned (16K pages
                    # too); everything else stored (resources.arsc) to 4 bytes
                    align = 16384 if info.filename.endswith(".so") else 4
                    data_at = zout.fp.tell() + 30 + len(info.filename.encode())
                    out.extra = b"\0" * (-data_at % align)
                if info.filename == "AndroidManifest.xml":
                    zout.writestr(out, manifest)
                    continue
                with zin.open(info) as s, zout.open(out, "w") as o:
                    shutil.copyfileobj(s, o, 1 << 20)
    sign_v2(dst)
    return True


def _lp(b):
    """A length-prefixed field of the APK signing block."""
    return struct.pack("<I", len(b)) + b


def _ssl(args, data=None):
    try:
        r = subprocess.run(["openssl"] + args, input=data, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CommandError("openssl: {}".format(e)) from e
    if r.returncode:
        raise CommandError("openssl {}: {}".format(args[0], r.stderr.decode(errors="replace").strip()))
    return r.stdout


def _digest(f, ranges):
    """APK Signature Scheme v2 content digest (SHA-256 over 1 MiB chunks) of the file ranges."""
    chunks = []
    for start, end in ranges:
        f.seek(start)
        while start < end:
            data = f.read(min(1 << 20, end - start))
            chunks.append(hashlib.sha256(b"\xa5" + struct.pack("<I", len(data)) + data).digest())
            start += len(data)
    return hashlib.sha256(b"\x5a" + struct.pack("<I", len(chunks)) + b"".join(chunks)).digest()


def sign_v2(path):
    """Sign the APK at path in place (APK Signature Scheme v2) with a throwaway key: a system
    app may change signer, and the image's WebView provider entry names no signature."""
    # ponytail: a new key per build; Android accepts a system app's signer changing, so a kept key buys nothing yet
    with open(path, "r+b") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(size - 22)
        eocd = f.read()
        if eocd[:4] != b"PK\5\6":
            raise ValueError("unexpected zip layout (archive comment or zip64)")
        cd_size, cd_off = struct.unpack_from("<II", eocd, 12)
        if cd_off + cd_size != size - 22:
            raise ValueError("unexpected zip layout")
        # entries, central directory, EOCD; the EOCD's central directory offset is digested as
        # it is now, which is where the signing block will start
        digest = _digest(f, ((0, cd_off), (cd_off, size - 22), (size - 22, size)))
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, "key.pem")
            cert = _ssl(["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-outform", "DER",
                         "-subj", "/CN=waydroid-manager WebView", "-days", "36500"])
            pub = _ssl(["pkey", "-in", key, "-pubout", "-outform", "DER"])
            signed = _lp(_lp(struct.pack("<I", _RSA_PKCS1_SHA256) + _lp(digest))) \
                + _lp(_lp(cert)) + _lp(b"")
            sig = _ssl(["dgst", "-sha256", "-sign", key], signed)
        signer = _lp(signed) + _lp(_lp(struct.pack("<I", _RSA_PKCS1_SHA256) + _lp(sig))) + _lp(pub)
        value = _lp(_lp(signer))
        pair = struct.pack("<QI", 4 + len(value), _V2_BLOCK_ID) + value
        block_size = len(pair) + 8 + 16
        block = struct.pack("<Q", block_size) + pair + struct.pack("<Q", block_size) + b"APK Sig Block 42"
        f.seek(cd_off)
        cd = f.read(cd_size)
        f.seek(cd_off)
        f.write(block + cd + eocd[:16] + struct.pack("<I", cd_off + len(block)) + eocd[20:])
