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

    def map_stride(self, bo, width, height):
        """The stride of rows as read() maps them (Mesa's staging copy, not the dmabuf's), or 0."""
        stride, handle = _U32(), _P()
        if not self.lib.gbm_bo_map(bo, 0, 0, width, height, TRANSFER_READ, ctypes.byref(stride),
                                   ctypes.byref(handle)):
            return 0
        self.lib.gbm_bo_unmap(bo, handle)
        return stride.value

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


# -- NVIDIA: through EGL ---------------------------------------------------------------
# NVIDIA's libgbm can't map its block-linear buffers (gbm_bo_map fails), so its frames are read
# by GLES on the same GPU instead: the dmabuf as an EGLImage, bound to a framebuffer, read back
# with glReadPixels, which detiles on the GPU. Rows come out as RGBA bytes: DRM's ABGR8888.

EGL_NONE, EGL_WIDTH, EGL_HEIGHT = 0x3038, 0x3057, 0x3056
EGL_PLATFORM_GBM_KHR, EGL_OPENGL_ES_API, EGL_CONTEXT_MAJOR_VERSION = 0x31D7, 0x30A0, 0x3098
EGL_LINUX_DMA_BUF_EXT, EGL_LINUX_DRM_FOURCC_EXT = 0x3270, 0x3271
EGL_DMA_BUF_PLANE0_FD_EXT, EGL_DMA_BUF_PLANE0_OFFSET_EXT, EGL_DMA_BUF_PLANE0_PITCH_EXT = 0x3272, 0x3273, 0x3274
EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT = 0x3443, 0x3444
GL_TEXTURE_2D, GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_FRAMEBUFFER_COMPLETE = 0x0DE1, 0x8D40, 0x8CE0, 0x8CD5
GL_RGBA, GL_UNSIGNED_BYTE, GL_PACK_ALIGNMENT, GL_PACK_ROW_LENGTH = 0x1908, 0x1401, 0x0D05, 0x0D02
DRM_FORMAT_MOD_INVALID = 0x00ffffffffffffff


class EglDevice:
    """Device's interface over EGL and GLES 3; for a GPU whose libgbm can't map buffers."""
    rgba = True     # read() always yields RGBA bytes, whatever the buffer's format

    def __init__(self, node):
        egl = self.egl = ctypes.CDLL("libEGL.so.1")
        gl = self.gl = ctypes.CDLL("libGLESv2.so.2")
        gbm = ctypes.CDLL(ctypes.util.find_library("gbm") or "libgbm.so.1")
        gbm.gbm_create_device.argtypes, gbm.gbm_create_device.restype = [ctypes.c_int], _P
        egl.eglGetProcAddress.argtypes, egl.eglGetProcAddress.restype = [ctypes.c_char_p], _P
        egl.eglGetPlatformDisplay.argtypes, egl.eglGetPlatformDisplay.restype = [_U32, _P, _P], _P
        egl.eglInitialize.argtypes = [_P, _P, _P]
        egl.eglCreateContext.argtypes, egl.eglCreateContext.restype = [_P, _P, _P, ctypes.POINTER(ctypes.c_int)], _P
        egl.eglMakeCurrent.argtypes = [_P, _P, _P, _P]
        egl.eglCreateImage.argtypes = [_P, _P, _U32, _P, ctypes.POINTER(ctypes.c_ssize_t)]
        egl.eglCreateImage.restype = _P
        egl.eglDestroyImage.argtypes = [_P, _P]
        gl.glReadPixels.argtypes = [ctypes.c_int] * 4 + [_U32, _U32, _P]
        self.image_target = ctypes.CFUNCTYPE(None, _U32, _P)(egl.eglGetProcAddress(b"glEGLImageTargetTexture2DOES"))
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        self.gbm = gbm.gbm_create_device(fd)        # kept: the display lives on it
        self.dpy = egl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, self.gbm, None) if self.gbm else None
        if not self.dpy or not egl.eglInitialize(self.dpy, None, None) or not egl.eglBindAPI(EGL_OPENGL_ES_API):
            raise OSError("EGL can't use " + node)
        ctx = egl.eglCreateContext(self.dpy, None, None, (ctypes.c_int * 3)(EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE))
        if not ctx or not egl.eglMakeCurrent(self.dpy, None, None, ctx):   # surfaceless, no config
            raise OSError("no GLES context on " + node)
        gl.glPixelStorei(GL_PACK_ALIGNMENT, 1)

    def import_buffer(self, fd, width, height, fmt, offset, stride, modifier):
        attrs = [EGL_WIDTH, width, EGL_HEIGHT, height, EGL_LINUX_DRM_FOURCC_EXT, fmt,
                 EGL_DMA_BUF_PLANE0_FD_EXT, fd, EGL_DMA_BUF_PLANE0_OFFSET_EXT, offset,
                 EGL_DMA_BUF_PLANE0_PITCH_EXT, stride]
        if modifier != DRM_FORMAT_MOD_INVALID:
            attrs += [EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, modifier & 0xffffffff,
                      EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, modifier >> 32]
        image = self.egl.eglCreateImage(self.dpy, None, EGL_LINUX_DMA_BUF_EXT, None,
                                        (ctypes.c_ssize_t * (len(attrs) + 1))(*attrs, EGL_NONE))
        if not image:
            return None
        tex, fbo = _U32(), _U32()
        self.gl.glGenTextures(1, ctypes.byref(tex))
        self.gl.glBindTexture(GL_TEXTURE_2D, tex)
        self.image_target(GL_TEXTURE_2D, image)
        self.gl.glGenFramebuffers(1, ctypes.byref(fbo))
        self.gl.glBindFramebuffer(GL_FRAMEBUFFER, fbo)
        self.gl.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
        bo = (image, tex, fbo)
        if self.gl.glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE:
            self.free(bo)
            return None
        return bo

    def map_stride(self, bo, width, height):
        return width * 4

    def read(self, bo, width, height, mem, stride):
        self.gl.glBindFramebuffer(GL_FRAMEBUFFER, bo[2])
        self.gl.glPixelStorei(GL_PACK_ROW_LENGTH, stride // 4)
        self.gl.glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE,
                             ctypes.addressof(ctypes.c_char.from_buffer(mem)))
        return True

    def free(self, bo):
        image, tex, fbo = bo
        self.gl.glDeleteFramebuffers(1, ctypes.byref(_U32(fbo.value)))
        self.gl.glDeleteTextures(1, ctypes.byref(_U32(tex.value)))
        self.egl.eglDestroyImage(self.dpy, image)


def open_device(node):
    """The reader for this GPU's buffers: libgbm's own mapping where it works (Mesa), EGL on NVIDIA."""
    try:
        driver = os.path.basename(os.path.realpath("/sys/class/drm/{}/device/driver".format(os.path.basename(node))))
    except OSError:
        driver = ""
    return EglDevice(node) if driver == "nvidia" else Device(node)
