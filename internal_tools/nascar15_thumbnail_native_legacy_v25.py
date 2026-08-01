#!/usr/bin/env python3
"""NASCAR 15 native thumbnail expansion backend v2.5.

App integration API:
* install_new_thumbnail(game, uid, donor_uid, image_path)
* replace_existing_thumbnail(game, uid, image_path)
* install_or_replace_thumbnail(game, uid, donor_uid, image_path)

This module retains the standalone research CLI for diagnostics.

NASCAR 15 thumbnail create + import v2.5 guarded native expansion.

This rebuilds one 2DRIVERSELECTTD_*.ARC container with a genuinely new
PAINTSCHEME_<UID> resource.

It keeps the v2.3 footer/count correction, but fixes v2.3's remaining identity
mistake: table word 2 is not a second relocatable name reference. The proven
working PAINTSCHEME_25599 slot keeps its donor identity in word 2 and points to
its visible new name only through word 6. v2.5 also rebuilds the bank-wide directory header stored inside the first texture wrapper. v2.3/v2.4 left those inner offsets at the old 10-entry layout, which made the game reject the entire container.

Commands:
  py nascar15_thumbnail_create_and_import_v2_5.py analyze --uid 25598 --donor-uid 25599 --image thumbnail_test_25598.png
  py nascar15_thumbnail_create_and_import_v2_5.py apply   --uid 25598 --donor-uid 25599 --image thumbnail_test_25598.png
  py nascar15_thumbnail_create_and_import_v2_5.py restore --uid 25598
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import struct
import zlib
from pathlib import Path

import containers as C
import nascar15_thumbnail_import_probe_v1 as thumb
import nascar15_true_extra_scheme_preview_persistence_probe_v0_10 as v10


def _parse_native(raw):
    """Parse an ARCC bank using v2.5-native base semantics.

    FIX (v1.0.2-dev6): this module reads the bank directory -- the 0x42
    resource-order record and the 0xFD name-area record -- from the 0x20 bytes
    at ``parsed.base``.  Under the v1.0 parser, base was 0x80 + logical*32,
    which lands exactly on that record pair (physical == 2*logical + 2, so
    0x80 + logical*32 == table_end - 0x20).

    dev5 re-pointed v10.parse_multi_arc at containers.parse_multi_arc, whose
    base is the *physical* table end.  Every directory read therefore shifted
    forward by 0x20 and landed on the first texture header instead, which is
    why the check reported values straight out of the DXT wrapper:
        data_section_bytes = 0x01000100  -> the (256, 256) second dims pair
        order_table_bytes  = 0x35        -> the '5' of the 'DXT5' fourcc
        name_pointer       = 0x89D0A4A7  -> the texture hash field
        name_area_bytes    = 0x9224      -> raw DXT payload bytes
    ...and then every "expandable base container" fallback re-ran the same bad
    assumption, so all of them failed too.

    Measured across the full archive map (2,689 containers): with dev5's base
    0/1950 containers pass the directory-header check; with the base corrected
    here, 1785/1950 pass and 21/21 2DRIVERSELECTTD_* banks pass.

    As of dev8 the correction lives in v10.parse_multi_arc itself, because
    team_assets parses independently and hands its own MultiArc to the
    validators below -- fixing only this module left the transfer path broken.
    This wrapper is kept so the intent stays documented at the call sites.
    """
    return v10.parse_multi_arc(raw)


SCRIPT_DIR = Path(__file__).resolve().parent
VERSION = "2.5"
ARCHIVE_ALIGN = 0x10
CONTAINER_ALIGN = 0x80
TABLE_RECORD_SIZE = 0x20
HEADER_SIZE = 0x80
FOOTER_PREFIX_SIZE = 0x20


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _native_texture_entry(raw: bytes, name: str) -> dict:
    entries, _ = C.parse_multi_arc(raw)
    hit = next((e for e in entries if e["name"] == name), None)
    if hit is None:
        raise ValueError(f"native texture entry not found: {name}")
    return hit


def _native_payload_size(raw: bytes, name: str) -> int:
    size = int(_native_texture_entry(raw, name)["payload_size"])
    if size <= 0:
        raise ValueError(f"{name} has no native pixel payload")
    return size


def _native_directory_start(raw: bytes) -> int:
    """Return the first physical order/name directory byte in a native bank."""
    if len(raw) < 0x90 or raw[:4] != b"ARCC":
        raise ValueError("not a native ARCC texture bank")
    count = struct.unpack_from("<I", raw, 4)[0]
    table_end = 0x80 + int(count) * 16
    if count <= 0 or table_end > len(raw):
        raise ValueError("invalid native physical record table")
    starts = []
    for i in range(int(count)):
        _key, data_off, _name_ref, packed = struct.unpack_from("<4I", raw, 0x80 + i*16)
        typ = int(packed) & 0xFF
        if typ in (0x42, 0xFD):
            starts.append(table_end + int(data_off))
    if not starts:
        raise ValueError("native order/name directory records are missing")
    start = min(starts)
    if start < table_end or start > len(raw):
        raise ValueError("native directory start is out of bounds")
    return start


def _validate_native_pixel_only(before: bytes, after: bytes, name: str) -> None:
    if len(before) != len(after):
        raise ValueError("native pixel import changed container size")
    b = _native_texture_entry(before, name); a = _native_texture_entry(after, name)
    for key in ("payload_abs", "payload_size", "w", "h", "fmt", "layout"):
        if b.get(key) != a.get(key):
            raise ValueError("native pixel import changed target geometry or bounds")
    lo = int(b["payload_abs"]); hi = lo + int(b["payload_size"])
    for i, (x, y) in enumerate(zip(before, after)):
        if x != y and not (lo <= i < hi):
            raise ValueError(f"native pixel import touched byte {i:#x} outside {name}")
    C.multi_read_png(after, a)


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) // boundary * boundary


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _append_at(path: Path, offset: int, data: bytes) -> None:
    with path.open("r+b") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        if end > offset:
            raise ValueError("archive changed during thumbnail planning")
        if end < offset:
            fh.write(b"\0" * (offset - end))
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
        fh.seek(offset)
        if fh.read(len(data)) != data:
            raise ValueError("expanded thumbnail container readback mismatch")


def manifest_paths(uid: int):
    stem = SCRIPT_DIR / f"thumbnail_native_expansion_v25_{uid}"
    return (
        Path(str(stem) + ".manifest.json"),
        Path(str(stem) + ".cdf1.bak"),
        Path(str(stem) + ".container.bak"),
    )


def _check_retired_v22(uid: int) -> None:
    old = SCRIPT_DIR / f"thumbnail_create_import_{uid}.manifest.json"
    if not old.exists():
        return
    try:
        info = json.loads(old.read_text(encoding="utf-8"))
    except Exception:
        return
    if info.get("applied") and not info.get("restored"):
        raise RuntimeError(
            "the retired v2.2 expansion is still marked active. Restore it first with:\n"
            f"  py .\\nascar15_thumbnail_create_and_import_v2.py restore --uid {uid}"
        )


def _check_active_v23(uid: int) -> None:
    old = SCRIPT_DIR / f"thumbnail_native_expansion_v23_{uid}.manifest.json"
    if not old.exists():
        return
    try:
        info = json.loads(old.read_text(encoding="utf-8"))
    except Exception:
        return
    if info.get("applied") and not info.get("restored"):
        raise RuntimeError(
            "v2.3 is still active and is causing the missing Driver Select assets. "
            "Restore it first with:\n"
            f"  py .\\nascar15_thumbnail_create_and_import_v2_3.py restore --uid {uid}"
        )


def _check_active_v24(uid: int) -> None:
    old = SCRIPT_DIR / f"thumbnail_native_expansion_v24_{uid}.manifest.json"
    if not old.exists():
        return
    try:
        info = json.loads(old.read_text(encoding="utf-8"))
    except Exception:
        return
    if info.get("applied") and not info.get("restored"):
        raise RuntimeError(
            "v2.4 is still active and is causing the missing Driver Select assets. "
            "Restore it first with:\n"
            f"  py .\\nascar15_thumbnail_create_and_import_v2_4.py restore --uid {uid}"
        )


def _footer_bounds(raw: bytes, parsed: v10.MultiArc) -> tuple[int, bytes, list[int]]:
    footer_len = align(FOOTER_PREFIX_SIZE + parsed.count * 4, 0x10)
    footer_start = parsed.name_blob - footer_len
    if footer_start <= parsed.base:
        raise ValueError("native resource-order footer overlaps texture data")
    footer = raw[footer_start:parsed.name_blob]
    if len(footer) != footer_len:
        raise ValueError("native resource-order footer is truncated")
    order_raw = footer[FOOTER_PREFIX_SIZE:FOOTER_PREFIX_SIZE + parsed.count * 4]
    order = list(struct.unpack("<" + "I" * parsed.count, order_raw))
    if order != list(range(parsed.count)):
        raise ValueError(
            "unexpected native resource-order table; refusing to guess its layout "
            f"(found {order[:20]})"
        )
    padding = footer[FOOTER_PREFIX_SIZE + parsed.count * 4:]
    if any(padding):
        raise ValueError("native resource-order footer padding is not zero")
    # All table data offsets must end before the footer.
    for entry in parsed.entries:
        start = parsed.base + entry.data_off
        if start < parsed.base or start >= footer_start:
            raise ValueError(f"bad texture offset for {entry.name}")
    return footer_start, footer, order




def _directory_header_values(raw: bytes, parsed: v10.MultiArc, footer_start: int) -> dict:
    """Read the bank-wide directory metadata stored in entry 0's first 0x20 bytes."""
    if not parsed.entries:
        raise ValueError("container has no texture entries")
    first = parsed.base + parsed.entries[0].data_off
    if first + 0x20 > len(raw):
        raise ValueError("first texture wrapper is truncated")
    return {
        "data_section_bytes": struct.unpack_from("<I", raw, first + 0x04)[0],
        "order_table_bytes": raw[first + 0x0F],
        "name_pointer": struct.unpack_from("<I", raw, first + 0x14)[0],
        "name_area_bytes": int.from_bytes(raw[first + 0x1E:first + 0x20], "big"),
        "expected_data_section_bytes": footer_start - parsed.base,
        "expected_order_table_bytes": parsed.count * 4,
        "expected_name_pointer": parsed.name_blob - parsed.base - 0x20,
        "expected_name_area_bytes": len(raw) - parsed.name_blob,
    }


def _validate_directory_header(raw: bytes, parsed: v10.MultiArc, footer_start: int) -> dict:
    vals = _directory_header_values(raw, parsed, footer_start)
    pairs = (
        ("data_section_bytes", "expected_data_section_bytes"),
        ("order_table_bytes", "expected_order_table_bytes"),
        ("name_pointer", "expected_name_pointer"),
        ("name_area_bytes", "expected_name_area_bytes"),
    )
    bad = [(a, vals[a], vals[b]) for a, b in pairs if vals[a] != vals[b]]
    if bad:
        detail = ", ".join(f"{a}=0x{got:X} expected 0x{exp:X}" for a, got, exp in bad)
        raise ValueError("inner ARCC directory header is inconsistent: " + detail)
    first = parsed.base + parsed.entries[0].data_off
    if raw[first + 0x0C:first + 0x0F] != b"\x42\x00\x00":
        raise ValueError("unexpected first-wrapper directory marker")
    if raw[first + 0x1C:first + 0x1E] != b"\xFD\x00":
        raise ValueError("unexpected first-wrapper name-table marker")
    return vals


def _patch_directory_header(buf: bytearray, base: int, count: int, footer_start: int, name_blob: int, total_size: int) -> dict:
    if count * 4 > 0xFF:
        raise ValueError("entry count is too large for the native one-byte order-table length")
    name_size = total_size - name_blob
    if not 0 <= name_size <= 0xFFFF:
        raise ValueError("name area is too large for the native two-byte length")
    struct.pack_into("<I", buf, base + 0x04, footer_start - base)
    buf[base + 0x0F] = count * 4
    struct.pack_into("<I", buf, base + 0x14, name_blob - base - 0x20)
    buf[base + 0x1E:base + 0x20] = name_size.to_bytes(2, "big")
    return {
        "data_section_bytes": footer_start - base,
        "order_table_bytes": count * 4,
        "name_pointer": name_blob - base - 0x20,
        "name_area_bytes": name_size,
    }

def _name_preamble(name: str) -> bytes:
    raw = name.encode("latin1")
    lower_crc = zlib.crc32(raw.lower()) & 0xFFFFFFFF
    exact_crc = zlib.crc32(raw) & 0xFFFFFFFF
    return struct.pack("<II", lower_crc, exact_crc) + b"\0"


def _entry_resource_bytes(raw: bytes, parsed: v10.MultiArc, entry: v10.MultiEntry, footer_start: int) -> bytes:
    starts = sorted(parsed.base + e.data_off for e in parsed.entries)
    start = parsed.base + entry.data_off
    end = min((x for x in starts if x > start), default=footer_start)
    if end <= start or end > footer_start:
        raise ValueError(f"could not isolate native texture resource {entry.name}")
    chunk = raw[start:end]
    if len(chunk) < 96:
        raise ValueError(f"native texture resource {entry.name} is too short")
    return chunk


def _new_table_record(donor: v10.MultiEntry, data_off: int, name_ref: int) -> bytes:
    fields = list(struct.unpack("<8I", donor.table_record))
    donor_identity_ref = fields[2]
    # Proven working alias pattern (PAINTSCHEME_25599):
    #   word 2 keeps the donor/native identity reference
    #   word 5 is the relocated texture-data offset
    #   word 6 points to the visible PAINTSCHEME_<new UID> name
    # Treating word 2 as a duplicate of word 6 made v2.3's entire shared
    # driver-select container fail to resolve in-game.
    fields[5] = data_off
    fields[6] = name_ref
    out = struct.pack("<8I", *fields)
    if struct.unpack_from("<I", out, 8)[0] != donor_identity_ref:
        raise AssertionError("donor identity reference was not preserved")
    if struct.unpack_from("<I", out, 20)[0] != data_off:
        raise AssertionError("data offset was not rebuilt")
    if struct.unpack_from("<I", out, 24)[0] != name_ref:
        raise AssertionError("visible name reference was not rebuilt")
    return out


def _new_footer(old_footer: bytes, new_count: int) -> bytes:
    out = bytearray(old_footer[:FOOTER_PREFIX_SIZE])
    out += struct.pack("<" + "I" * new_count, *range(new_count))
    out += b"\0" * (align(len(out), 0x10) - len(out))
    return bytes(out)


def build_expanded_container(raw: bytes, donor_name: str, new_name: str) -> tuple[bytes, dict]:
    parsed = _parse_native(raw)
    if any(e.name == new_name for e in parsed.entries):
        raise ValueError(f"{new_name} already exists in this container")
    donor = v10.entry_by_name(parsed, donor_name)
    if (donor.width, donor.height, donor.fmt) != (256, 256, "DXT5"):
        raise ValueError(
            f"donor {donor_name} is {donor.width}x{donor.height} {donor.fmt}; expected 256x256 DXT5"
        )

    footer_start, old_footer, old_order = _footer_bounds(raw, parsed)
    old_directory = _validate_directory_header(raw, parsed, footer_start)
    old_table = raw[HEADER_SIZE:parsed.base]
    old_chunks = raw[parsed.base:footer_start]
    old_names = raw[parsed.name_blob:]
    donor_chunk = _entry_resource_bytes(raw, parsed, donor, footer_start)

    new_count = parsed.count + 1
    new_data_off = len(old_chunks)
    new_name_ref = len(old_names) + 9
    new_record = _new_table_record(donor, new_data_off, new_name_ref)
    new_footer = _new_footer(old_footer, new_count)
    new_name_record = _name_preamble(new_name) + new_name.encode("latin1") + b"\0"

    header = bytearray(raw[:HEADER_SIZE])
    struct.pack_into("<I", header, 4, new_count * 2 + 2)
    struct.pack_into("<I", header, 8, new_count)

    rebuilt = bytearray(
        bytes(header)
        + old_table
        + new_record
        + old_chunks
        + donor_chunk
        + new_footer
        + old_names
        + new_name_record
    )
    new_base = HEADER_SIZE + new_count * TABLE_RECORD_SIZE
    new_footer_start_planned = new_base + len(old_chunks) + len(donor_chunk)
    new_name_blob_planned = new_footer_start_planned + len(new_footer)
    new_directory = _patch_directory_header(
        rebuilt, new_base, new_count, new_footer_start_planned, new_name_blob_planned, len(rebuilt)
    )
    struct.pack_into("<i", rebuilt, 0x70, 0x8000 - align(len(rebuilt), CONTAINER_ALIGN))
    rebuilt = bytes(rebuilt)

    # Full structural verification.
    check = _parse_native(rebuilt)
    if check.count != new_count:
        raise ValueError("expanded container count verification failed")
    if len(rebuilt) <= len(raw):
        raise ValueError("expanded container did not grow")
    if struct.unpack_from("<I", rebuilt, 4)[0] != new_count * 2 + 2:
        raise ValueError("secondary resource count is wrong")
    if struct.unpack_from("<i", rebuilt, 0x70)[0] != 0x8000 - align(len(rebuilt), CONTAINER_ALIGN):
        raise ValueError("end-relative container pointer is wrong")

    check_footer_start, check_footer, check_order = _footer_bounds(rebuilt, check)
    checked_directory = _validate_directory_header(rebuilt, check, check_footer_start)
    if checked_directory["data_section_bytes"] != new_directory["data_section_bytes"]:
        raise ValueError("rebuilt inner directory data length did not survive parsing")
    if check_order != list(range(new_count)):
        raise ValueError("expanded resource-order table verification failed")
    if check_footer[:FOOTER_PREFIX_SIZE] != old_footer[:FOOTER_PREFIX_SIZE]:
        raise ValueError("native footer prefix changed")

    target = v10.entry_by_name(check, new_name)
    if target.data_off != new_data_off or target.name_ref != new_name_ref:
        raise ValueError("new resource table offsets do not match the rebuilt layout")
    fields = struct.unpack("<8I", target.table_record)
    donor_fields = struct.unpack("<8I", donor.table_record)
    if fields[2] != donor_fields[2] or fields[5] != new_data_off or fields[6] != new_name_ref:
        raise ValueError("new resource record identity/data/name references are inconsistent")
    actual_preamble = rebuilt[check.name_blob + new_name_ref - 9:check.name_blob + new_name_ref]
    if actual_preamble != _name_preamble(new_name):
        raise ValueError("new resource name CRC preamble is wrong")

    # Every old table record, texture resource, and name record must survive byte-identically.
    for old_entry in parsed.entries:
        new_entry = v10.entry_by_name(check, old_entry.name)
        if new_entry.table_record != old_entry.table_record:
            raise ValueError(f"existing table record changed for {old_entry.name}")
        old_chunk = _entry_resource_bytes(raw, parsed, old_entry, footer_start)
        new_chunk = _entry_resource_bytes(rebuilt, check, new_entry, check_footer_start)
        if old_entry.index == 0:
            # Entry 0 owns the bank-wide directory header. Expansion must update
            # four layout fields in its first 0x20 bytes, while preserving the
            # rest of the native wrapper and all pixels byte-for-byte.
            if new_chunk[0x20:] != old_chunk[0x20:]:
                raise ValueError(f"first texture payload/wrapper changed outside its directory header: {old_entry.name}")
            allowed = set(range(0x04, 0x08)) | {0x0F} | set(range(0x14, 0x18)) | {0x1E, 0x1F}
            for i, (a, b) in enumerate(zip(old_chunk[:0x20], new_chunk[:0x20])):
                if a != b and i not in allowed:
                    raise ValueError(f"unexpected first-wrapper byte changed at +0x{i:X}")
        elif new_chunk != old_chunk:
            raise ValueError(f"existing texture resource changed for {old_entry.name}")
        old_name_start = parsed.name_blob + old_entry.name_ref - 9
        old_name_end = parsed.name_blob + old_entry.name_ref + len(old_entry.name.encode("latin1")) + 1
        new_name_start = check.name_blob + new_entry.name_ref - 9
        new_name_end = check.name_blob + new_entry.name_ref + len(new_entry.name.encode("latin1")) + 1
        if raw[old_name_start:old_name_end] != rebuilt[new_name_start:new_name_end]:
            raise ValueError(f"existing name record changed for {old_entry.name}")

    report = {
        "old_count": parsed.count,
        "new_count": new_count,
        "old_size": len(raw),
        "new_size": len(rebuilt),
        "growth": len(rebuilt) - len(raw),
        "old_table_end": parsed.base,
        "new_table_end": check.base,
        "old_footer_start": footer_start,
        "new_footer_start": check_footer_start,
        "old_name_blob": parsed.name_blob,
        "new_name_blob": check.name_blob,
        "new_data_off": new_data_off,
        "new_name_ref": new_name_ref,
        "donor_identity_ref": struct.unpack("<8I", donor.table_record)[2],
        "donor_resource_size": len(donor_chunk),
        "old_order": old_order,
        "new_order": check_order,
        "old_inner_directory": old_directory,
        "new_inner_directory": checked_directory,
        "new_name_crc_lower": f"{zlib.crc32(new_name.lower().encode('latin1')) & 0xFFFFFFFF:08x}",
        "new_name_crc_exact": f"{zlib.crc32(new_name.encode('latin1')) & 0xFFFFFFFF:08x}",
        "old_sha256": sha(raw),
        "expanded_sha256": sha(rebuilt),
    }
    return rebuilt, report


def _build_final_candidate(game: Path, uid: int, donor_uid: int, image_path: Path):
    donor_hit = thumb.find_target(game, donor_uid)
    if not donor_hit:
        raise ValueError(f"donor PAINTSCHEME_{donor_uid} was not found")
    _, donor_row, donor_container, _donor_import_entry = donor_hit
    new_name = f"PAINTSCHEME_{uid}"
    donor_name = f"PAINTSCHEME_{donor_uid}"
    expanded, report = build_expanded_container(donor_container, donor_name, new_name)

    entries = {e["name"]: e for e in thumb.parse_multi(expanded)}
    if new_name not in entries:
        raise ValueError("thumbnail importer could not see the newly-created native resource")
    # The new entry is last, so the generic parser sees the native footer after
    # its texture and would otherwise mistake footer bytes for image data.
    # Derive the donor's exact native resource size independently of position.
    donor_parsed = _parse_native(donor_container)
    donor_footer_start, _, _ = _footer_bounds(donor_container, donor_parsed)
    donor_native_entry = v10.entry_by_name(donor_parsed, donor_name)
    _entry_resource_bytes(donor_container, donor_parsed, donor_native_entry, donor_footer_start)  # structural guard
    donor_payload_size = _native_payload_size(donor_container, donor_name)
    if donor_payload_size <= 0:
        raise ValueError("donor thumbnail has no native pixel payload")
    target_import_entry = dict(entries[new_name])
    target_import_entry["payload_size"] = donor_payload_size
    expanded_parsed = _parse_native(expanded)
    expanded_footer_start, expanded_footer_before, _ = _footer_bounds(expanded, expanded_parsed)
    if target_import_entry["payload_abs"] + target_import_entry["payload_size"] > _native_directory_start(expanded):
        raise ValueError("target thumbnail payload would overlap the native physical directory")
    image = thumb.Image.open(image_path)
    image.load()
    final, encoder = thumb.replace_payload(expanded, target_import_entry, image)
    _validate_native_pixel_only(expanded, final, new_name)
    final_parsed_guard = _parse_native(final)
    if len(final) != len(expanded):
        raise ValueError("thumbnail pixel import changed expanded container size")
    # Re-run all structural guards after pixel replacement.
    parsed_final = _parse_native(final)
    v10.entry_by_name(parsed_final, new_name)
    final_footer_start, _, _ = _footer_bounds(final, parsed_final)
    _validate_directory_header(final, parsed_final, final_footer_start)
    report.update({
        "container": donor_row["name"],
        "container_offset_before": donor_row["offset"],
        "container_size_before": donor_row["size"],
        "uid": uid,
        "donor_uid": donor_uid,
        "entry": new_name,
        "donor_entry": donor_name,
        "image": str(image_path),
        "encoder": encoder,
        "final_sha256": sha(final),
    })
    return donor_row, donor_container, final, report



def find_target(game_arg, uid: int):
    """Return the live PAINTSCHEME hit used by the app, or None."""
    game = Path(game_arg)
    return thumb.find_target(game, int(uid))


def install_new_thumbnail(game_arg, uid: int, donor_uid: int, image_path) -> dict:
    """Append/repoint a v2.5-expanded driver-select container transactionally.

    The caller may wrap this with a wider paint/database transaction. This
    function still rolls ARCHIVE1/cdfiles1 back locally if its own write fails.
    """
    game = Path(game_arg)
    uid = int(uid); donor_uid = int(donor_uid); image_path = Path(image_path)
    if thumb.find_target(game, uid):
        raise ValueError(f"PAINTSCHEME_{uid} already exists; use replace_existing_thumbnail")
    row, original, final, report = _build_final_candidate(game, uid, donor_uid, image_path)
    archive = game / "data" / "ARCHIVE1.AR"
    cdf = game / "data" / "cdfiles1.dat"
    old_archive_size = archive.stat().st_size
    old_cdf = cdf.read_bytes()
    new_offset = align(old_archive_size, ARCHIVE_ALIGN)
    try:
        _append_at(archive, new_offset, final)
        raw_cdf, rows = v10.parse_cdf_rows(cdf)
        live_row = v10.find_row(rows, row["name"])
        if live_row.offset != row["offset"] or live_row.size != row["size"]:
            raise ValueError("thumbnail container index changed during planning")
        struct.pack_into("<I", raw_cdf, live_row.offset_pos, new_offset)
        struct.pack_into("<I", raw_cdf, live_row.size_pos, len(final))
        _atomic_write(cdf, bytes(raw_cdf))

        _, check_rows = v10.parse_cdf_rows(cdf)
        check_row = v10.find_row(check_rows, live_row.name)
        indexed = v10.read_entry(archive, check_row)
        if indexed != final:
            raise ValueError("expanded thumbnail container indexed readback mismatch")
        parsed = _parse_native(indexed)
        v10.entry_by_name(parsed, f"PAINTSCHEME_{uid}")
        footer_start, _, _ = _footer_bounds(indexed, parsed)
        _validate_directory_header(indexed, parsed, footer_start)
        report.update({
            "ok": True, "method": "native_expand_v25", "uid": uid,
            "donor_uid": donor_uid, "container": row["name"],
            "archive_offset": new_offset, "container_size": len(final),
            "readback_verified": True,
        })
        return report
    except Exception as original_error:
        rollback_errors = []
        try:
            _atomic_write(cdf, old_cdf)
            with archive.open("r+b") as fh:
                fh.truncate(old_archive_size)
                fh.flush(); os.fsync(fh.fileno())
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise


def replace_existing_thumbnail(game_arg, uid: int, image_path) -> dict:
    """Replace an existing native or v2.5-created thumbnail in place safely."""
    game = Path(game_arg); uid = int(uid); image_path = Path(image_path)
    hit = thumb.find_target(game, uid)
    if not hit:
        raise ValueError(f"PAINTSCHEME_{uid} does not exist")
    archive, row, original, import_entry = hit
    parsed = _parse_native(original)
    footer_start, footer_before, _ = _footer_bounds(original, parsed)
    native_entry = v10.entry_by_name(parsed, f"PAINTSCHEME_{uid}")
    _entry_resource_bytes(original, parsed, native_entry, footer_start)  # structural guard
    payload_size = _native_payload_size(original, f"PAINTSCHEME_{uid}")
    if payload_size <= 0:
        raise ValueError("thumbnail resource has no writable native pixel payload")
    target = dict(import_entry)
    target["payload_size"] = payload_size
    image = thumb.Image.open(image_path); image.load()
    rebuilt, encoder = thumb.replace_payload(original, target, image)
    _validate_native_pixel_only(original, rebuilt, f"PAINTSCHEME_{uid}")
    parsed_after = _parse_native(rebuilt)
    footer_after_start, _footer_after, _ = _footer_bounds(rebuilt, parsed_after)
    _validate_directory_header(rebuilt, parsed_after, footer_after_start)
    try:
        with archive.open("r+b") as fh:
            fh.seek(row["offset"]); fh.write(rebuilt); fh.flush(); os.fsync(fh.fileno())
            fh.seek(row["offset"]); readback = fh.read(row["size"])
        if readback != rebuilt:
            raise ValueError("thumbnail in-place readback mismatch")
    except Exception as original_error:
        rollback_errors = []
        try:
            with archive.open("r+b") as fh:
                fh.seek(row["offset"]); fh.write(original); fh.flush(); os.fsync(fh.fileno())
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise
    return {
        "ok": True, "method": "in_place_v25", "uid": uid,
        "container": row["name"], "encoder": encoder,
        "container_size": row["size"], "readback_verified": True,
    }


def install_or_replace_thumbnail(game_arg, uid: int, donor_uid: int, image_path) -> dict:
    if find_target(game_arg, uid):
        return replace_existing_thumbnail(game_arg, uid, image_path)
    return install_new_thumbnail(game_arg, uid, donor_uid, image_path)

def cmd_analyze(args):
    if thumb.game_running():
        raise RuntimeError("NASCAR15.exe is running; close it first")
    _check_retired_v22(args.uid)
    _check_active_v23(args.uid)
    _check_active_v24(args.uid)
    game = thumb.detect_game(args.game)
    existing = thumb.find_target(game, args.uid)
    if existing:
        raise ValueError(
            f"PAINTSCHEME_{args.uid} already exists. This expansion test expects a missing slot; "
            "restore any prior thumbnail experiment first."
        )
    image_path = Path(args.image)
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    row, original, final, report = _build_final_candidate(game, args.uid, args.donor_uid, image_path)
    candidate = SCRIPT_DIR / f"thumbnail_native_expansion_v25_{args.uid}.candidate.ARC"
    report_path = SCRIPT_DIR / f"thumbnail_native_expansion_v25_{args.uid}.analysis.json"
    candidate.write_bytes(final)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("V2.5 OFFLINE ANALYSIS PASSED — NO GAME FILES CHANGED")
    print(f"Container: {row['name']}")
    print(f"Entries: {report['old_count']} -> {report['new_count']}")
    print(f"Size: {report['old_size']} -> {report['new_size']} (+{report['growth']})")
    print(f"Footer moved after the new texture: 0x{report['new_footer_start']:X}")
    print(f"Donor identity ref preserved: {report['donor_identity_ref']}")
    print(f"New visible-name CRCs: lower={report['new_name_crc_lower']} exact={report['new_name_crc_exact']}")
    print(f"Candidate: {candidate.name}")
    print(f"Report: {report_path.name}")
    print("All old records, names, pixels, and wrappers are preserved; only entry 0's four bank-directory fields are updated.")


def cmd_apply(args):
    if thumb.game_running():
        raise RuntimeError("NASCAR15.exe is running; close it first")
    _check_retired_v22(args.uid)
    _check_active_v23(args.uid)
    _check_active_v24(args.uid)
    game = thumb.detect_game(args.game)
    existing = thumb.find_target(game, args.uid)
    if existing:
        raise ValueError(
            f"PAINTSCHEME_{args.uid} already exists. Restore prior experiments before running this expansion probe."
        )
    image_path = Path(args.image)
    if not image_path.exists():
        raise FileNotFoundError(image_path)

    mf, cdf_backup, container_backup = manifest_paths(args.uid)
    if mf.exists():
        old = json.loads(mf.read_text(encoding="utf-8"))
        if old.get("applied") and not old.get("restored"):
            raise RuntimeError("v2.5 is already active for this UID; restore it first")

    row, original, final, report = _build_final_candidate(game, args.uid, args.donor_uid, image_path)
    archive = game / "data" / "ARCHIVE1.AR"
    cdf = game / "data" / "cdfiles1.dat"
    old_archive_size = archive.stat().st_size
    old_cdf = cdf.read_bytes()
    cdf_backup.write_bytes(old_cdf)
    container_backup.write_bytes(original)
    new_offset = align(old_archive_size, ARCHIVE_ALIGN)

    try:
        _append_at(archive, new_offset, final)
        raw_cdf, rows = v10.parse_cdf_rows(cdf)
        live_row = v10.find_row(rows, row["name"])
        if live_row.offset != row["offset"] or live_row.size != row["size"]:
            raise ValueError("thumbnail container index changed during planning")
        struct.pack_into("<I", raw_cdf, live_row.offset_pos, new_offset)
        struct.pack_into("<I", raw_cdf, live_row.size_pos, len(final))
        _atomic_write(cdf, bytes(raw_cdf))

        # Indexed and structural readback.
        _, check_rows = v10.parse_cdf_rows(cdf)
        check_row = v10.find_row(check_rows, live_row.name)
        indexed = v10.read_entry(archive, check_row)
        if indexed != final:
            raise ValueError("expanded thumbnail container indexed readback mismatch")
        check = _parse_native(indexed)
        v10.entry_by_name(check, f"PAINTSCHEME_{args.uid}")
        _footer_bounds(indexed, check)
        entries = {e["name"]: e for e in thumb.parse_multi(indexed)}
        if f"PAINTSCHEME_{args.uid}" not in entries:
            raise ValueError("pixel importer cannot see target after indexed readback")

        after_cdf = cdf.read_bytes()
        manifest = dict(report)
        manifest.update({
            "version": VERSION,
            "game": str(game),
            "archive_size_before": old_archive_size,
            "archive_size_after": archive.stat().st_size,
            "cdf_before_sha256": sha(old_cdf),
            "cdf_after_sha256": sha(after_cdf),
            "container_offset_after": check_row.offset,
            "container_size_after": check_row.size,
            "applied": True,
            "restored": False,
        })
        mf.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    except Exception as original_error:
        rollback_errors = []
        try:
            _atomic_write(cdf, old_cdf)
            with archive.open("r+b") as fh:
                fh.truncate(old_archive_size)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise

    print("THUMBNAIL NATIVE EXPANSION V2.5 APPLIED")
    print(f"Entry: PAINTSCHEME_{args.uid}")
    print(f"Container: {row['name']}")
    print(f"Entries: {report['old_count']} -> {report['new_count']}")
    print(f"Encoder: {report['encoder']}")
    print("The outer table/footer and entry 0's inner bank-directory offsets were rebuilt; the new slot preserves the donor identity reference.")
    print("Start NASCAR 15 and check the target driver's Paint Select.")


def cmd_restore(args):
    if thumb.game_running():
        raise RuntimeError("NASCAR15.exe is running; close it first")
    mf, cdf_backup, container_backup = manifest_paths(args.uid)
    if not mf.exists() or not cdf_backup.exists():
        raise FileNotFoundError("v2.5 manifest/backup not found")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    game = Path(manifest["game"])
    archive = game / "data" / "ARCHIVE1.AR"
    cdf = game / "data" / "cdfiles1.dat"
    current_cdf = cdf.read_bytes()
    if sha(current_cdf) != manifest["cdf_after_sha256"]:
        raise RuntimeError("cdfiles1.dat changed after v2.5; restore refused to avoid overwriting newer edits")
    if archive.stat().st_size != manifest["archive_size_after"]:
        raise RuntimeError("ARCHIVE1.AR size changed after v2.5; restore refused to avoid overwriting newer edits")
    with archive.open("r+b") as fh:
        fh.truncate(manifest["archive_size_before"])
        fh.flush()
        os.fsync(fh.fileno())
    _atomic_write(cdf, cdf_backup.read_bytes())
    manifest["restored"] = True
    mf.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("THUMBNAIL NATIVE EXPANSION V2.5 RESTORED")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["analyze", "apply", "restore"])
    parser.add_argument("--uid", type=int, default=25598)
    parser.add_argument("--donor-uid", type=int, default=25599)
    parser.add_argument("--image", default=str(SCRIPT_DIR / "thumbnail_test_25598.png"))
    parser.add_argument("--game")
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            cmd_analyze(args)
        elif args.command == "apply":
            cmd_apply(args)
        else:
            cmd_restore(args)
        return 0
    except Exception as exc:
        print("ERROR:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
