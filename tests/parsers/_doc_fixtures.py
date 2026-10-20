"""Synthetic minimal file samples for extraction tests (no PII).

There is no real Word/LibreOffice on the machine, so the ``.doc`` is built by hand:
a mini OLE writer (v3, 512-byte sectors) + FIB/piece table per MS-DOC.
The other formats are tiny valid files of the same kind the portal serves.
"""

from __future__ import annotations

import io
import struct
import zipfile

_END = 0xFFFFFFFE
_FREE = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD


def _dir_entry(
    name: str, kind: int, left: int, right: int, child: int, start: int, size: int
) -> bytes:
    raw = name.encode("utf-16-le") + b"\x00\x00" if name else b""
    e = raw.ljust(64, b"\x00") + struct.pack("<HBB", len(raw), kind, 1)
    e += struct.pack("<III", left & 0xFFFFFFFF, right & 0xFFFFFFFF, child & 0xFFFFFFFF)
    e += b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 16
    e += struct.pack("<IQ", start, size)
    return e


def make_ole(streams: dict[str, bytes]) -> bytes:
    """Two streams (WordDocument, 0Table), each padded to ≥4096 → regular sectors."""
    names = ["WordDocument", "0Table"]
    padded = {
        n: streams[n].ljust(max(4096, -(-len(streams[n]) // 512) * 512), b"\x00")
        for n in names
    }
    fat = {0: _FATSECT, 1: _END}
    sect = 2
    starts: dict[str, int] = {}
    body = b""
    for n in names:
        cnt = len(padded[n]) // 512
        starts[n] = sect
        for k in range(cnt):
            fat[sect + k] = sect + k + 1 if k < cnt - 1 else _END
        sect += cnt
        body += padded[n]
    fat_sec = b"".join(struct.pack("<I", fat.get(i, _FREE)) for i in range(128))
    dirs = (
        _dir_entry("Root Entry", 5, -1, -1, 1, _END, 0)
        + _dir_entry(
            "WordDocument",
            2,
            2,
            -1,
            -1,
            starts["WordDocument"],
            len(padded["WordDocument"]),
        )
        + _dir_entry("0Table", 2, -1, -1, -1, starts["0Table"], len(padded["0Table"]))
        + _dir_entry("", 0, -1, -1, -1, 0, 0)
    )
    hdr = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
    hdr += struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6
    hdr += struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, _END, 0, _END, 0)
    hdr += struct.pack("<I", 0) + struct.pack("<I", _FREE) * 108
    return hdr + fat_sec + dirs + body


def make_doc(text: str, *, compressed: bool = True, encrypted: bool = False) -> bytes:
    """A Word-97 document with a single text piece (compressed=cp1252 / UTF-16)."""
    raw = text.encode("cp1252") if compressed else text.encode("utf-16-le")
    fc_text = 0x800
    wd = bytearray(fc_text) + raw
    # FIB: base(32) csw=14 (28 bytes) cslw=22 (88 bytes) cbRgFcLcb=93 pairs
    struct.pack_into("<HH", wd, 0, 0xA5EC, 0x00C1)
    flags = (0x0100 if encrypted else 0) | 0x0200 * 0  # 0Table
    struct.pack_into("<H", wd, 0x0A, flags)
    pos = 32
    struct.pack_into("<H", wd, pos, 14)
    pos += 2 + 28
    struct.pack_into("<H", wd, pos, 22)
    lw = pos + 2
    struct.pack_into("<i", wd, lw + 12, len(text))  # ccpText
    pos = lw + 22 * 4
    struct.pack_into("<H", wd, pos, 93)
    pos += 2
    fc = fc_text * 2 if compressed else fc_text
    if compressed:
        fc |= 0x40000000
    plc = struct.pack("<II", 0, len(text)) + struct.pack("<HIH", 0, fc, 0)
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc
    struct.pack_into("<II", wd, pos + 33 * 8, 0, len(clx))
    return make_ole({"WordDocument": bytes(wd), "0Table": clx})


def make_odt(paragraphs: list[str]) -> bytes:
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    xml = (
        '<?xml version="1.0"?><office:document-content xmlns:office="o" xmlns:text="t">'
        f"<office:body><office:text>{body}</office:text></office:body></office:document-content>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", xml)
    return buf.getvalue()


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, d in members.items():
            zf.writestr(n, d)
    return buf.getvalue()


RTF_SAMPLE = (
    rb"{\rtf1\ansi\ansicpg1250\deff0{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
    rb"{\*\generator Hidden;}\pard Tehni\'e8ka specifikacija\par Cena: 5 \u269?a\'e8\par}"
)
