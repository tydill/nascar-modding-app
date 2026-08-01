#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import struct
import zlib
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
import containers as C
from typing import Iterable

APP_NAME = "NASCAR 15 Texture Discovery"
APP_VERSION = "0.2-rc11"

PRINTABLE_EXT = re.compile(rb"[A-Za-z0-9_./\\\- ]{2,120}\.(?:dds|png|tga|bmp|jpg|jpeg)\x00", re.I)
PLAIN_NAME = re.compile(rb"(?:IMG_[A-Za-z0-9_\-]{2,80}|[A-Za-z][A-Za-z0-9_\-]{3,80})\x00")

@dataclass
class TextureRow:
    archive: str
    container: str
    entry: str
    family: str
    w: int
    h: int
    fmt: str
    payload_size: int
    payload_abs: int
    archive_entry_offset_hex: str
    archive_entry_size: int
    decoded: int
    png_path: str = ""
    image_sha1: str = ""
    decode_error: str = ""
    record_index: int | None = None
    record_type: str = ""
    mip_layout: str = "unknown"
    mip_count: int = 0
    overlap_previous: int = 0
    overlap_next: int = 0
    write_blocked: int = 0
    source: str = "live_discovery"


def parse_cdfiles(path: str | os.PathLike) -> list[tuple[int, int, str]]:
    data = Path(path).read_bytes()
    if len(data) < 0x40 or struct.unpack_from("<I", data, 0)[0] != 0x436C6966:
        raise ValueError(f"{path}: not a filC index")
    hdr = struct.unpack_from("<12I", data, 0)
    count, string_size = hdr[8], hdr[10]
    string_base = len(data) - string_size
    if count <= 0 or string_size <= 0 or string_base < 0:
        raise ValueError(f"{path}: invalid cdfiles header")

    def name_at(off: int) -> str:
        p = string_base + off
        if p < string_base or p >= len(data):
            return ""
        e = data.find(b"\0", p)
        if e < p:
            return ""
        return data[p:e].decode("ascii", "replace")

    best: list[tuple[int, int, str]] = []
    for start, layout in ((0x40, "A"), (0x50, "B")):
        rows: list[tuple[int, int, str]] = []
        good = 0
        pos = start
        for _ in range(count):
            if pos + 32 > string_base:
                break
            fields = struct.unpack_from("<8I", data, pos)
            if layout == "A":
                name_off, size, arc_off = fields[1], fields[2], fields[5]
            else:
                name_off, size, arc_off = fields[3], fields[4], fields[7]
            name = name_at(name_off) if name_off < string_size else ""
            if name and all(32 <= ord(ch) < 127 for ch in name):
                good += 1
            rows.append((arc_off, size, name))
            pos += 32
        if rows and good >= max(1, int(count * 0.8)):
            best = [r for r in rows if r[2]]
            break
    if not best:
        raise ValueError(f"{path}: unsupported cdfiles layout")
    return best


def _record_type_size(packed: int) -> tuple[int, int]:
    raw = packed.to_bytes(4, "little")
    return raw[0], int.from_bytes(raw[1:4], "big")


def _likely_family(container: str, entry: str) -> str:
    c = container.upper()
    e = entry.upper()
    if c == "NASCAR6_TEXTURES_X.ARC":
        if e.startswith("TYRE") or "WHEELBLUR" in e:
            return "tire_wheel_textures"
        return "shared_vehicle_textures"
    if c.startswith("DF_"):
        return "driver_face_textures"
    if c.startswith("DTS_"):
        return "driver_suit_glove_textures"
    if c.startswith("GTS_"):
        return "garage_character_textures"
    if c.startswith("ITS_"):
        return "infield_character_textures"
    if c.startswith("PMH_"):
        return "driver_head_textures"
    if c.startswith("CHAMP_"):
        return "champion_character_textures"
    if c.startswith("LIVERY_") or e == "IMG_LIV":
        return "vehicle_livery_textures"
    if any(k in c for k in ("TRACK", "CIRCUIT", "SPEEDWAY")):
        return "track_environment_textures"
    if any(k in e for k in ("TIRE", "TYRE", "WHEEL")):
        return "tire_wheel_textures"
    return "discovered_texture"


def _infer_standard_mips(w: int, h: int, fmt: str, payload_size: int) -> int | None:
    bpb = 8 if fmt == "DXT1" else 16 if fmt == "DXT5" else 0
    if not bpb or w <= 0 or h <= 0:
        return None
    total = 0
    cw, ch = w, h
    for level in range(1, 21):
        total += max(1, (cw + 3) // 4) * max(1, (ch + 3) // 4) * bpb
        if total == payload_size:
            return level
        if total > payload_size or (cw == 1 and ch == 1):
            break
        cw, ch = max(1, cw // 2), max(1, ch // 2)
    return None



def _resolve_multi_dims(w1: int, h1: int, data_size: int, fmt: str) -> tuple[int, int]:
    bpb = 8 if fmt == "DXT1" else 16
    candidates = {(max(1, w1), max(1, h1))}
    for sw in (1, 2, 4):
        for sh in (1, 2, 4):
            candidates.add((max(1, w1) * sw, max(1, h1) * sh))
    exact = []
    meta_aspect = (w1 / h1) if h1 else 1.0
    for w, h in candidates:
        needed = max(1, w // 4) * max(1, h // 4) * bpb
        if needed == data_size:
            exact.append((abs((w / h) - meta_aspect), 0 if w >= h else 1, w, h))
    if exact:
        exact.sort()
        return exact[0][2], exact[0][3]
    return max(1, w1), max(1, h1)


def _scan_multi_texture_arc(blob: bytes, archive: str, container: str,
                            archive_entry_offset: int = 0) -> list[TextureRow]:
    """Inventory a native ARCC bank through the canonical physical table."""
    try:
        entries, _ = C.parse_multi_arc(blob)
    except Exception:
        return []
    rows: list[TextureRow] = []
    for e in entries:
        fmt = str(e.get("fmt") or "")
        decoded = int(fmt in ("DXT1", "DXT5", "A8R8G8B8"))
        rows.append(TextureRow(
            archive=str(archive), container=container, entry=str(e["name"]),
            family=_likely_family(container, str(e["name"])),
            w=int(e["w"]), h=int(e["h"]), fmt=fmt,
            payload_size=int(e["payload_size"]), payload_abs=int(e["payload_abs"]),
            archive_entry_offset_hex=f"0x{archive_entry_offset:X}" if archive_entry_offset else "",
            archive_entry_size=len(blob), decoded=decoded,
            decode_error=("" if decoded else f"Unsupported resource format {fmt}; raw export only"),
            record_index=int(e.get("physical_record_index", e.get("index", 0))),
            record_type=str(e.get("layout", "primary16")),
            mip_layout=(f"native:{int(e.get('mip_count') or 0)}" if int(e.get('mip_count') or 0) else "padded_or_custom"),
            mip_count=int(e.get("mip_count") or 0), source="live_discovery_native16",
        ))
    return rows


def _collect_tail_strings(blob: bytes, tail_start: int) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    seen = set()
    tail = blob[tail_start:]
    for rx in (PRINTABLE_EXT, PLAIN_NAME):
        for m in rx.finditer(tail):
            raw = m.group()[:-1]
            try:
                text = raw.decode("latin1")
            except Exception:
                continue
            key = (tail_start + m.start(), text)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return sorted(out)


def _derive_name_base(records: list[dict], strings: list[tuple[int, str]], blob_len: int) -> int | None:
    # First use CRC32 matches. Resource records typically use CRC32(lowercase filename).
    by_crc: dict[int, list[tuple[int, str]]] = {}
    for pos, text in strings:
        by_crc.setdefault(zlib.crc32(text.lower().encode("latin1")) & 0xFFFFFFFF, []).append((pos, text))
    candidates: dict[int, int] = {}
    for rec in records:
        nr = rec["name_ref"]
        if nr == 0xFFFFFFFF:
            continue
        for pos, _ in by_crc.get(rec["key"], []):
            base = pos - nr
            if 0 <= base < blob_len:
                candidates[base] = candidates.get(base, 0) + 10

    # Also consider position - ref combinations, then score how many refs resolve to known strings.
    sample_refs = [r["name_ref"] for r in records if r["name_ref"] not in (0xFFFFFFFF, 0)]
    for nr in sample_refs[:80]:
        for pos, _ in strings[:300]:
            base = pos - nr
            if 0 <= base < blob_len:
                candidates.setdefault(base, 0)

    string_by_pos = {p: s for p, s in strings}
    best_base = None
    best_score = -1
    for base, bonus in candidates.items():
        score = bonus
        for rec in records:
            nr = rec["name_ref"]
            if nr == 0xFFFFFFFF:
                continue
            if base + nr in string_by_pos:
                score += 1
        if score > best_score:
            best_score = score
            best_base = base
    return best_base if best_score >= 2 else None


def _mark_payload_overlaps(rows: list[TextureRow]) -> list[TextureRow]:
    ordered=sorted(rows,key=lambda r:(int(r.payload_abs),int(r.payload_size),r.entry))
    for i,row in enumerate(ordered):
        start=int(row.payload_abs);end=start+int(row.payload_size)
        if i>0:
            prev=ordered[i-1];prev_end=int(prev.payload_abs)+int(prev.payload_size)
            if start<prev_end:
                row.overlap_previous=1;prev.overlap_next=1;row.write_blocked=1;prev.write_blocked=1
        if i+1<len(ordered):
            nxt=ordered[i+1]
            if end>int(nxt.payload_abs):
                row.overlap_next=1;nxt.overlap_previous=1;row.write_blocked=1;nxt.write_blocked=1
    return rows


def scan_arcc_bytes(blob: bytes, archive: str, container: str, archive_entry_offset: int = 0) -> list[TextureRow]:
    if len(blob) < 0x90 or blob[:4] != b"ARCC":
        return []
    count = struct.unpack_from("<I", blob, 4)[0]
    if count <= 0 or count > 1_000_000:
        return _mark_payload_overlaps(_scan_multi_texture_arc(blob, archive, container, archive_entry_offset))
    table_end = 0x80 + count * 16
    if table_end > len(blob):
        return _mark_payload_overlaps(_scan_multi_texture_arc(blob, archive, container, archive_entry_offset))

    records: list[dict] = []
    for i in range(count):
        key, data_off, name_ref, packed = struct.unpack_from("<4I", blob, 0x80 + i * 16)
        rec_type, size = _record_type_size(packed)
        absolute = table_end + data_off
        if size and (absolute < table_end or absolute + size > len(blob)):
            continue
        records.append(dict(index=i, key=key, data_off=data_off, name_ref=name_ref,
                            type=rec_type, size=size, absolute=absolute))

    # FIX (v1.0.2-dev6): the container states where its names are.  Exactly one
    # type-0xFD record exists per ARCC bank and its payload IS the name area;
    # name_ref is a plain offset into it.  Verified across 2,668 containers with
    # zero counterexamples.  Only fall back to scanning up to 2.5 MB for
    # string-shaped bytes when that record is absent.
    name_area_end = None
    name_base = None
    _fd = [r for r in records if r["type"] == 0xFD]
    if len(_fd) == 1:
        _b = table_end + int(_fd[0]["data_off"])
        _e = _b + int(_fd[0]["size"])
        if table_end <= _b < _e <= len(blob):
            name_base, name_area_end = _b, _e
    string_by_pos: dict[int, str] = {}
    crc_names: dict[int, list[str]] = {}
    if name_base is None:
        tail_start = max(table_end, len(blob) - min(len(blob), 2_500_000))
        strings = _collect_tail_strings(blob, tail_start)
        name_base = _derive_name_base(records, strings, len(blob))
        string_by_pos = {p: s for p, s in strings}
        for _, text in strings:
            crc_names.setdefault(zlib.crc32(text.lower().encode("latin1")) & 0xFFFFFFFF, []).append(text)

    rows: list[TextureRow] = []
    used_names: set[str] = set()
    for rec in records:
        if rec["type"] != 0x01 or rec["size"] < 24:
            continue
        a = rec["absolute"]
        try:
            w1, h1, w2, h2 = struct.unpack_from("<4H", blob, a)
            fmt_raw = blob[a + 12:a + 16]
            data_size = struct.unpack_from("<I", blob, a + 16)[0]
        except struct.error:
            continue
        if fmt_raw in (b"DXT1", b"DXT5"):
            fmt = fmt_raw.decode("ascii")
            decoded = 1
        else:
            fmt_code = struct.unpack_from("<I", fmt_raw)[0]
            if fmt_code not in (0x15, 0x19):
                continue
            if fmt_code == 0x19:
                # NASCAR's native format-25 header stores quarter dimensions.
                # The clean v0.2 audit and primary parser validate it as DXT1.
                fmt = "DXT1"; w1=max(1,w1*4); h1=max(1,h1*4); decoded=1
            else:
                fmt = "FMT_0x15"; decoded=0
        if w1 <= 0 or h1 <= 0 or w1 > 16384 or h1 > 16384 or data_size <= 0:
            continue
        payload_abs = a + 24
        if payload_abs + data_size > a + rec["size"] or payload_abs + data_size > len(blob):
            continue
        if decoded:
            base_need = max(1, (w1 + 3) // 4) * max(1, (h1 + 3) // 4) * (8 if fmt == "DXT1" else 16)
            if base_need > data_size:
                continue

        name = ""
        nr = rec["name_ref"]
        if name_base is not None and nr != 0xFFFFFFFF:
            name = string_by_pos.get(name_base + nr, "")
            limit = name_area_end if name_area_end is not None else min(len(blob), name_base + nr + 160)
            if not name and name_base <= name_base + nr < limit:
                p = name_base + nr
                e = blob.find(b"\0", p, limit)
                if e > p:
                    try:
                        candidate = blob[p:e].decode("latin1")
                        if re.fullmatch(r"[A-Za-z0-9_./\\\- ]{2,120}", candidate):
                            name = candidate
                    except Exception:
                        pass
        if not name:
            names = crc_names.get(rec["key"], [])
            if names:
                name = names[0]
        if not name:
            name = f"texture_record_{rec['index']:04d}"
        if name in used_names:
            name = f"{name}__r{rec['index']}"
        used_names.add(name)
        mips = _infer_standard_mips(w1, h1, fmt, data_size)
        rows.append(TextureRow(
            archive=str(archive), container=container, entry=name,
            family=_likely_family(container, name), w=w1, h=h1, fmt=fmt,
            payload_size=data_size, payload_abs=payload_abs,
            archive_entry_offset_hex=f"0x{archive_entry_offset:X}" if archive_entry_offset else "",
            archive_entry_size=len(blob), decoded=decoded,
            decode_error=("" if decoded else f"Unsupported resource format {fmt}; raw export only"),
            record_index=rec["index"], record_type=f"0x{rec['type']:02X}",
            mip_layout=(f"standard:{mips}" if mips else "padded_or_custom"),
            mip_count=int(blob[a + 8]) if a + 8 < len(blob) else 0,
        ))
    # Also scan the older 32-byte-table multi-texture layout. Exact identities
    # discovered by the resource parser take precedence.
    combined = {(r.archive, r.container.upper(), r.entry): r for r in rows}
    for row in _scan_multi_texture_arc(blob, archive, container, archive_entry_offset):
        combined.setdefault((row.archive, row.container.upper(), row.entry), row)
    return _mark_payload_overlaps(list(combined.values()))


def scan_archive_pair(archive_id: str, archive_path: str, cdfiles_path: str,
                      max_container_mb: int = 256) -> tuple[list[TextureRow], dict]:
    entries = parse_cdfiles(cdfiles_path)
    rows: list[TextureRow] = []
    scanned = 0
    skipped_large = 0
    parse_hits = 0
    with open(archive_path, "rb") as fh:
        for off, size, name in entries:
            if not name.upper().endswith(".ARC"):
                continue
            if size <= 0:
                continue
            if size > max_container_mb * 1024 * 1024:
                skipped_large += 1
                continue
            scanned += 1
            fh.seek(off)
            blob = fh.read(size)
            if len(blob) != size:
                continue
            found = scan_arcc_bytes(blob, archive_id, name, off)
            if found:
                parse_hits += 1
                rows.extend(found)
    return rows, dict(archive=str(archive_id), containers_scanned=scanned,
                      containers_with_textures=parse_hits, skipped_large=skipped_large,
                      textures=len(rows))


def scan_registry(registry: dict[str, dict], max_container_mb: int = 256) -> tuple[list[dict], dict]:
    all_rows: list[TextureRow] = []
    reports = []
    for archive_id, paths in sorted(registry.items(), key=lambda kv: kv[0]):
        try:
            rows, report = scan_archive_pair(archive_id, paths["ar"], paths["cdf"], max_container_mb)
            all_rows.extend(rows)
            reports.append(report)
        except Exception as exc:
            reports.append(dict(archive=str(archive_id), error=str(exc), textures=0))
    # Dedupe exact identity.
    dedup: dict[tuple[str, str, str], TextureRow] = {}
    for row in all_rows:
        dedup[(row.archive, row.container.upper(), row.entry)] = row
    out = [asdict(r) for r in dedup.values()]
    summary = dict(tool=APP_NAME, version=APP_VERSION, archive_reports=reports,
                   textures=len(out), containers=sum(r.get("containers_with_textures", 0) for r in reports),
                   archives=len(reports))
    return out, summary


def write_csv(rows: Iterable[dict], path: str | os.PathLike) -> None:
    fields = [
        "archive", "container", "entry", "family", "w", "h", "fmt", "payload_size",
        "payload_abs", "archive_entry_offset_hex", "archive_entry_size", "decoded", "png_path",
        "image_sha1", "decode_error", "record_index", "record_type", "mip_layout", "mip_count", "overlap_previous", "overlap_next", "write_blocked", "source",
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    ap.add_argument("--container", help="scan one raw ARC container")
    ap.add_argument("--archive-id", default="3")
    ap.add_argument("--container-name")
    ap.add_argument("--game-data", help="NASCAR 15 data folder containing ARCHIVE*.AR and cdfiles*.dat")
    ap.add_argument("--output", required=True)
    ap.add_argument("--report")
    ap.add_argument("--max-container-mb", type=int, default=256)
    args = ap.parse_args()
    if args.container:
        p = Path(args.container)
        rows = [asdict(r) for r in scan_arcc_bytes(p.read_bytes(), args.archive_id,
                                                   args.container_name or p.name, 0)]
        summary = dict(tool=APP_NAME, version=APP_VERSION, textures=len(rows), container=str(p))
    elif args.game_data:
        data_dir = Path(args.game_data)
        reg = {}
        for cdf in data_dir.glob("cdfiles*.dat"):
            suffix = cdf.stem[7:] or "0"
            archive = data_dir / f"ARCHIVE{suffix}.AR"
            if archive.is_file():
                reg[suffix] = {"ar": str(archive), "cdf": str(cdf)}
        rows, summary = scan_registry(reg, args.max_container_mb)
    else:
        ap.error("use --container or --game-data")
    write_csv(rows, args.output)
    report_path = Path(args.report) if args.report else Path(args.output).with_suffix(".report.json")
    report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
