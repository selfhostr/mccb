"""Builds a small Outlook .msg (a compound file with the MAPI streams of MS-OXMSG) with no library: sectors only,
every stream padded to the 4096 bytes of the mini-stream cutoff so that no mini FAT is needed."""
from __future__ import annotations

import struct
from datetime import datetime, timezone

END, FATSECT, FREE, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF, 0xFFFFFFFF


def build_cfb(tree: dict) -> bytes:
    """tree: {name: bytes (stream) | dict (storage)}. The root has no name."""
    entries: list[dict] = [{"name": "Root Entry", "type": 5, "data": b"", "child": NOSTREAM, "right": NOSTREAM}]

    def add(children: dict) -> int:
        ids = []
        for name, val in children.items():
            e = {"name": name, "type": 2 if isinstance(val, bytes) else 1, "child": NOSTREAM, "right": NOSTREAM,
                 "data": val if isinstance(val, bytes) else b""}
            entries.append(e)
            ids.append(len(entries) - 1)
            if isinstance(val, dict):
                e["child"] = add(val)
        for a, b in zip(ids, ids[1:]):
            entries[a]["right"] = b
        return ids[0] if ids else NOSTREAM

    entries[0]["child"] = add(tree)
    for e in entries:
        if e["type"] == 2 and len(e["data"]) < 4096:
            e["data"] = e["data"].ljust(4096, b"\x00")
    n_dir = -(-len(entries) // 4)
    n_data = [-(-len(e["data"]) // 512) if e["type"] == 2 else 0 for e in entries]
    total = n_dir + sum(n_data)
    n_fat = 1
    while n_fat * 128 < total + n_fat:
        n_fat += 1
    fat = [FREE] * (n_fat * 128)
    for i in range(n_fat):
        fat[i] = FATSECT
    nxt = n_fat
    first_dir = nxt
    for i in range(n_dir):
        fat[nxt + i] = nxt + i + 1 if i < n_dir - 1 else END
    nxt += n_dir
    for e, n in zip(entries, n_data):
        if n:
            e["start"] = nxt
            for i in range(n):
                fat[nxt + i] = nxt + i + 1 if i < n - 1 else END
            nxt += n
        else:
            e["start"] = END if e["type"] != 1 else 0
    header = bytearray(512)
    header[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, n_fat, first_dir, 0, 4096, END, 0, END, 0)
    difat = [i for i in range(n_fat)] + [FREE] * (109 - n_fat)
    struct.pack_into("<109I", header, 76, *difat)
    out = bytearray(header)
    out += struct.pack(f"<{len(fat)}I", *fat)
    dir_blob = bytearray()
    for e in entries:
        raw = e["name"].encode("utf-16-le")
        rec = bytearray(128)
        rec[0:len(raw)] = raw
        struct.pack_into("<HBB", rec, 64, len(raw) + 2, e["type"], 1)
        struct.pack_into("<III", rec, 68, NOSTREAM, e["right"], e["child"])
        struct.pack_into("<IQ", rec, 116, e["start"], len(e["data"]) if e["type"] == 2 else 0)
        dir_blob += rec
    dir_blob = dir_blob.ljust(n_dir * 512, b"\x00")
    for i in range(n_dir * 4 - len(entries)):  # unused entries must read as free
        pos = (len(entries) + i) * 128
        struct.pack_into("<III", dir_blob, pos + 68, NOSTREAM, NOSTREAM, NOSTREAM)
    out += dir_blob
    for e, n in zip(entries, n_data):
        if n:
            out += e["data"].ljust(n * 512, b"\x00")
    return bytes(out)


def s(pid: int, text: str) -> tuple[str, bytes]:
    return f"__substg1.0_{pid:04X}001F", text.encode("utf-16-le")


def prop(pid: int, typ: int, value: bytes) -> bytes:
    return struct.pack("<IIq", (pid << 16) | typ, 6, 0)[:8] + value.ljust(8, b"\x00")


def make_msg(subject: str = "Quarterly report", sender: tuple[str, str] = ("Alice Martin", "alice@example.test"),
             to: tuple[str, str] = ("Bob", "bob@example.test"), body: str = "Hello Bob,\nsee attached.\n",
             attachment: tuple[str, bytes] | None = ("report.txt", b"A" * 5000)) -> bytes:
    ft = int((datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10**7)
    top_props = bytes(32) + prop(0x0E06, 0x0040, struct.pack("<Q", ft))
    tree: dict = dict([s(0x0037, subject), s(0x0C1A, sender[0]), s(0x0C1F, sender[1]), s(0x1000, body),
                       s(0x1035, "<msg-1@example.test>")])
    tree["__properties_version1.0"] = top_props
    tree["__recip_version1.0_#00000000"] = {"__properties_version1.0": bytes(8) + prop(0x0C15, 0x0003, struct.pack("<I", 1)),
                                            **dict([s(0x3003, to[1]), s(0x3001, to[0])])}
    if attachment:
        tree["__attach_version1.0_#00000000"] = {
            "__properties_version1.0": bytes(8), "__substg1.0_37010102": attachment[1],
            **dict([s(0x3707, attachment[0]), s(0x370E, "text/plain")])}
    return build_cfb(tree)
