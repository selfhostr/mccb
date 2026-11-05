"""Access to libpff features that pypff does not expose to Python.

pypff has no binding for ``libpff_attachment_get_item`` (embedded message of an
attachment with PR_ATTACH_METHOD = 5).  The C symbols are exported by the
pypff extension module, so we call them through ctypes and wrap the result
with pypff's own ``pypff_item_create`` (the Python object then owns the item).
The pointer read is validated against ``identifier`` before use.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import POINTER, byref, c_char_p, c_int, c_size_t, c_uint32, c_void_p, py_object

import pypff

_lock = threading.Lock()
_lib = None
_ok = None


def _load():
    global _lib, _ok
    if _ok is not None:
        return _ok
    with _lock:
        if _ok is not None:
            return _ok
        try:
            lib = ctypes.PyDLL(pypff.__file__)
            lib.libpff_attachment_get_item.argtypes = [c_void_p, POINTER(c_void_p), POINTER(c_void_p)]
            lib.libpff_attachment_get_item.restype = c_int
            lib.libpff_item_get_identifier.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_void_p)]
            lib.libpff_item_get_identifier.restype = c_int
            lib.pypff_item_create.argtypes = [c_void_p, py_object]
            lib.pypff_item_create.restype = py_object
            lib.libpff_error_free.argtypes = [POINTER(c_void_p)]
            lib.libpff_error_free.restype = c_int
            lib.libpff_error_sprint.argtypes = [c_void_p, c_char_p, c_size_t]
            lib.libpff_error_sprint.restype = c_int
            _lib = lib
            _ok = pypff.attachment.__basicsize__ >= object.__basicsize__ + ctypes.sizeof(c_void_p)
        except (OSError, AttributeError):
            _ok = False
    return _ok


def _err_text(err: c_void_p) -> str:
    if not err.value:
        return "unknown libpff error"
    buf = ctypes.create_string_buffer(512)
    try:
        _lib.libpff_error_sprint(err, buf, 512)
    finally:
        _lib.libpff_error_free(byref(err))
    return buf.value.decode("utf-8", "replace").strip()


def embedded_message(attachment):
    """Return the embedded pypff message of an attachment (method 5)."""
    if not isinstance(attachment, pypff.attachment):
        raise TypeError("not a pypff.attachment")
    if not _load():
        raise RuntimeError("embedded messages not supported by this pypff build")
    ptr = c_void_p.from_address(id(attachment) + object.__basicsize__).value
    if not ptr:
        raise RuntimeError("attachment has no libpff item")
    ident = c_uint32()
    err = c_void_p()
    if _lib.libpff_item_get_identifier(ptr, byref(ident), byref(err)) != 1:
        raise RuntimeError(_err_text(err))
    if ident.value != attachment.identifier:
        raise RuntimeError("pypff object layout mismatch")
    out = c_void_p()
    err = c_void_p()
    if _lib.libpff_attachment_get_item(ptr, byref(out), byref(err)) != 1 or not out.value:
        raise RuntimeError(_err_text(err))
    return _lib.pypff_item_create(out.value, attachment)
