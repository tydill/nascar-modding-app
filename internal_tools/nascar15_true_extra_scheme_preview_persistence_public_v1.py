#!/usr/bin/env python3
r"""
NASCAR 15 True Extra Scheme Preview + Persistence Probe v0.10

Purpose
-------
The v0.9 PatchSetAttr + ApplyPatch route proved that a true 337th livery can
appear in AJ Allmendinger's carousel, load into a race, and survive normal menu
navigation. Its remaining visible gap is the blank carousel thumbnail.

This probe changes only the front-end preview side. It keeps the working v0.9
livery/paint installation active and repoints AJ's driver-select container to a
stock, game-authored 10-entry template. AJ's original driver image, 3D number,
and primary paint preview are transplanted into native indexed slots. Brad's
Indianapolis preview slot is copied and renamed from PAINTSCHEME_25580 to the
new livery UID PAINTSCHEME_25582.

No resource is appended inside the ARCC container. Its count, layout, chunk
boundaries, and total size remain exactly stock. The rebuilt container is
appended to ARCHIVE1.AR and only AJ's cdfiles1.dat row is repointed.

Expected result
---------------
AJ's carousel shows both:
  * PAINTSCHEME_25364 — AJ's original primary thumbnail
  * PAINTSCHEME_25582 — Brad's Indianapolis thumbnail for the new extra scheme

Prerequisite
------------
The working v0.9 extra-scheme patch must remain applied. This script validates
that LIVERIE_c UID 25582 / ScriptName 15_47_AJ_EXTRA_SLOT_TEST belongs to AJ
before it writes anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "0.10"
TEMPLATE_CONTAINER = "2DRIVERSELECTTD_1336.ARC"
DEST_CONTAINER = "2DRIVERSELECTTD_1326.ARC"
DONOR_PREVIEW = "PAINTSCHEME_25580"
REQUIRED_EXTRA = "PAINTSCHEME_25582"

# Existing native slots in the Brad/Joey template that will be repurposed.
# All old/new names are intentionally equal-length, preserving name references.
TRANSPLANTS = [
    ("DRIVERPAINT_1117_25041", "DRIVERPAINT_1083_25041"),
    ("DRIVER_1117_3DNUM_25041", "DRIVER_1083_3DNUM_25041"),
    ("PAINTSCHEME_25358", "PAINTSCHEME_25364"),
]

MANIFEST_NAME = "true_extra_scheme_preview_v0_10_manifest.json"
ANALYSIS_NAME = "true_extra_scheme_preview_v0_10_analysis.json"
CDF_BACKUP_SUFFIX = ".true_extra_scheme_preview_v0_10.bak"
ALIGNMENT = 16


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_range(path: Path, off: int, size: int) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        f.seek(off)
        left = size
        while left:
            b = f.read(min(left, 1024 * 1024))
            if not b:
                raise ValueError("short archive read")
            h.update(b)
            left -= len(b)
    return h.hexdigest()


def detect_game(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for cfg in [
        app_dir() / "config.json",
        Path(os.environ.get("LOCALAPPDATA", "")) / "NASCAR15ModdingApp" / "config.json",
    ]:
        try:
            if cfg.exists():
                p = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("game")
                if p:
                    candidates.append(Path(p))
        except Exception:
            pass
    candidates += [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\NASCAR 15"),
        Path(r"D:\SteamLibrary\steamapps\common\NASCAR 15"),
        Path(r"E:\SteamLibrary\steamapps\common\NASCAR 15"),
    ]
    seen: set[str] = set()
    for p in candidates:
        k = str(p).casefold()
        if not k or k in seen:
            continue
        seen.add(k)
        if (p / "data" / "ARCHIVE1.AR").exists():
            return p
    raise FileNotFoundError("NASCAR 15 was not found. Pass --game with the game folder.")


def game_running() -> bool:
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq NASCAR15.exe"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "nascar15.exe" in (r.stdout or "").casefold()
    except Exception:
        return False


def validate_working_v09(game: Path) -> dict[str, Any]:
    """Prove the working UID 25582 livery exists before touching previews."""
    try:
        import nascar15_inrange_true_extra_scheme_probe_v0_4_base as base
    except Exception as exc:
        raise RuntimeError(
            "The included database mapper helpers could not be loaded. "
            "Keep all files from this ZIP together."
        ) from exc

    ctx = base.load_context(str(game))
    rec = base.find_record(ctx, "LIVERIE_c", 25582)
    summary = base.livery_summary(ctx, rec)
    script_name = summary["fields"].get("ScriptName")
    if script_name != "15_47_AJ_EXTRA_SLOT_TEST":
        raise ValueError(
            "Livery UID 25582 exists, but its ScriptName is not the working v0.9 test identity."
        )
    if summary.get("driver_uid") != 1083:
        raise ValueError("Livery UID 25582 is not assigned to AJ Allmendinger (driver UID 1083).")

    cdf2 = game / "data" / "cdfiles2.dat"
    raw = cdf2.read_bytes()
    for name in (
        b"LIVERY_15_47_AJ_EXTRA_SLOT_TEST.ARC",
        b"HDLIVERY_15_47_AJ_EXTRA_SLOT_TEST.ARC",
    ):
        if name not in raw:
            raise ValueError(
                f"The v0.9 paint entry {name.decode('ascii')} is missing from cdfiles2.dat."
            )
    return summary


@dataclass
class CdfRow:
    name: str
    offset: int
    size: int
    record_pos: int
    size_pos: int
    offset_pos: int
    layout: str


def parse_cdf_rows(path: Path) -> tuple[bytearray, list[CdfRow]]:
    raw = bytearray(path.read_bytes())
    if len(raw) < 48 or struct.unpack_from("<I", raw, 0)[0] != 0x436C6966:
        raise ValueError(f"{path.name} is not a valid filC index")
    hdr = struct.unpack_from("<12I", raw, 0)
    count, string_size = hdr[8], hdr[10]
    base = len(raw) - string_size

    def name_at(off: int) -> str:
        if off >= string_size:
            return ""
        p = base + off
        e = raw.find(b"\0", p)
        if e < p:
            return ""
        return raw[p:e].decode("ascii", "replace")

    choices = []
    for start, layout, ni, si, oi in ((0x40, "A", 1, 2, 5), (0x50, "B", 3, 4, 7)):
        rows: list[CdfRow] = []
        valid = 0
        pos = start
        for _ in range(count):
            if pos + 32 > base:
                break
            fields = struct.unpack_from("<8I", raw, pos)
            name = name_at(fields[ni])
            if name and all(32 <= ord(c) < 127 for c in name):
                valid += 1
            rows.append(
                CdfRow(
                    name,
                    int(fields[oi]),
                    int(fields[si]),
                    pos,
                    pos + si * 4,
                    pos + oi * 4,
                    layout,
                )
            )
            pos += 32
        choices.append((valid, rows))
    valid, rows = max(choices, key=lambda x: x[0])
    rows = [r for r in rows if r.name]
    if valid < max(1, int(count * 0.8)):
        raise ValueError("could not parse cdfiles1.dat record layout")
    return raw, rows


def find_row(rows: list[CdfRow], name: str) -> CdfRow:
    hits = [r for r in rows if r.name.casefold() == name.casefold()]
    if len(hits) != 1:
        raise ValueError(f"Expected one {name}; found {len(hits)}")
    return hits[0]


def read_entry(archive: Path, row: CdfRow) -> bytes:
    with archive.open("rb") as f:
        f.seek(row.offset)
        data = f.read(row.size)
    if len(data) != row.size:
        raise ValueError(f"short read for {row.name}")
    return data


@dataclass
class MultiEntry:
    index: int
    name: str
    data_off: int
    name_ref: int
    table_start: int
    table_record: bytes
    chunk_start: int
    chunk_end: int
    chunk: bytes
    payload_abs: int
    width: int
    height: int
    fmt: str


@dataclass
class MultiArc:
    raw: bytes
    count: int
    base: int
    name_blob: int
    entries: list[MultiEntry]


def locate_name_blob(arc: bytes, recs: list[tuple[int, int]]) -> int:
    refs = [r for _, r in recs]
    ok_re = re.compile(rb"^[A-Za-z0-9_\-. ]{2,64}$")
    tail_start = max(0, len(arc) - max(refs, default=0) - 8192)
    candidates: list[int] = []
    for pat in (rb"[A-Z0-9][A-Za-z0-9_]{3,}", rb"[A-Za-z0-9][A-Za-z0-9_\-. ]{3,}"):
        candidates += [tail_start + m.start() for m in re.finditer(pat, arc[tail_start:])]
    for cstart in candidates:
        cand = cstart - min(refs, default=0)
        if cand < 0:
            continue
        good = 0
        for ref in refs:
            p = cand + ref
            if p >= len(arc):
                break
            e = arc.find(b"\0", p)
            if e > p and ok_re.match(arc[p:e]):
                good += 1
            else:
                break
        if good == len(refs):
            return cand
    raise ValueError("could not locate multi-ARC name blob")


def parse_multi_arc(arc: bytes) -> MultiArc:
    if len(arc) < 0xA0 or arc[:4] != b"ARCC":
        raise ValueError("container is not an ARCC multi-texture file")
    count = struct.unpack_from("<I", arc, 8)[0]
    if count <= 0 or count > 10000:
        raise ValueError(f"invalid entry count {count}")
    recs: list[tuple[int, int]] = []
    records: list[bytes] = []
    pos = 0x80
    for _ in range(count):
        if pos + 32 > len(arc):
            raise ValueError("entry table exceeds container")
        tr = bytes(arc[pos : pos + 32])
        r2 = struct.unpack_from("<4I", tr, 16)
        recs.append((int(r2[1]), int(r2[2])))
        records.append(tr)
        pos += 32
    base = pos
    blob = locate_name_blob(arc, recs)
    if blob <= base:
        raise ValueError("name blob overlaps data table")

    def name_at(ref: int) -> str:
        p = blob + ref
        e = arc.find(b"\0", p)
        if p < blob or e <= p:
            raise ValueError(f"bad name reference {ref}")
        return arc[p:e].decode("latin1")

    offsets = sorted(o for o, _ in recs)
    entries: list[MultiEntry] = []
    for i, ((off, ref), tr) in enumerate(zip(recs, records)):
        chunk_start = base + off
        next_off = min((o for o in offsets if o > off), default=blob - base)
        chunk_end = base + next_off
        if chunk_start < base or chunk_end > blob or chunk_end <= chunk_start:
            raise ValueError(f"invalid chunk range for entry {i}")
        if chunk_start + 96 > chunk_end:
            raise ValueError(f"entry {i} chunk is too short")
        width, height = struct.unpack_from("<2H", arc, chunk_start + 32)
        fmt_b = arc[chunk_start + 44 : chunk_start + 48]
        fmt = fmt_b.decode("ascii", "replace") if fmt_b in (b"DXT1", b"DXT5") else "UNKNOWN"
        entries.append(
            MultiEntry(
                i,
                name_at(ref),
                off,
                ref,
                0x80 + i * 32,
                tr,
                chunk_start,
                chunk_end,
                bytes(arc[chunk_start:chunk_end]),
                chunk_start + 96,
                int(width),
                int(height),
                fmt,
            )
        )
    return MultiArc(arc, count, base, blob, entries)


def entry_by_name(m: MultiArc, name: str) -> MultiEntry:
    hits = [e for e in m.entries if e.name == name]
    if len(hits) != 1:
        raise ValueError(f"Expected one {name}; found {len(hits)}")
    return hits[0]


def expected_texture_bytes(entry: MultiEntry) -> int:
    if entry.width <= 0 or entry.height <= 0:
        raise ValueError(f"invalid dimensions for {entry.name}: {entry.width}x{entry.height}")
    blocks = max(1, (entry.width + 3) // 4) * max(1, (entry.height + 3) // 4)
    if entry.fmt == "DXT1":
        return blocks * 8
    if entry.fmt == "DXT5":
        return blocks * 16
    raise ValueError(f"unsupported texture format for {entry.name}: {entry.fmt}")


def identity_record(source_record: bytes, source: MultiEntry, target: MultiEntry) -> bytes:
    """Use source name/hash identity but retain target data/name locations."""
    out = bytearray(source_record)
    if len(out) != 32:
        raise ValueError("resource record is not 32 bytes")
    if struct.unpack_from("<I", out, 20)[0] != source.data_off:
        raise ValueError(f"source table data offset mismatch for {source.name}")
    if struct.unpack_from("<I", out, 24)[0] != source.name_ref:
        raise ValueError(f"source table name reference mismatch for {source.name}")
    struct.pack_into("<I", out, 20, target.data_off)
    struct.pack_into("<I", out, 24, target.name_ref)
    return bytes(out)


def transplant_entry(
    out: bytearray,
    template: MultiArc,
    recipient: MultiArc,
    template_name: str,
    recipient_name: str,
) -> dict[str, Any]:
    target = entry_by_name(template, template_name)
    source = entry_by_name(recipient, recipient_name)
    if len(template_name.encode("latin1")) != len(recipient_name.encode("latin1")):
        raise ValueError(f"name lengths differ: {template_name} vs {recipient_name}")
    if (source.width, source.height, source.fmt) != (target.width, target.height, target.fmt):
        raise ValueError(
            f"texture profiles differ for {template_name} <- {recipient_name}: "
            f"{target.width}x{target.height} {target.fmt} vs "
            f"{source.width}x{source.height} {source.fmt}"
        )

    expected = expected_texture_bytes(source)
    source_room = source.chunk_end - source.payload_abs
    target_room = target.chunk_end - target.payload_abs
    block = 16 if source.fmt == "DXT5" else 8
    if source_room <= 0:
        raise ValueError(f"source {recipient_name} has no texture storage")
    if target_room <= 0:
        raise ValueError(f"template {template_name} has no texture storage")

    # NASCAR 15 preview resources do not all store the same amount of the final
    # DXT image. Measured stock examples are 0, 16, or 64 bytes short, depending
    # on the wrapper/slot. Treat both resources as the same logical texture:
    # pad the source to the full logical size, then write only the number of
    # bytes the native template slot actually owns. This safely handles both:
    #   * a shorter source copied into a larger template slot, and
    #   * a longer source copied into a more-truncated template slot.
    source_image_bytes = min(source_room, expected)
    target_image_bytes = min(target_room, expected)
    if source_image_bytes % block:
        raise ValueError(
            f"source {recipient_name} ends mid-{source.fmt} block "
            f"({source_image_bytes} bytes)"
        )
    if target_image_bytes % block:
        raise ValueError(
            f"template {template_name} ends mid-{target.fmt} block "
            f"({target_image_bytes} bytes)"
        )

    source_native_short = expected - source_image_bytes
    target_native_short = expected - target_image_bytes
    # Keep the probe bounded to the stock layouts measured so far. Anything
    # more than 64 bytes short may indicate a different resource type/layout.
    if source_native_short < 0 or source_native_short > 64 or source_native_short % block:
        raise ValueError(
            f"source {recipient_name} is {source_native_short} bytes short of its "
            f"{expected}-byte {source.fmt} image; unsupported layout"
        )
    if target_native_short < 0 or target_native_short > 64 or target_native_short % block:
        raise ValueError(
            f"template {template_name} is {target_native_short} bytes short of its "
            f"{expected}-byte {target.fmt} image; unsupported layout"
        )

    source_pixels = recipient.raw[
        source.payload_abs : source.payload_abs + source_image_bytes
    ]
    logical_pixels = source_pixels + b"\0" * source_native_short
    target_pixels = logical_pixels[:target_image_bytes]
    copied_from_source = min(source_image_bytes, target_image_bytes)
    source_bytes_dropped = max(0, source_image_bytes - target_image_bytes)
    zero_filled_bytes = max(0, target_image_bytes - source_image_bytes)

    target_tail_before = bytes(
        out[target.payload_abs + target_image_bytes : target.chunk_end]
    )
    wrapper_before = bytes(out[target.chunk_start : target.payload_abs])

    # Use the recipient's proven outer identity record, relocated to the native
    # template slot. Preserve the template's complete chunk wrapper/header.
    new_record = identity_record(source.table_record, source, target)
    out[target.table_start : target.table_start + 32] = new_record
    out[target.payload_abs : target.payload_abs + target_image_bytes] = target_pixels

    # Equal-length name replacement leaves the native name table layout intact.
    name_abs = template.name_blob + target.name_ref
    old_name = bytes(out[name_abs : name_abs + len(template_name)]).decode("latin1")
    if old_name != template_name:
        raise ValueError(f"template name mismatch at {name_abs:#x}: {old_name!r}")
    out[name_abs : name_abs + len(template_name)] = recipient_name.encode("latin1")

    return {
        "template_entry": template_name,
        "recipient_entry": recipient_name,
        "template_index": target.index,
        "texture_profile": f"{target.width}x{target.height} {target.fmt}",
        "logical_texture_bytes": expected,
        "texture_bytes_copied_from_source": copied_from_source,
        "source_native_short_bytes": source_native_short,
        "template_native_short_bytes": target_native_short,
        "source_bytes_dropped_for_template_layout": source_bytes_dropped,
        "zero_filled_bytes": zero_filled_bytes,
        "template_image_capacity": target_image_bytes,
        "preserved_template_native_tail_bytes": len(target_tail_before),
        "template_chunk_size": len(target.chunk),
        "recipient_chunk_size": len(source.chunk),
        "template_wrapper_sha256": sha256_bytes(wrapper_before),
        "preserved_template_tail_sha256": sha256_bytes(target_tail_before),
        "recipient_pixels_sha256": sha256_bytes(source_pixels),
        "target_pixels_sha256": sha256_bytes(target_pixels),
        "replacement_record_words": list(struct.unpack("<8I", new_record)),
    }


def rename_equal_length_entry(
    out: bytearray,
    template: MultiArc,
    old_name: str,
    new_name: str,
) -> dict[str, Any]:
    """Rename a native indexed resource without changing its record or layout."""
    if len(old_name.encode("latin1")) != len(new_name.encode("latin1")):
        raise ValueError(f"Preview names are not equal length: {old_name} vs {new_name}")
    entry = entry_by_name(template, old_name)
    name_abs = template.name_blob + entry.name_ref
    current = bytes(out[name_abs : name_abs + len(old_name)]).decode("latin1")
    if current != old_name:
        raise ValueError(f"Preview name mismatch at {name_abs:#x}: {current!r}")
    pixels = bytes(out[entry.payload_abs : entry.chunk_end])
    out[name_abs : name_abs + len(old_name)] = new_name.encode("latin1")
    return {
        "old_name": old_name,
        "new_name": new_name,
        "index": entry.index,
        "name_ref": entry.name_ref,
        "data_offset": entry.data_off,
        "texture_profile": f"{entry.width}x{entry.height} {entry.fmt}",
        "chunk_size": len(entry.chunk),
        "payload_sha256": sha256_bytes(pixels),
        "table_record_words": list(struct.unpack("<8I", entry.table_record)),
    }


def build_native_template(template_raw: bytes, recipient_raw: bytes) -> tuple[bytes, dict[str, Any]]:
    template = parse_multi_arc(template_raw)
    recipient = parse_multi_arc(recipient_raw)

    if any(e.name == REQUIRED_EXTRA for e in recipient.entries):
        raise ValueError(
            f"AJ's container already contains {REQUIRED_EXTRA}. Restore this preview probe first."
        )

    # Enforce a clean recipient source and a native donor preview slot.
    entry_by_name(recipient, "PAINTSCHEME_25364")
    donor_extra = entry_by_name(template, DONOR_PREVIEW)
    if any(e.name == REQUIRED_EXTRA for e in template.entries):
        raise ValueError(f"Template unexpectedly already contains {REQUIRED_EXTRA}")

    out = bytearray(template_raw)
    changes = []
    for old_name, new_name in TRANSPLANTS:
        changes.append(transplant_entry(out, template, recipient, old_name, new_name))

    # The stock Penske template already owns a valid native preview slot for
    # Brad's Indianapolis image. Only its equal-length lookup name changes so
    # AJ's new livery UID 25582 resolves the same proven image and wrapper.
    rename = rename_equal_length_entry(out, template, DONOR_PREVIEW, REQUIRED_EXTRA)

    rebuilt = bytes(out)
    if len(rebuilt) != len(template_raw):
        raise ValueError("native-template rebuild changed container size")
    parsed = parse_multi_arc(rebuilt)
    if parsed.count != template.count:
        raise ValueError("native-template entry count changed")

    for old_name, new_name in TRANSPLANTS:
        if any(e.name == old_name for e in parsed.entries):
            raise ValueError(f"old template name still exists: {old_name}")
        entry_by_name(parsed, new_name)
    if any(e.name == DONOR_PREVIEW for e in parsed.entries):
        raise ValueError(f"AJ rebuilt container still exposes donor lookup {DONOR_PREVIEW}")
    extra = entry_by_name(parsed, REQUIRED_EXTRA)
    primary = entry_by_name(parsed, "PAINTSCHEME_25364")

    # Verify transplanted image bytes and preserved wrappers/tails.
    verification = []
    for old_name, new_name in TRANSPLANTS:
        src = entry_by_name(recipient, new_name)
        dst = entry_by_name(parsed, new_name)
        expected = expected_texture_bytes(src)
        src_room = src.chunk_end - src.payload_abs
        dst_room = dst.chunk_end - dst.payload_abs
        src_image_bytes = min(src_room, expected)
        dst_image_bytes = min(dst_room, expected)
        src_pixels = recipient_raw[src.payload_abs : src.payload_abs + src_image_bytes]
        normalized = (src_pixels + b"\0" * (expected - src_image_bytes))[:dst_image_bytes]
        dst_pixels = rebuilt[dst.payload_abs : dst.payload_abs + dst_image_bytes]
        if dst_pixels != normalized:
            raise ValueError(f"normalized texture verification failed: {new_name}")
        verification.append({
            "entry": new_name,
            "source_image_bytes": src_image_bytes,
            "destination_image_bytes": dst_image_bytes,
            "source_bytes_dropped": max(0, src_image_bytes - dst_image_bytes),
            "zero_filled_bytes": max(0, dst_image_bytes - src_image_bytes),
            "pixels_sha256": sha256_bytes(dst_pixels),
        })

    # Renaming must not alter the donor preview's table record, wrapper, or pixels.
    if extra.table_record != donor_extra.table_record:
        raise ValueError("renamed extra preview table record changed")
    donor_chunk = template_raw[donor_extra.chunk_start : donor_extra.chunk_end]
    extra_chunk = rebuilt[extra.chunk_start : extra.chunk_end]
    if extra_chunk != donor_chunk:
        raise ValueError("renamed extra preview chunk/pixels changed")

    report = {
        "template_count": template.count,
        "recipient_original_count": recipient.count,
        "result_count": parsed.count,
        "template_size": len(template_raw),
        "recipient_original_size": len(recipient_raw),
        "result_size": len(rebuilt),
        "original_primary_entry": primary.name,
        "required_extra_entry": REQUIRED_EXTRA,
        "required_extra_index": extra.index,
        "extra_preview_source": DONOR_PREVIEW,
        "extra_preview_payload_sha256": sha256_bytes(extra_chunk),
        "result_names": [e.name for e in parsed.entries],
        "transplants": changes,
        "preview_rename": rename,
        "verification": verification,
        "rebuilt_sha256": sha256_bytes(rebuilt),
    }
    return rebuilt, report


def paths_for_game(game: Path) -> tuple[Path, Path]:
    data = game / "data"
    archive = data / "ARCHIVE1.AR"
    cdf = data / "cdfiles1.dat"
    if not archive.exists() or not cdf.exists():
        raise FileNotFoundError("ARCHIVE1.AR/cdfiles1.dat was not found")
    return archive, cdf


def build_analysis(game_arg: str | None):
    game = detect_game(game_arg)
    v09_summary = validate_working_v09(game)
    archive, cdf = paths_for_game(game)
    _raw, rows = parse_cdf_rows(cdf)
    template_row = find_row(rows, TEMPLATE_CONTAINER)
    dest_row = find_row(rows, DEST_CONTAINER)
    template_raw = read_entry(archive, template_row)
    recipient_raw = read_entry(archive, dest_row)
    rebuilt, report = build_native_template(template_raw, recipient_raw)
    analysis = {
        "version": VERSION,
        "game": str(game),
        "archive": str(archive),
        "cdfiles": str(cdf),
        "template_container": TEMPLATE_CONTAINER,
        "template_offset": template_row.offset,
        "template_size": template_row.size,
        "template_sha256": sha256_bytes(template_raw),
        "destination_container": DEST_CONTAINER,
        "destination_offset_before": dest_row.offset,
        "destination_size_before": dest_row.size,
        "destination_sha256_before": sha256_bytes(recipient_raw),
        "working_v09_livery": v09_summary,
        **report,
    }
    return analysis, rebuilt, template_row, dest_row, archive, cdf


def cmd_analyze(args: argparse.Namespace) -> int:
    analysis, rebuilt, *_ = build_analysis(args.game)
    (app_dir() / ANALYSIS_NAME).write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    (app_dir() / f"native_template_{DEST_CONTAINER}").write_bytes(rebuilt)
    print(json.dumps(analysis, indent=2))
    print("\n[+] Native-template analysis and rebuilt test container written beside this script.")
    print("[i] Nothing was written to the game.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if game_running():
        raise RuntimeError("NASCAR15.exe is running; close the game first")
    manifest_path = app_dir() / MANIFEST_NAME
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("applied") and not old.get("restored"):
            raise RuntimeError("The v0.10 preview probe is already active. Restore it first.")
    analysis, rebuilt, _template_row, dest_row, archive, cdf = build_analysis(args.game)
    cdf_backup = Path(str(cdf) + CDF_BACKUP_SUFFIX)
    if not cdf_backup.exists():
        shutil.copy2(cdf, cdf_backup)
    old_archive_size = archive.stat().st_size
    old_cdf = cdf.read_bytes()
    new_offset = (old_archive_size + ALIGNMENT - 1) & ~(ALIGNMENT - 1)
    try:
        with archive.open("ab") as f:
            if new_offset > old_archive_size:
                f.write(b"\0" * (new_offset - old_archive_size))
            f.write(rebuilt)
            f.flush()
            os.fsync(f.fileno())
        if sha256_range(archive, new_offset, len(rebuilt)) != sha256_bytes(rebuilt):
            raise ValueError("appended native-template container readback mismatch")

        cdf_raw, rows = parse_cdf_rows(cdf)
        live = find_row(rows, DEST_CONTAINER)
        if live.offset != dest_row.offset or live.size != dest_row.size:
            raise ValueError("AJ container changed after analysis; rerun the probe")
        struct.pack_into("<I", cdf_raw, live.offset_pos, new_offset)
        struct.pack_into("<I", cdf_raw, live.size_pos, len(rebuilt))
        tmp = Path(str(cdf) + ".true_extra_preview.tmp")
        tmp.write_bytes(cdf_raw)
        os.replace(tmp, cdf)

        _, check_rows = parse_cdf_rows(cdf)
        installed_row = find_row(check_rows, DEST_CONTAINER)
        installed = read_entry(archive, installed_row)
        parsed = parse_multi_arc(installed)
        entry_by_name(parsed, "PAINTSCHEME_25364")
        entry_by_name(parsed, REQUIRED_EXTRA)
        entry_by_name(parsed, "DRIVERPAINT_1083_25041")
        entry_by_name(parsed, "DRIVER_1083_3DNUM_25041")
    except Exception:
        try:
            with archive.open("r+b") as f:
                f.truncate(old_archive_size)
            tmp = Path(str(cdf) + ".true_extra_preview.rollback.tmp")
            tmp.write_bytes(old_cdf)
            os.replace(tmp, cdf)
        except Exception:
            pass
        raise

    manifest = {
        **analysis,
        "archive_size_before": old_archive_size,
        "destination_offset_after": new_offset,
        "destination_size_after": len(rebuilt),
        "archive_size_after": new_offset + len(rebuilt),
        "cdf_backup": str(cdf_backup),
        "applied": True,
        "restored": False,
    }
    (app_dir() / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("[+] AJ now uses a stock game-authored multi-preview container template.")
    print("[+] PAINTSCHEME_25364 and PAINTSCHEME_25582 are both in native indexed slots.")
    print("[+] The 25582 slot keeps Brad's proven Indianapolis preview pixels and wrapper.")
    print("[i] Test both thumbnails, a race, a full game restart, and menu/save persistence.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    if game_running():
        raise RuntimeError("NASCAR15.exe is running; close the game first")
    mp = app_dir() / MANIFEST_NAME
    if not mp.exists():
        raise FileNotFoundError(f"{MANIFEST_NAME} was not found")
    m = json.loads(mp.read_text(encoding="utf-8"))
    archive = Path(m["archive"])
    cdf = Path(m["cdfiles"])
    cdf_raw, rows = parse_cdf_rows(cdf)
    row = find_row(rows, DEST_CONTAINER)
    old_off = int(m["destination_offset_before"])
    old_size = int(m["destination_size_before"])
    new_off = int(m["destination_offset_after"])
    new_size = int(m["destination_size_after"])
    if row.offset == old_off and row.size == old_size:
        print("[i] Native-template preview probe is already restored.")
        return 0
    if row.offset != new_off or row.size != new_size:
        raise RuntimeError("AJ container changed after this probe; restore refused.")
    struct.pack_into("<I", cdf_raw, row.offset_pos, old_off)
    struct.pack_into("<I", cdf_raw, row.size_pos, old_size)
    tmp = Path(str(cdf) + ".true_extra_preview.restore.tmp")
    tmp.write_bytes(cdf_raw)
    os.replace(tmp, cdf)

    _, rows2 = parse_cdf_rows(cdf)
    restored = read_entry(archive, find_row(rows2, DEST_CONTAINER))
    parsed = parse_multi_arc(restored)
    entry_by_name(parsed, "PAINTSCHEME_25364")
    if any(e.name == REQUIRED_EXTRA for e in parsed.entries):
        raise ValueError("restored AJ container unexpectedly still contains the extra preview")
    original_archive_size = int(m.get("archive_size_before", archive.stat().st_size))
    expected_after = int(m.get("archive_size_after", archive.stat().st_size))
    truncated = False
    if archive.stat().st_size == expected_after:
        with archive.open("r+b") as f:
            f.truncate(original_archive_size)
        truncated = True

    m["restored"] = True
    m["restored_archive_truncated"] = truncated
    mp.write_text(json.dumps(m, indent=2), encoding="utf-8")
    print("[+] AJ's original preview container restored.")
    if truncated:
        print("[+] ARCHIVE1.AR returned to its exact pre-probe size.")
    else:
        print("[i] Preview bytes remain orphaned at the archive end because ARCHIVE1 changed after Apply.")
    print("[i] The working v0.9 livery and SD/HD paint installation remain active.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="NASCAR 15 true extra-scheme preview and persistence probe")
    p.add_argument("command", choices=["analyze", "apply", "restore"])
    p.add_argument("--game")
    args = p.parse_args()
    try:
        return {"analyze": cmd_analyze, "apply": cmd_apply, "restore": cmd_restore}[args.command](args)
    except Exception as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
