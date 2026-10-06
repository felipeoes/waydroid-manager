# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading Android's frames out of another GPU's memory through libgbm. Mapped for reading, Mesa
has the GPU copy a buffer into cached memory first; reading VRAM straight through the PCI BAR is
uncached and an order of magnitude slower (11 ms for a 720p frame on an AMD iGPU, against 0.8)."""
import ctypes
import ctypes.util
import os

IMPORT_FD_MODIFIER = 0x5504
TRANSFER_READ = 1
_U32, _P = ctypes.c_uint32, ctypes.c_void_p


class _ImportFdModifier(ctypes.Structure):
    _fields_ = [("width", _U32), ("height", _U32), ("format", _U32), ("num_fds", _U32),
                ("fds", ctypes.c_int * 4), ("strides", ctypes.c_int * 4), ("offsets", ctypes.c_int * 4),
                ("modifier", ctypes.c_uint64)]


class Device:
    def __init__(self, node):
        lib = self.lib = ctypes.CDLL(ctypes.util.find_library("gbm") or "libgbm.so.1")
        lib.gbm_create_device.argtypes, lib.gbm_create_device.restype = [ctypes.c_int], _P
        lib.gbm_bo_import.argtypes, lib.gbm_bo_import.restype = [_P, _U32, _P, _U32], _P
        lib.gbm_bo_map.argtypes = [_P, _U32, _U32, _U32, _U32, _U32, ctypes.POINTER(_U32), ctypes.POINTER(_P)]
        lib.gbm_bo_map.restype = _P
        lib.gbm_bo_unmap.argtypes = [_P, _P]
        lib.gbm_bo_destroy.argtypes = [_P]
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        self.dev = lib.gbm_create_device(fd)
        if not self.dev:
            os.close(fd)
            raise OSError("libgbm can't use " + node)

    def import_buffer(self, fd, width, height, fmt, offset, stride, modifier):
        """The dmabuf as a gbm buffer (holding its own reference), or None."""
        data = _ImportFdModifier(width, height, fmt, 1, (ctypes.c_int * 4)(fd), (ctypes.c_int * 4)(stride),
                                 (ctypes.c_int * 4)(offset), modifier)
        return self.lib.gbm_bo_import(self.dev, IMPORT_FD_MODIFIER, ctypes.byref(data), 0) or None

    def read(self, bo, width, height, mem, stride):
        """Copy the buffer's pixels into mem, rows stride bytes apart; False if it can't be mapped."""
        src_stride, handle = _U32(), _P()
        src = self.lib.gbm_bo_map(bo, 0, 0, width, height, TRANSFER_READ, ctypes.byref(src_stride),
                                  ctypes.byref(handle))
        if not src:
            return False
        try:
            dst = ctypes.addressof(ctypes.c_char.from_buffer(mem))
            if src_stride.value == stride:
                ctypes.memmove(dst, src, stride * height)
            else:
                row = min(src_stride.value, stride)
                for y in range(height):
                    ctypes.memmove(dst + y * stride, src + y * src_stride.value, row)
        finally:
            self.lib.gbm_bo_unmap(bo, handle)
        return True

    def free(self, bo):
        self.lib.gbm_bo_destroy(bo)
