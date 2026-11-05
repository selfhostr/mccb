"""Attachment content-type detection: MIME tag > magic bytes > extension."""
from __future__ import annotations

import mimetypes
import os
import re

_EXTRA = {
    ".msg": "application/vnd.ms-outlook", ".eml": "message/rfc822", ".ics": "text/calendar",
    ".vcf": "text/vcard", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docm": "application/vnd.ms-word.document.macroEnabled.12",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".7z": "application/x-7z-compressed", ".rar": "application/vnd.rar",
    ".webp": "image/webp", ".heic": "image/heic", ".svg": "image/svg+xml",
    ".dat": "application/octet-stream", ".p7s": "application/pkcs7-signature",
    ".p7m": "application/pkcs7-mime", ".rtf": "application/rtf", ".csv": "text/csv",
    ".log": "text/plain", ".md": "text/markdown", ".json": "application/json",
}

_MAGIC = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", None),  # checked below
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"{\\rtf", "application/rtf"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"\x1f\x8b", "application/gzip"),
    (b"BZh", "application/x-bzip2"),
    (b"\xfd7zXZ\x00", "application/x-xz"),
    (b"ID3", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"\x78\x9f\x3e\x22", "application/ms-tnef"),
    (b"BEGIN:VCALENDAR", "text/calendar"),
    (b"BEGIN:VCARD", "text/vcard"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"MZ", "application/x-msdownload"),
]
_ZIP_EXT = {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm", ".odt", ".ods", ".odp",
            ".jar", ".apk", ".epub", ".xps", ".vsdx", ".kmz"}
_OLE_EXT = {".doc", ".xls", ".ppt", ".msg", ".vsd", ".pub", ".mpp", ".dot", ".xlt", ".pps"}


def by_extension(name: str | None) -> str | None:
    if not name:
        return None
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        return None
    if ext in _EXTRA:
        return _EXTRA[ext]
    t, _ = mimetypes.guess_type("x" + ext, strict=False)
    return t


def by_content(data: bytes, name: str | None = None) -> str | None:
    head = bytes(data[:64])
    ext = os.path.splitext(name or "")[1].lower()
    for sig, t in _MAGIC:
        if head.startswith(sig):
            if sig == b"BM":
                if len(head) > 14 and head[6:10] == b"\x00\x00\x00\x00":
                    return "image/bmp"
                continue
            if sig == b"MZ" and ext not in ("", ".exe", ".dll"):
                continue
            return t
    if head[:4] == b"RIFF" and len(head) >= 12:
        return {b"WAVE": "audio/wav", b"AVI ": "video/x-msvideo", b"WEBP": "image/webp"}.get(head[8:12])
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1"):
            return "image/heic"
        if brand.startswith(b"qt"):
            return "video/quicktime"
        return "video/mp4"
    if head.startswith(b"PK\x03\x04"):
        if ext in _ZIP_EXT:
            return by_extension(name)
        window = bytes(data[:2048])
        if b"word/" in window:
            return _EXTRA[".docx"]
        if b"xl/" in window:
            return _EXTRA[".xlsx"]
        if b"ppt/" in window:
            return _EXTRA[".pptx"]
        if b"mimetypeapplication/vnd.oasis.opendocument.text" in window:
            return _EXTRA[".odt"]
        return "application/zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if ext in _OLE_EXT:
            return by_extension(name)
        return "application/x-ole-storage"
    low = head.lstrip().lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html"):
        return "text/html"
    if low.startswith(b"<?xml"):
        return by_extension(name) or "application/xml"
    if low.startswith(b"<svg"):
        return "image/svg+xml"
    return None


def _looks_text(data: bytes) -> bool:
    sample = bytes(data[:4096])
    if not sample or b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as ex:
        if ex.start < len(sample) - 4:
            return False
    ctrl = sum(1 for b in sample if b < 32 and b not in (9, 10, 13, 12))
    return ctrl == 0


_VALID_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_GENERIC = {"application/octet-stream", "application/x-unknown", "application/unknown",
            "application/binary", "binary/octet-stream", "application/x-msdownload-unknown"}


def detect(data: bytes, name: str | None, mime_tag: str | None) -> str:
    tag = (mime_tag or "").strip().lower().split(";", 1)[0].strip()
    if tag == "image/jpg" or tag == "image/pjpeg":
        tag = "image/jpeg"
    if tag and _VALID_MIME.match(tag) and tag not in _GENERIC:
        return tag
    t = by_content(data, name) or by_extension(name)
    if t:
        return t
    if _looks_text(data):
        return "text/plain"
    return "application/octet-stream"


def extension_for(mime: str) -> str:
    for ext, t in _EXTRA.items():
        if t == mime:
            return ext
    ext = mimetypes.guess_extension(mime, strict=False)
    if mime == "image/jpeg":
        ext = ".jpg"
    return ext or ".bin"
