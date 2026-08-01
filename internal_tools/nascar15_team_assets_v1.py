#!/usr/bin/env python3
"""NASCAR 15 team presentation asset backend v1.

Provides the missing front-end half of team transfers:
* TEAM_<uid> logo resources in 2DRIVERSELECTMENUIMAGE.ARC
* 2DRIVERSELECTTD_<uid>.ARC team paint/number/thumbnail containers
* safe creation of a new cdfiles1.dat entry by cloning a stock V6 file/tree row
* additive merging of driver art and PAINTSCHEME resources into destination teams

The ARCC expansion is the same proven v2.5 layout used by native extra-scheme
thumbnails: outer count/table, resource order footer, names, and the hidden
first-wrapper directory are rebuilt together.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import os
import re
import struct
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

import containers as C

import nascar15_thumbnail_import_probe_v1 as thumb
import nascar15_thumbnail_native_v25 as v25
import nascar15_true_extra_scheme_preview_persistence_probe_v0_10 as v10

VERSION = "2.3-v1-transfer-rebuild-fallback"
ALIGNMENT = 0x10
MENU_CONTAINER = "2DRIVERSELECTMENUIMAGE.ARC"

TEAM_LOGO_RESOURCE_ALIASES = {
    1327: 'ms',
    1333: 'mk__r41',
    25430: 'mk',
}

def _team_logo_name(entries, team_uid: int) -> str | None:
    names = {str(e.name) for e in entries}
    wanted = f'TEAM_{int(team_uid)}'
    if wanted in names:
        return wanted
    alias = TEAM_LOGO_RESOURCE_ALIASES.get(int(team_uid))
    return alias if alias in names else None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_PACKAGED_ALIAS_CACHE: dict[str, list[tuple[str, str]]] | None = None


def _packaged_driver_aliases() -> dict[str, list[tuple[str, str]]]:
    """Return logical driver-art name -> [(container, physical name), ...].

    The shipped UI mapping was produced from a clean NASCAR 15 archive scan and
    records the real logical identity for short public aliases such as ``UUU``
    and ``UU__r13``.  Some stock banks do not expose that identity through table
    word 2, so relying on the live table alone makes clean JGR/RCR banks look
    incomplete.  This map is a read-only clean-game crosswalk, never user state.
    """
    global _PACKAGED_ALIAS_CACHE
    if _PACKAGED_ALIAS_CACHE is not None:
        return _PACKAGED_ALIAS_CACHE
    out: dict[str, list[tuple[str, str]]] = {}
    root = Path(__file__).resolve().parent.parent
    path = root / 'data' / 'ui_asset_map_v2.csv'
    try:
        with path.open('r', encoding='utf-8-sig', newline='') as fh:
            for row in csv.DictReader(fh):
                container = str(row.get('container') or '').strip()
                physical = str(row.get('entry') or '').strip()
                label = str(row.get('label') or '').strip()
                if not container.upper().startswith('2DRIVERSELECTTD_'):
                    continue
                m = re.search(r'\b(DRIVERPAINT_\d+_25041|DRIVER_\d+_3DNUM_25041)\b', label)
                if not m or not physical:
                    continue
                out.setdefault(m.group(1), []).append((container, physical))
    except Exception:
        out = {}
    _PACKAGED_ALIAS_CACHE = out
    return out


def _packaged_alias_entry(parsed: v10.MultiArc, resource_name: str,
                            container_name: str | None = None):
    entries = {e.name: e for e in parsed.entries}
    hits = []
    for container, physical in _packaged_driver_aliases().get(str(resource_name), []):
        if container_name and container.casefold() != str(container_name).casefold():
            continue
        entry = entries.get(physical)
        if entry is not None:
            hits.append(entry)
    # A parsed bank is already a strong discriminator.  Require one physical hit
    # so a stale/incorrect mapping cannot silently select the wrong texture.
    unique = {e.name: e for e in hits}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        raise ValueError(
            f"packaged driver-art identity is ambiguous: {resource_name} -> " +
            ", ".join(sorted(unique)))
    return None


def _v10_entry_for_logical(parsed: v10.MultiArc, resource_name: str,
                           container_name: str | None = None):
    """Resolve a public resource identity to its physical table entry.

    Several stock team banks (JGR, RCR and others) expose short public aliases
    such as ``UUU``/``RU`` while table word 2 still names the real resource,
    e.g. ``DRIVERPAINT_1096_25041``.  Exact-name lookup made clean stock banks
    look incomplete and broke transfers, previews and image replacement.
    """
    exact = next((e for e in parsed.entries if e.name == resource_name), None)
    if exact is not None:
        return exact
    hits = []
    for entry in parsed.entries:
        try:
            if _identity_name(parsed, entry) == resource_name:
                hits.append(entry)
        except Exception:
            continue
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError(
            f"native texture identity is ambiguous: {resource_name} -> " +
            ", ".join(x.name for x in hits))
    packaged = _packaged_alias_entry(parsed, resource_name, container_name)
    if packaged is not None:
        return packaged
    raise ValueError(f"native texture resource not found: {resource_name}")


def _canonical_entry(raw: bytes, resource_name: str,
                     container_name: str | None = None) -> dict[str, Any]:
    parsed = v10.parse_multi_arc(raw)
    physical = _v10_entry_for_logical(parsed, resource_name, container_name)
    entries, _ = C.parse_multi_arc(raw)
    hit = next((e for e in entries if e["name"] == physical.name), None)
    if hit is None:
        raise ValueError(f"native texture resource not found: {resource_name}")
    hit = dict(hit)
    hit['logical_name'] = str(resource_name)
    hit['physical_name'] = str(physical.name)
    return hit


def _canonical_encoder(image: Image.Image, fmt: str = "DXT5"):
    # Prefer texconv when present. Returning None lets containers.py use its
    # deterministic built-in DXT5 encoder.
    return thumb.texconv_encode(image, fmt)


def _atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".team_assets.tmp")
    with tmp.open("wb") as fh:
        fh.write(data); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def _append(path: Path, offset: int, data: bytes) -> None:
    with path.open("r+b") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        if end > offset:
            raise ValueError(f"{path.name} changed during team-asset planning")
        if end < offset:
            fh.write(b"\0" * (offset - end))
        fh.write(data); fh.flush(); os.fsync(fh.fileno())
        fh.seek(offset)
        if fh.read(len(data)) != data:
            raise ValueError(f"{path.name} append readback mismatch")


def archive_paths(game_arg, archive_index: int) -> tuple[Path, Path, Path]:
    game = Path(game_arg)
    if int(archive_index) == 0:
        archive = game / "data" / "ARCHIVE0.AR"
        cdf = game / "data" / "cdfiles.dat"
        label = "ARCHIVE0.AR/cdfiles.dat"
    elif int(archive_index) == 1:
        archive = game / "data" / "ARCHIVE1.AR"
        cdf = game / "data" / "cdfiles1.dat"
        label = "ARCHIVE1.AR/cdfiles1.dat"
    else:
        raise ValueError("only archive groups 0 and 1 are supported")
    if not archive.exists() or not cdf.exists():
        raise FileNotFoundError(label + " were not found")
    return game, archive, cdf


def game_paths(game_arg) -> tuple[Path, Path, Path]:
    """Backward-compatible alias for the team driver-art archive group."""
    return archive_paths(game_arg, 1)


@dataclasses.dataclass(frozen=True)
class V6File:
    null0: int
    folder_name_offset: int
    file_name_offset: int
    data_size: int
    uncompressed_size: int
    null1: int
    data_offset: int
    archive_index: int
    entry_type: int
    null2: int
    unk4: int

    @classmethod
    def unpack(cls, raw: bytes, offset: int) -> "V6File":
        return cls(*struct.unpack_from("<7I4B", raw, offset))

    def pack(self) -> bytes:
        return struct.pack("<7I4B", self.null0, self.folder_name_offset,
                           self.file_name_offset, self.data_size,
                           self.uncompressed_size, self.null1, self.data_offset,
                           self.archive_index, self.entry_type, self.null2,
                           self.unk4)


@dataclasses.dataclass(frozen=True)
class V6Tree:
    parent_node: int
    unk0: int
    unk1: int
    unk2: int
    name_hash: int
    folded_hash: int
    unk6: int
    flags: int
    tail_name_offset: int
    file_index: int

    @classmethod
    def unpack(cls, raw: bytes, offset: int) -> "V6Tree":
        return cls(*struct.unpack_from("<i7iII", raw, offset))

    def pack(self) -> bytes:
        return struct.pack("<i7iII", self.parent_node, self.unk0, self.unk1,
                           self.unk2, _s32(self.name_hash),
                           _s32(self.folded_hash), self.unk6,
                           _s32(self.flags), self.tail_name_offset,
                           self.file_index)


@dataclasses.dataclass
class CDFV6:
    unknown: tuple[int, int, int, int, int]
    archives: list[tuple[int, int]]
    files: list[V6File]
    trees: list[V6Tree]
    strings: bytes

    def cstr(self, offset: int) -> str:
        if offset < 0 or offset >= len(self.strings):
            raise ValueError("CDF string offset is out of range")
        end = self.strings.find(b"\0", offset)
        if end < 0:
            raise ValueError("CDF string is unterminated")
        return self.strings[offset:end].decode("ascii", "strict")

    def basename(self, file: V6File) -> str:
        return self.cstr(file.file_name_offset)

    def archive_name(self, file: V6File) -> str:
        return self.cstr(self.archives[file.archive_index][0])


def _s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v if v < 0x80000000 else v - 0x100000000


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


def fnv1(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h * 0x01000193) & 0xFFFFFFFF) ^ b
    return h


def fnv1_folded(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h * 0x01000193) & 0xFFFFFFFF) ^ (b & 0xDF)
    return h


def parse_cdf_v6(raw: bytes) -> CDFV6:
    if len(raw) < 44:
        raise ValueError("cdfiles1.dat is too short")
    magic, version = struct.unpack_from("<II", raw, 0)
    if magic != 0x436C6966 or version != 6:
        raise ValueError("cdfiles1.dat is not filC version 6")
    vals = struct.unpack_from("<9I", raw, 8)
    unknown = tuple(vals[:5])
    na, nf, nt, ss = vals[5:]
    pos = 44
    expected = pos + na * 8 + nf * 32 + nt * 40 + ss
    if expected != len(raw):
        raise ValueError(f"CDF section lengths resolve to {expected}, file has {len(raw)}")
    archives = [struct.unpack_from("<II", raw, pos + i * 8) for i in range(na)]
    pos += na * 8
    files = [V6File.unpack(raw, pos + i * 32) for i in range(nf)]
    pos += nf * 32
    trees = [V6Tree.unpack(raw, pos + i * 40) for i in range(nt)]
    pos += nt * 40
    out = CDFV6(unknown, archives, files, trees, raw[pos:pos + ss])
    real = [n.file_index for n in trees if n.file_index != 0xFFFFFFFF]
    if sorted(real) != list(range(len(files))):
        raise ValueError("CDF tree does not map every file exactly once")
    return out


def serialize_cdf_v6(cdf: CDFV6) -> bytes:
    out = bytearray(struct.pack("<II", 0x436C6966, 6))
    out += struct.pack("<9I", *cdf.unknown, len(cdf.archives), len(cdf.files),
                       len(cdf.trees), len(cdf.strings))
    for x in cdf.archives:
        out += struct.pack("<II", *x)
    for x in cdf.files:
        out += x.pack()
    for x in cdf.trees:
        out += x.pack()
    out += cdf.strings
    return bytes(out)


def _find_file(cdf: CDFV6, name: str) -> tuple[int, V6File]:
    hits = [(i, f) for i, f in enumerate(cdf.files)
            if cdf.basename(f).casefold() == name.casefold()]
    if len(hits) != 1:
        raise ValueError(f"Expected one {name} CDF record; found {len(hits)}")
    return hits[0]


def _find_tree(cdf: CDFV6, file_index: int) -> V6Tree:
    hits = [t for t in cdf.trees if t.file_index == file_index]
    if len(hits) != 1:
        raise ValueError("CDF file does not own exactly one tree node")
    return hits[0]


def cdf_has_file(cdf_raw: bytes, name: str) -> bool:
    cdf = parse_cdf_v6(cdf_raw)
    return any(cdf.basename(f).casefold() == name.casefold() for f in cdf.files)


def clone_cdf_entry(cdf_raw: bytes, donor_name: str, new_name: str,
                    data_offset: int, data_size: int) -> bytes:
    cdf = parse_cdf_v6(cdf_raw)
    new_name.encode("ascii", "strict")
    if any(cdf.basename(f).casefold() == new_name.casefold() for f in cdf.files):
        raise ValueError(f"CDF entry already exists: {new_name}")
    donor_idx, donor = _find_file(cdf, donor_name)
    tree = _find_tree(cdf, donor_idx)
    if donor.entry_type not in (4, 5):
        raise ValueError("donor CDF record is not a streamed archive file")
    if cdf.archive_name(donor).casefold() != "archive1.ar":
        raise ValueError("donor CDF record does not point to ARCHIVE1.AR")
    strings = bytearray(cdf.strings)
    name_off = len(strings)
    strings += new_name.encode("ascii") + b"\0"
    files = list(cdf.files)
    new_index = len(files)
    files.append(dataclasses.replace(donor, file_name_offset=name_off,
                                     data_size=int(data_size),
                                     uncompressed_size=int(data_size),
                                     data_offset=int(data_offset)))
    trees = list(cdf.trees)
    trees.append(dataclasses.replace(tree,
        name_hash=_s32(fnv1(new_name.encode("ascii"))),
        folded_hash=_s32(fnv1_folded(new_name.encode("ascii"))),
        tail_name_offset=name_off, file_index=new_index))
    rebuilt = serialize_cdf_v6(CDFV6(cdf.unknown, list(cdf.archives), files,
                                     trees, bytes(strings)))
    check = parse_cdf_v6(rebuilt)
    _, row = _find_file(check, new_name)
    if int(row.data_offset) != int(data_offset) or int(row.data_size) != int(data_size):
        raise ValueError("cloned CDF record failed readback")
    return rebuilt


def _resource_chunk(raw: bytes, parsed: v10.MultiArc, entry: v10.MultiEntry) -> bytes:
    footer_start, _, _ = v25._footer_bounds(raw, parsed)
    return v25._entry_resource_bytes(raw, parsed, entry, footer_start)


def _is_first_chunk(parsed: v10.MultiArc, entry: v10.MultiEntry) -> bool:
    """True when this entry owns the bank's first physical chunk.

    That chunk's leading 0x20 bytes are not texture: the bank-wide directory
    header (data_section, order_table_bytes, name_pointer, name_area) is
    written over them.
    """
    return int(entry.data_off) == 0


def _physical_texture_chunk(raw: bytes, entry: v10.MultiEntry) -> bytes:
    """Return one complete native wrapper+pixel surface from physical bounds.

    Clean RCR/JGR banks can use a resource directory that the historical v2.5
    expansion parser does not understand.  Source extraction does not need that
    footer at all: the physical 16-byte table already gives the wrapper start and
    the parsed texture profile gives the exact payload size.
    """
    block = 16 if entry.fmt == 'DXT5' else 8 if entry.fmt == 'DXT1' else 4 if entry.fmt == 'A8R8G8B8' else None
    if block is None:
        raise ValueError(f"unsupported source texture format for {entry.name}: {entry.fmt}")
    needed = (int(entry.width) * int(entry.height) * 4 if entry.fmt == 'A8R8G8B8' else
              max(1,(int(entry.width)+3)//4) * max(1,(int(entry.height)+3)//4) * block)
    start = int(entry.chunk_start)
    end = start + 96 + needed
    if start < 0 or end > len(raw):
        raise ValueError(f"complete physical texture is truncated for {entry.name}")
    return bytes(raw[start:end])


def _neutral_chunk_prefix(raw: bytes, parsed: v10.MultiArc,
                          entry: v10.MultiEntry) -> bytes:
    """Texture bytes to stand in for a bank directory header.

    Prefer a sibling with the same pixel profile so the substituted block still
    decodes as valid compressed data instead of arbitrary bytes.  Read the sibling
    from physical wrapper/payload bounds rather than assuming a particular footer.
    """
    want = struct.unpack("<8I", entry.table_record)[7]
    sibling = next((e for e in parsed.entries
                    if not _is_first_chunk(parsed, e)
                    and struct.unpack("<8I", e.table_record)[7] == want), None)
    if sibling is None:
        sibling = next((e for e in parsed.entries
                        if not _is_first_chunk(parsed, e)), None)
    if sibling is None:
        raise ValueError(
            f"cannot transfer {entry.name}: it is the source bank's first "
            f"resource and that bank has no sibling to borrow prefix bytes from")
    return _physical_texture_chunk(raw, sibling)[:0x20]


def _strip_source_directory_header(chunk: bytes, raw: bytes,
                                   parsed: v10.MultiArc,
                                   entry: v10.MultiEntry) -> tuple[bytes, bool]:
    """Replace a copied bank directory header with plausible texture bytes.

    Only the first chunk of a bank carries the directory header. Copying that
    chunk into another bank transplants a header describing the wrong
    container, and the destination bank then fails to load entirely.

    Live example: DRIVERPAINT_1121_25041 was the first chunk of its source
    bank, so its copy into 2DRIVERSELECTTD_2403.ARC carried that bank's header
    (data_section 524480, name_pointer 524512, order_bytes 24) into a container
    whose real values are 1311584 / 1311648 / 60. The game fatalled at Team
    Select until those 32 bytes were replaced.
    """
    if not _is_first_chunk(parsed, entry):
        return chunk, False
    if len(chunk) < 0x20:
        raise ValueError(f"source chunk for {entry.name} is too small to sanitize")
    return _neutral_chunk_prefix(raw, parsed, entry) + chunk[0x20:], True


try:
    import nascar15_bank_verify_v1 as bankverify
except Exception:          # verification module absent - fall back to the
    bankverify = None      # narrower inline check below


def _assert_single_directory_header(raw: bytes, parsed: v10.MultiArc) -> None:
    """Fail if any chunk other than the first looks like a directory header.

    This tests a property of the OUTPUT rather than re-deriving values the tool
    just computed, which is why it catches what _validate_directory_header
    cannot: that function only inspects the header at `base`.
    """
    if bankverify is not None:
        # Full artifact verification: order table, directory, name table,
        # offsets. Catches whatever this rebuild broke, not only the one
        # failure mode that was understood when this check was written.
        bankverify.assert_container_ok(raw, getattr(parsed, "name", ""))
        return
    for entry in parsed.entries:
        if _is_first_chunk(parsed, entry):
            continue
        blk = _resource_chunk(raw, parsed, entry)[:0x20]
        if len(blk) == 0x20 and blk[8:12] == b"\xff\xff\xff\xff" and blk[12] == 0x42:
            ds, = struct.unpack_from("<I", blk, 4)
            raise ValueError(
                f"{entry.name} carries a foreign bank directory header "
                f"(claims data_section={ds}); this bank would not load")


def _standalone_resource_chunk(raw: bytes, parsed: v10.MultiArc,
                               entry: v10.MultiEntry) -> bytes:
    """Copy one complete physical wrapper and native texture payload."""
    return _physical_texture_chunk(raw, entry)


def _rebuild_same_count(dest_raw: bytes, replacement_name: str,
                        replacement_chunk: bytes,
                        replacement_fields: list[int]) -> bytes:
    """Rebuild one bank at the same count with a resized replacement chunk."""
    dest = v10.parse_multi_arc(dest_raw)
    target = v10.entry_by_name(dest, replacement_name)
    footer_start, old_footer, _ = v25._footer_bounds(dest_raw, dest)
    v25._validate_directory_header(dest_raw, dest, footer_start)
    records = []
    chunks = []
    data_off = 0
    for entry in dest.entries:
        fields = list(struct.unpack('<8I', entry.table_record))
        chunk = replacement_chunk if entry.name == replacement_name else _resource_chunk(dest_raw, dest, entry)
        if entry.name == replacement_name:
            fields = list(replacement_fields)
        fields[5] = data_off
        fields[6] = int(entry.name_ref)
        records.append(struct.pack('<8I', *fields))
        chunks.append(chunk)
        data_off += len(chunk)
    header = bytearray(dest_raw[:v25.HEADER_SIZE])
    old_names = dest_raw[dest.name_blob:]
    rebuilt = bytearray(bytes(header) + b''.join(records) + b''.join(chunks) +
                        old_footer + old_names)
    new_base = v25.HEADER_SIZE + dest.count * v25.TABLE_RECORD_SIZE
    footer_planned = new_base + sum(map(len, chunks))
    names_planned = footer_planned + len(old_footer)
    v25._patch_directory_header(rebuilt, new_base, dest.count, footer_planned,
                                names_planned, len(rebuilt))
    struct.pack_into('<i', rebuilt, 0x70,
                     0x8000 - v25.align(len(rebuilt), v25.CONTAINER_ALIGN))
    rebuilt = bytes(rebuilt)
    check = v10.parse_multi_arc(rebuilt)
    if check.count != dest.count:
        raise ValueError('same-count team resource rebuild changed entry count')
    cfooter, _, _ = v25._footer_bounds(rebuilt, check)
    v25._validate_directory_header(rebuilt, check, cfooter)
    got = v10.entry_by_name(check, replacement_name)
    got_chunk = _resource_chunk(rebuilt, check, got)
    # Entry 0 owns a bank-wide hidden directory in its first 0x20 bytes.  The
    # directory is necessarily rewritten when any resource size/offset moves,
    # so a byte-for-byte comparison against the source wrapper is invalid for
    # the first resource.  The actual texture wrapper/payload begins at 0x20.
    if got.index == 0:
        if got_chunk[0x20:] != replacement_chunk[0x20:]:
            raise ValueError(f"replacement first-resource payload failed readback: {replacement_name}")
    elif got_chunk != replacement_chunk:
        raise ValueError(f"replacement resource failed readback: {replacement_name}")
    for old in dest.entries:
        if old.name == replacement_name:
            continue
        now = v10.entry_by_name(check, old.name)
        before = _resource_chunk(dest_raw, dest, old)
        after = _resource_chunk(rebuilt, check, now)
        if old.index == 0:
            if before[0x20:] != after[0x20:]:
                raise ValueError('first resource changed outside hidden directory')
        elif before != after:
            raise ValueError(f"unrelated team resource changed: {old.name}")
    return rebuilt


def _source_name_record(raw: bytes, parsed: v10.MultiArc, entry: v10.MultiEntry) -> bytes:
    """Return the exact 9-byte hash prefix + public name + NUL."""
    start = parsed.name_blob + int(entry.name_ref) - 9
    if start < parsed.name_blob:
        raise ValueError(f"name preamble is out of range for {entry.name}")
    end = raw.find(b"\0", parsed.name_blob + int(entry.name_ref))
    if end < 0:
        raise ValueError(f"name record is unterminated for {entry.name}")
    return bytes(raw[start:end + 1])


def _identity_name(parsed: v10.MultiArc, entry: v10.MultiEntry) -> str | None:
    """Resolve table word 2 to the source resource whose identity it names."""
    fields = struct.unpack("<8I", entry.table_record)
    identity_ref = int(fields[2])
    hit = next((e for e in parsed.entries if int(e.name_ref) == identity_ref), None)
    return hit.name if hit is not None else None


def _is_native_paint_identity(parsed: v10.MultiArc, entry: v10.MultiEntry | None) -> bool:
    """A stable alias anchor is a self-identifying PAINTSCHEME resource."""
    if entry is None or not entry.name.startswith("PAINTSCHEME_"):
        return False
    fields = struct.unpack("<8I", entry.table_record)
    return int(fields[2]) == int(entry.name_ref)




def _paint_identity_root(parsed: v10.MultiArc, resource_name: str) -> str:
    """Resolve a PAINTSCHEME alias chain to its self-identifying root."""
    current = str(resource_name)
    seen = set()
    while True:
        if current in seen:
            raise ValueError(f"PAINTSCHEME identity cycle detected at {current}")
        seen.add(current)
        entry = v10.entry_by_name(parsed, current)
        if not entry.name.startswith("PAINTSCHEME_"):
            raise ValueError(f"{current} is not a PAINTSCHEME identity resource")
        identity = _identity_name(parsed, entry)
        if identity is None or not identity.startswith("PAINTSCHEME_"):
            raise ValueError(f"{current} has an unresolved/non-paint identity dependency")
        if identity == current:
            if not _is_native_paint_identity(parsed, entry):
                raise ValueError(f"{current} is not a self-identifying PAINTSCHEME root")
            return current
        current = identity

def _repair_existing_resource(dest_raw: bytes, source_raw: bytes,
                              resource_name: str,
                              fallback_identity_name: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """Repair identity references and make copied texture payloads standalone."""
    dest = v10.parse_multi_arc(dest_raw)
    src = v10.parse_multi_arc(source_raw)
    existing = v10.entry_by_name(dest, resource_name)
    source = v10.entry_by_name(src, resource_name)
    source_fields = list(struct.unpack("<8I", source.table_record))
    dest_fields = list(struct.unpack("<8I", existing.table_record))
    changed_parts: list[str] = []

    identity_name = _identity_name(src, source)
    identity_dest = next((e for e in dest.entries if e.name == identity_name), None)
    if identity_name == resource_name:
        wanted_identity = int(existing.name_ref)
        resolved_identity_name = resource_name
    elif (identity_dest is not None and
          (not resource_name.startswith("PAINTSCHEME_") or
           _paint_identity_root(dest, identity_dest.name))):
        wanted_identity = int(identity_dest.name_ref)
        resolved_identity_name = identity_dest.name
    else:
        raise ValueError(
            f"{resource_name} depends on the exact PAINTSCHEME identity "
            f"{identity_name or 'unresolved'}, which is not present in the destination bank"
        )
    if int(dest_fields[2]) != wanted_identity:
        dest_fields[2] = wanted_identity
        changed_parts.append("identity_ref")
    if int(dest_fields[6]) != int(existing.name_ref):
        dest_fields[6] = int(existing.name_ref)
        changed_parts.append("name_ref")

    source_chunk = _standalone_resource_chunk(source_raw, src, source)
    existing_chunk = _resource_chunk(dest_raw, dest, existing)
    # The first resource's first 0x20 bytes describe the destination bank as a
    # whole.  Keep those bytes from the destination and repair only the native
    # wrapper/pixels from the pristine source.  Copying a source bank's hidden
    # directory into another bank is structurally wrong even when the picture
    # bytes are correct.
    # The first chunk's leading 0x20 bytes are the bank-wide directory header,
    # not texture. Two separate cases, and only the first used to be handled:
    #   * destination entry is first -> keep the DESTINATION's header bytes
    #   * source entry is first      -> its bytes are the SOURCE bank's header
    #                                   and must never be transplanted
    # Testing only `existing.index == 0` missed the second case entirely, which
    # is how a foreign directory header reached 2DRIVERSELECTTD_2403.ARC.
    sanitized_source_header = False
    if existing.index == 0:
        source_chunk = existing_chunk[:0x20] + source_chunk[0x20:]
        payload_differs = source_chunk[0x20:] != existing_chunk[0x20:]
    else:
        source_chunk, sanitized_source_header = _strip_source_directory_header(
            source_chunk, source_raw, src, source)
        payload_differs = source_chunk != existing_chunk
    if sanitized_source_header:
        changed_parts.append("source_directory_header_stripped")
    if payload_differs:
        changed_parts.append("complete_texture_payload")

    if not changed_parts:
        return dest_raw, {"changed": False, "resource": resource_name,
                          "repaired": False}

    rebuilt = _rebuild_same_count(dest_raw, resource_name, source_chunk, dest_fields)
    check = v10.parse_multi_arc(rebuilt)
    _assert_single_directory_header(rebuilt, check)
    got = v10.entry_by_name(check, resource_name)
    got_fields = struct.unpack("<8I", got.table_record)
    if int(got_fields[6]) != int(got.name_ref):
        raise ValueError(f"repaired name reference failed for {resource_name}")
    if identity_name == resource_name and int(got_fields[2]) != int(got.name_ref):
        raise ValueError(f"repaired identity reference failed for {resource_name}")
    return rebuilt, {"changed": True, "resource": resource_name,
                     "repaired": True, "repairs": changed_parts,
                     "identity_source": identity_name,
                     "identity_resolved_to": resolved_identity_name,
                     "old_size": len(dest_raw), "new_size": len(rebuilt)}


def add_exact_resource(dest_raw: bytes, source_raw: bytes, resource_name: str,
                       fallback_identity_name: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """Append one exact source resource or repair a prior copied resource."""
    dest = v10.parse_multi_arc(dest_raw)
    if any(e.name == resource_name for e in dest.entries):
        return _repair_existing_resource(
            dest_raw, source_raw, resource_name, fallback_identity_name)
    src = v10.parse_multi_arc(source_raw)
    source = v10.entry_by_name(src, resource_name)
    source_chunk = _standalone_resource_chunk(source_raw, src, source)
    # An appended resource is never the destination's first chunk, so if the
    # source entry was first in ITS bank those leading 0x20 bytes are that
    # bank's directory header and must be replaced with texture bytes.
    source_chunk, sanitized_source_header = _strip_source_directory_header(
        source_chunk, source_raw, src, source)

    footer_start, old_footer, _ = v25._footer_bounds(dest_raw, dest)
    v25._validate_directory_header(dest_raw, dest, footer_start)
    old_table = dest_raw[v25.HEADER_SIZE:dest.base]
    old_chunks = dest_raw[dest.base:footer_start]
    old_names = dest_raw[dest.name_blob:]
    new_count = dest.count + 1
    data_off = len(old_chunks)
    name_ref = len(old_names) + 9
    fields = list(struct.unpack("<8I", source.table_record))
    identity_name = _identity_name(src, source)
    identity_dest = next((e for e in dest.entries if e.name == identity_name), None)
    if identity_name == resource_name:
        fields[2] = name_ref
        resolved_identity_name = resource_name
    elif (identity_dest is not None and
          (not resource_name.startswith("PAINTSCHEME_") or
           _paint_identity_root(dest, identity_dest.name))):
        fields[2] = int(identity_dest.name_ref)
        resolved_identity_name = identity_dest.name
    else:
        raise ValueError(
            f"cannot copy {resource_name}: its exact source identity "
            f"{identity_name or 'unresolved'} is not present in the destination bank"
        )
    fields[5] = data_off
    fields[6] = name_ref
    new_record = struct.pack("<8I", *fields)
    new_footer = v25._new_footer(old_footer, new_count)
    # Preserve the source's exact hash/identity prefix. This matters for app-
    # created thumbnail aliases whose visible name intentionally differs from
    # the donor CRC pair.
    name_record = _source_name_record(source_raw, src, source)
    header = bytearray(dest_raw[:v25.HEADER_SIZE])
    struct.pack_into("<I", header, 4, new_count * 2 + 2)
    struct.pack_into("<I", header, 8, new_count)
    rebuilt = bytearray(bytes(header) + old_table + new_record + old_chunks +
                        source_chunk + new_footer + old_names + name_record)
    new_base = v25.HEADER_SIZE + new_count * v25.TABLE_RECORD_SIZE
    footer_planned = new_base + len(old_chunks) + len(source_chunk)
    names_planned = footer_planned + len(new_footer)
    v25._patch_directory_header(rebuilt, new_base, new_count, footer_planned,
                                names_planned, len(rebuilt))
    struct.pack_into("<i", rebuilt, 0x70,
                     0x8000 - v25.align(len(rebuilt), v25.CONTAINER_ALIGN))
    rebuilt = bytes(rebuilt)

    check = v10.parse_multi_arc(rebuilt)
    if check.count != new_count:
        raise ValueError("resource merge count failed readback")
    target = v10.entry_by_name(check, resource_name)
    if _resource_chunk(rebuilt, check, target) != source_chunk:
        raise ValueError(f"merged resource changed: {resource_name}")
    check_footer, _, _ = v25._footer_bounds(rebuilt, check)
    v25._validate_directory_header(rebuilt, check, check_footer)
    _assert_single_directory_header(rebuilt, check)
    target_fields = struct.unpack("<8I", target.table_record)
    if identity_name == resource_name and int(target_fields[2]) != int(target.name_ref):
        raise ValueError(f"self identity was not relocated for {resource_name}")
    for old in dest.entries:
        got = v10.entry_by_name(check, old.name)
        old_chunk = _resource_chunk(dest_raw, dest, old)
        new_chunk = _resource_chunk(rebuilt, check, got)
        if old.index == 0:
            if old_chunk[0x20:] != new_chunk[0x20:]:
                raise ValueError("first resource changed outside hidden directory")
        elif old_chunk != new_chunk:
            raise ValueError(f"existing resource changed: {old.name}")
    return rebuilt, {
        "changed": True, "resource": resource_name,
        "old_count": dest.count, "new_count": new_count,
        "old_size": len(dest_raw), "new_size": len(rebuilt),
        "source_sha256": _sha(source_chunk), "rebuilt_sha256": _sha(rebuilt),
        "identity_relocated": identity_name == resource_name,
        "identity_source": identity_name,
        "identity_resolved_to": resolved_identity_name,
    }



def add_logical_driver_resource(dest_raw: bytes, source_raw: bytes,
                                source_resource_name: str,
                                target_resource_name: str) -> tuple[bytes, dict[str, Any]]:
    """Copy a short-alias stock driver resource under its real public name.

    This is deliberately limited to DRIVERPAINT/DRIVER_*_3DNUM resources. Paint
    thumbnail aliases have a separate dependency chain and must keep using the
    exact PAINTSCHEME copier.
    """
    if not (target_resource_name.startswith('DRIVERPAINT_') or
            (target_resource_name.startswith('DRIVER_') and '_3DNUM_' in target_resource_name)):
        raise ValueError('logical alias transplant is restricted to driver art')
    dest = v10.parse_multi_arc(dest_raw)
    src = v10.parse_multi_arc(source_raw)
    source = v10.entry_by_name(src, source_resource_name)
    source_chunk = _standalone_resource_chunk(source_raw, src, source)
    source_chunk, sanitized = _strip_source_directory_header(
        source_chunk, source_raw, src, source)

    # Repair/replace an existing logical target, including an existing short alias.
    try:
        existing = _v10_entry_for_logical(dest, target_resource_name)
    except ValueError:
        existing = None
    if existing is not None:
        dest_fields = list(struct.unpack('<8I', existing.table_record))
        existing_chunk = _resource_chunk(dest_raw, dest, existing)
        if existing.index == 0:
            source_chunk = existing_chunk[:0x20] + source_chunk[0x20:]
        # Keep the physical alias's existing identity/name references. They are
        # already what makes the clean bank resolve to target_resource_name.
        rebuilt = _rebuild_same_count(
            dest_raw, existing.name, source_chunk, dest_fields)
        check = v10.parse_multi_arc(rebuilt)
        got = _v10_entry_for_logical(check, target_resource_name)
        _assert_single_directory_header(rebuilt, check)
        return rebuilt, {
            'changed': rebuilt != dest_raw,
            'resource': target_resource_name,
            'physical_resource': existing.name,
            'source_resource': source_resource_name,
            'repaired': True,
            'source_directory_header_stripped': bool(sanitized),
            'old_size': len(dest_raw), 'new_size': len(rebuilt),
        }

    footer_start, old_footer, _ = v25._footer_bounds(dest_raw, dest)
    v25._validate_directory_header(dest_raw, dest, footer_start)
    old_table = dest_raw[v25.HEADER_SIZE:dest.base]
    old_chunks = dest_raw[dest.base:footer_start]
    old_names = dest_raw[dest.name_blob:]
    new_count = dest.count + 1
    data_off = len(old_chunks)
    name_ref = len(old_names) + 9
    fields = list(struct.unpack('<8I', source.table_record))
    # The destination owns a real, long public name. Make it self-identifying
    # rather than carrying a hidden pointer into the source alias bank.
    fields[2] = name_ref
    fields[5] = data_off
    fields[6] = name_ref
    new_record = struct.pack('<8I', *fields)
    new_footer = v25._new_footer(old_footer, new_count)
    name_record = (v25._name_preamble(target_resource_name) +
                   target_resource_name.encode('latin1') + b'\0')
    header = bytearray(dest_raw[:v25.HEADER_SIZE])
    struct.pack_into('<I', header, 4, new_count * 2 + 2)
    struct.pack_into('<I', header, 8, new_count)
    rebuilt = bytearray(bytes(header) + old_table + new_record + old_chunks +
                        source_chunk + new_footer + old_names + name_record)
    new_base = v25.HEADER_SIZE + new_count * v25.TABLE_RECORD_SIZE
    footer_planned = new_base + len(old_chunks) + len(source_chunk)
    names_planned = footer_planned + len(new_footer)
    v25._patch_directory_header(rebuilt, new_base, new_count, footer_planned,
                                names_planned, len(rebuilt))
    struct.pack_into('<i', rebuilt, 0x70,
                     0x8000 - v25.align(len(rebuilt), v25.CONTAINER_ALIGN))
    rebuilt = bytes(rebuilt)

    check = v10.parse_multi_arc(rebuilt)
    if check.count != new_count:
        raise ValueError('logical driver resource count failed readback')
    target = v10.entry_by_name(check, target_resource_name)
    target_fields = struct.unpack('<8I', target.table_record)
    if int(target_fields[2]) != int(target.name_ref) or int(target_fields[6]) != int(target.name_ref):
        raise ValueError('logical driver resource is not self-identifying')
    if _resource_chunk(rebuilt, check, target) != source_chunk:
        raise ValueError('logical driver resource payload changed during merge')
    check_footer, _, _ = v25._footer_bounds(rebuilt, check)
    v25._validate_directory_header(rebuilt, check, check_footer)
    _assert_single_directory_header(rebuilt, check)
    return rebuilt, {
        'changed': True, 'resource': target_resource_name,
        'physical_resource': target_resource_name,
        'source_resource': source_resource_name,
        'short_alias_resolved': source_resource_name != target_resource_name,
        'source_directory_header_stripped': bool(sanitized),
        'old_count': dest.count, 'new_count': new_count,
        'old_size': len(dest_raw), 'new_size': len(rebuilt),
    }

def _replace_driver_art_image(raw: bytes, resource_name: str, image_path: Path) -> tuple[bytes, str, dict[str, Any]]:
    """Replace one DRIVERPAINT/3DNUM texture through the native 16-byte table.

    The previous 0x40 overlap-tail theory came from the synthetic 32-byte view
    and wrote the wrong byte range.  The clean game proves these are complete,
    standard-order DXT5 payloads behind a 24-byte texture header.
    """
    target = _canonical_entry(raw, resource_name)
    if target["fmt"] != "DXT5":
        raise ValueError(f'{resource_name} is {target["fmt"]}; expected DXT5')
    if int(target["payload_size"]) < int(target["needed"]):
        raise ValueError(
            f'{resource_name} payload is {target["payload_size"]} bytes; '
            f'expected {target["needed"]}'
        )
    image = Image.open(image_path); image.load()
    rebuilt = C.multi_write_png_validated(
        raw, target, image, encode_fn=_canonical_encoder)
    target2 = _canonical_entry(rebuilt, resource_name)
    # Readback must decode using the same native geometry the game uses.
    decoded = C.multi_read_png(rebuilt, target2)
    if decoded.size != (int(target["w"]), int(target["h"])):
        raise ValueError('driver-art image readback dimensions changed')
    encoder = "native DXT5 (texconv preferred; built-in fallback)"
    return rebuilt, encoder, {
        'native_layout': str(target.get('layout')),
        'payload_abs': int(target['payload_abs']),
        'payload_size': int(target['payload_size']),
        'full_encoded_bytes': int(target['needed']),
        'dxt5_swapped': bool(target.get('dxt5_swapped')),
        'readback_verified': True,
    }


def _replace_resource_image(raw: bytes, resource_name: str, image_path: Path) -> tuple[bytes, str]:
    target = _canonical_entry(raw, resource_name)
    if target["fmt"] not in ("DXT1", "DXT5", "A8R8G8B8"):
        raise ValueError(f'unsupported native texture format: {target["fmt"]}')
    if int(target["payload_size"]) < int(target["needed"]):
        raise ValueError(
            f'{resource_name} payload is {target["payload_size"]} bytes; '
            f'expected {target["needed"]}'
        )
    image = Image.open(image_path); image.load()
    rebuilt = C.multi_write_png_validated(
        raw, target, image, encode_fn=_canonical_encoder)
    C.multi_read_png(rebuilt, _canonical_entry(rebuilt, resource_name))
    encoder = f'native {target["fmt"]}'
    return rebuilt, encoder


def _write_existing_container(archive: Path, cdf_path: Path, name: str,
                              expected_row: v10.CdfRow, data: bytes) -> dict[str, Any]:
    old_size = archive.stat().st_size
    old_cdf = cdf_path.read_bytes()
    new_off = v25.align(old_size, ALIGNMENT)
    try:
        _append(archive, new_off, data)
        raw, rows = v10.parse_cdf_rows(cdf_path)
        row = v10.find_row(rows, name)
        if row.offset != expected_row.offset or row.size != expected_row.size:
            raise ValueError("container CDF row changed during planning")
        struct.pack_into("<I", raw, row.offset_pos, new_off)
        struct.pack_into("<I", raw, row.size_pos, len(data))
        _atomic(cdf_path, bytes(raw))
        _, rows2 = v10.parse_cdf_rows(cdf_path)
        got = v10.find_row(rows2, name)
        if v10.read_entry(archive, got) != data:
            raise ValueError("repointed team container failed indexed readback")
        return {"created": False, "container": name, "offset": new_off,
                "size": len(data), "readback_verified": True}
    except Exception as original_error:
        rollback_errors = []
        try:
            _atomic(cdf_path, old_cdf)
            with archive.open("r+b") as fh:
                fh.truncate(old_size); fh.flush(); os.fsync(fh.fileno())
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise


def _write_new_container(archive: Path, cdf_path: Path, donor_name: str,
                         new_name: str, data: bytes) -> dict[str, Any]:
    old_size = archive.stat().st_size
    old_cdf = cdf_path.read_bytes()
    new_off = v25.align(old_size, ALIGNMENT)
    try:
        _append(archive, new_off, data)
        rebuilt_cdf = clone_cdf_entry(old_cdf, donor_name, new_name,
                                      new_off, len(data))
        _atomic(cdf_path, rebuilt_cdf)
        _, rows = v10.parse_cdf_rows(cdf_path)
        row = v10.find_row(rows, new_name)
        if v10.read_entry(archive, row) != data:
            raise ValueError("new team container failed indexed readback")
        return {"created": True, "container": new_name, "offset": new_off,
                "size": len(data), "readback_verified": True}
    except Exception as original_error:
        rollback_errors = []
        try:
            _atomic(cdf_path, old_cdf)
            with archive.open("r+b") as fh:
                fh.truncate(old_size); fh.flush(); os.fsync(fh.fileno())
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise


def _all_td_from_paths(archive: Path, cdf: Path) -> list[tuple[v10.CdfRow, bytes, v10.MultiArc]]:
    _, rows = v10.parse_cdf_rows(cdf)
    out = []
    for row in rows:
        if not row.name.upper().startswith("2DRIVERSELECTTD_"):
            continue
        try:
            raw = v10.read_entry(archive, row)
            out.append((row, raw, v10.parse_multi_arc(raw)))
        except Exception:
            continue
    return out


def _all_td(game: Path) -> list[tuple[v10.CdfRow, bytes, v10.MultiArc]]:
    _, archive, cdf = game_paths(game)
    return _all_td_from_paths(archive, cdf)


def _backup_variant(path: Path) -> Path | None:
    """Return the oldest backup by release lineage, never by mtime.

    Copying an install, cloud sync and ZIP extraction can rewrite timestamps.
    A legacy ``.gridapp.bak`` necessarily predates ``.n15mod.bak`` and is the
    safer stock source even when its filesystem mtime is newer.
    """
    for suffix in (".gridapp.bak", ".n15mod.bak", ".bak"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            return candidate
    return None


def _pristine_td(game: Path) -> list[tuple[v10.CdfRow, bytes, v10.MultiArc]]:
    """Read stock driver-art banks from the app's pristine backups when present."""
    _, archive, cdf = game_paths(game)
    bar = _backup_variant(archive)
    bcdf = _backup_variant(cdf)
    if not bar or not bcdf:
        return []
    try:
        return _all_td_from_paths(bar, bcdf)
    except Exception:
        return []


def _bank_named(banks, name: str):
    return [x for x in banks if x[0].name.casefold() == str(name).casefold()]


def find_resource(game_arg, resource_name: str):
    game, _, _ = game_paths(game_arg)
    for row, raw, parsed in _all_td(game):
        try:
            entry = _v10_entry_for_logical(parsed, resource_name)
            return row, raw, parsed, entry
        except Exception:
            continue
    return None


def team_logo_spec(game_arg, team_uid: int, donor_team_uid: int | None = None) -> dict[str, Any]:
    """Return the exact native dimensions/format used by a TEAM resource."""
    game, archive, cdf = archive_paths(game_arg, 0)
    _, rows = v10.parse_cdf_rows(cdf)
    row = v10.find_row(rows, MENU_CONTAINER)
    raw = v10.read_entry(archive, row)
    parsed = v10.parse_multi_arc(raw)
    target = _team_logo_name(parsed.entries, int(team_uid))
    donor = (_team_logo_name(parsed.entries, int(donor_team_uid)) if donor_team_uid is not None else None)
    entry = next((e for e in parsed.entries if e.name == target), None) if target else None
    source_name = target or f"TEAM_{int(team_uid)}"
    if entry is None and donor:
        entry = next((e for e in parsed.entries if e.name == donor), None)
        source_name = donor
    if entry is None:
        raise ValueError(f"no native TEAM resource is available for {team_uid}")
    return {"entry": source_name, "width": int(entry.width),
            "height": int(entry.height), "format": str(entry.fmt)}


def ensure_team_logo(game_arg, team_uid: int, donor_team_uid: int,
                     image_path=None) -> dict[str, Any]:
    game, archive, cdf = archive_paths(game_arg, 0)
    requested_target = f"TEAM_{int(team_uid)}"
    _, rows = v10.parse_cdf_rows(cdf)
    row = v10.find_row(rows, MENU_CONTAINER)
    original = v10.read_entry(archive, row)
    parsed = v10.parse_multi_arc(original)
    names = {e.name for e in parsed.entries}
    target_name = _team_logo_name(parsed.entries, int(team_uid))
    donor_name = _team_logo_name(parsed.entries, int(donor_team_uid))
    if target_name:
        rebuilt = original
        added = False
    else:
        target_name = requested_target
        if not donor_name:
            raise ValueError(f"source team logo for team {donor_team_uid} was not found")
        # Add under a new public name by using the proven v2.5
        # identity-preserving expansion. The donor resource remains untouched.
        rebuilt, _report = v25.build_expanded_container(original, donor_name, target_name)
        added = True
    encoder = None
    if image_path:
        rebuilt, encoder = _replace_resource_image(rebuilt, target_name, Path(image_path))
    if rebuilt == original:
        return {"ok": True, "changed": False, "container": MENU_CONTAINER,
                "entry": target_name, "added": False, "encoder": encoder}
    written = _write_existing_container(archive, cdf, MENU_CONTAINER, row, rebuilt)
    # Final indexed parse.
    _, rows2 = v10.parse_cdf_rows(cdf)
    check_raw = v10.read_entry(archive, v10.find_row(rows2, MENU_CONTAINER))
    v10.entry_by_name(v10.parse_multi_arc(check_raw), target_name)
    return {"ok": True, "changed": True, "container": MENU_CONTAINER,
            "entry": target_name, "added": added, "encoder": encoder, **written}


def replace_team_logo(game_arg, team_uid: int, image_path) -> dict[str, Any]:
    game, archive, cdf = archive_paths(game_arg, 0)
    _, rows = v10.parse_cdf_rows(cdf)
    row = v10.find_row(rows, MENU_CONTAINER)
    original = v10.read_entry(archive, row)
    parsed = v10.parse_multi_arc(original)
    target = _team_logo_name(parsed.entries, int(team_uid))
    if not target:
        raise ValueError(f"TEAM_{int(team_uid)} does not exist yet; prepare the team first")
    rebuilt, encoder = _replace_resource_image(original, target, Path(image_path))
    written = _write_existing_container(archive, cdf, MENU_CONTAINER, row, rebuilt)
    return {"ok": True, "changed": True, "entry": target,
            "encoder": encoder, **written}


def _source_kind(row, pristine_banks) -> str:
    return 'pristine' if any(row.name.casefold() == pr[0].name.casefold() and raw == pr[1]
                             for pr in pristine_banks for raw in [pr[1]]) else 'live'


def _paint_identity_chain(source_raw: bytes, resource_name: str) -> list[str]:
    """Return exact hidden PAINTSCHEME dependencies, root first.

    Table word 2 is not a generic same-bank pointer.  The working pre-team
    thumbnails preserve the donor's exact identity chain.  Moving an alias
    without that dependency—or rebinding it to an arbitrary paint—can fatal the
    whole Paint Select bank.
    """
    parsed = v10.parse_multi_arc(source_raw)
    chain = []
    seen = set()
    current = str(resource_name)
    while True:
        if current in seen:
            raise ValueError(f"PAINTSCHEME identity cycle detected at {current}")
        seen.add(current)
        entry = v10.entry_by_name(parsed, current)
        identity = _identity_name(parsed, entry)
        if identity is None or not identity.startswith('PAINTSCHEME_'):
            raise ValueError(f"{current} has an unresolved/non-paint identity dependency")
        if identity == current:
            break
        chain.append(identity)
        current = identity
    chain.reverse()
    return chain


def ensure_driver_assets(game_arg, destination_team_uid: int,
                         source_team_uid: int, driver_uid: int,
                         livery_uids: Iterable[int],
                         destination_empty: bool = False) -> dict[str, Any]:
    """Use the public-v1 direct destination-bank revision path.

    The v1.0 release successfully transferred drivers by reading the currently
    indexed destination bank, appending/repairing only the required resources,
    writing a complete new revision, and repointing the CDF entry. dev2-dev4
    added a preflight parser for a supposed resource-order footer. Several real
    stock banks do not use that interpreted layout, so the preflight read DXT
    texture bytes as directory integers and blocked transfers before the proven
    writer ran.

    Keep the newer logical short-alias resolver, but do not require the source or
    destination bank to pass that unrelated footer model. ``destination_empty``
    is accepted for API compatibility and intentionally does not select a
    different writer.
    """
    game, archive, cdf = game_paths(game_arg)
    dest_name = f"2DRIVERSELECTTD_{int(destination_team_uid)}.ARC"
    source_name = f"2DRIVERSELECTTD_{int(source_team_uid)}.ARC"
    _, rows = v10.parse_cdf_rows(cdf)
    row_by_name = {r.name.casefold(): r for r in rows}
    dest_row = row_by_name.get(dest_name.casefold())
    source_row = row_by_name.get(source_name.casefold())
    if source_row is None:
        raise ValueError(f"source team art container is missing: {source_name}")
    source_live_raw = v10.read_entry(archive, source_row)
    if dest_row is None:
        dest_raw = source_live_raw
        created = True
    else:
        dest_raw = v10.read_entry(archive, dest_row)
        created = False

    mandatory = [f"DRIVERPAINT_{int(driver_uid)}_25041",
                 f"DRIVER_{int(driver_uid)}_3DNUM_25041"]
    requested_paints = list(dict.fromkeys(
        f"PAINTSCHEME_{int(uid)}" for uid in livery_uids))

    live_banks = _all_td(game)
    pristine_banks = _pristine_td(game)
    # The current live source team is authoritative for presentation assets.
    # This preserves user-replaced Paint Select thumbnails, driver photos, and
    # 3D-number art when the driver is moved.  Pristine banks remain the fallback
    # only when the live source does not contain a structurally usable resource.
    preferred_source = _bank_named(live_banks, source_name) + _bank_named(pristine_banks, source_name)
    live_other = [x for x in live_banks if x[0].name.casefold() != dest_name.casefold()]
    pristine_other = [x for x in pristine_banks if x[0].name.casefold() != source_name.casefold()]
    destination_hits = _bank_named(live_banks, dest_name)

    sources: dict[str, tuple[bytes, str, str, str]] = {}
    def locate(name: str, candidates, allow_identity_alias=False) -> tuple[bytes, str, str, str] | None:
        for row, raw, parsed in candidates:
            actual = next((e.name for e in parsed.entries if e.name == name), None)
            if actual is None and allow_identity_alias:
                try:
                    actual = _v10_entry_for_logical(parsed, name, row.name).name
                except Exception:
                    actual = None
            if actual is not None:
                kind = 'pristine' if any(row.name.casefold() == pr[0].name.casefold() and raw == pr[1]
                                         for pr in pristine_banks) else 'live'
                return raw, row.name, kind, actual
        return None

    for name in mandatory:
        found = locate(name, preferred_source + pristine_other + live_other,
                       allow_identity_alias=True)
        if found:
            sources[name] = found
    hard_missing = [n for n in mandatory if n not in sources]
    if hard_missing:
        raise ValueError("required driver-select resources are missing: " + ", ".join(hard_missing))

    missing_optional = []
    for name in requested_paints:
        found = locate(name, preferred_source + live_other + pristine_other + destination_hits,
                       allow_identity_alias=False)
        if found:
            sources[name] = found
        else:
            missing_optional.append(name)

    # Pull the exact alias anchors from the same source bank as each requested
    # paint.  These are support resources, not extra livery assignments.
    support_paints = []
    expected_identity: dict[str, str] = {}
    for name in requested_paints:
        source = sources.get(name)
        if source is None:
            continue
        src_raw, src_container, src_kind, src_actual = source
        parsed = v10.parse_multi_arc(src_raw)
        entry = v10.entry_by_name(parsed, src_actual)
        identity = _identity_name(parsed, entry)
        if identity is None:
            # This bank's table record points at an identity that is not present
            # in it, so the alias chain is broken here. Requested paints are
            # already optional when a source cannot be found at all, so treat an
            # unresolvable one the same way: skip it, report it, and let the rest
            # of the transfer finish. Dropping it from `sources` removes it from
            # `ordered`, so it is neither written nor verified.
            missing_optional.append(name)
            sources.pop(name, None)
            continue
        expected_identity[name] = identity
        for dep in _paint_identity_chain(src_raw, name):
            dep_entry = v10.entry_by_name(parsed, dep)
            dep_identity = _identity_name(parsed, dep_entry)
            expected_identity[dep] = dep_identity or dep
            if dep not in sources:
                sources[dep] = (src_raw, src_container, src_kind, dep)
            if dep not in support_paints and dep not in requested_paints:
                support_paints.append(dep)

    # Dependencies are already root-first per chain; dedupe without reordering.
    support_paints = list(dict.fromkeys(support_paints))
    ordered = list(mandatory) + support_paints + [n for n in requested_paints if n in sources]

    reports = []
    source_details = {}
    for name in ordered:
        src_raw, src_container, src_kind, src_actual = sources[name]
        if name in mandatory and src_actual != name:
            dest_raw, meta = add_logical_driver_resource(
                dest_raw, src_raw, src_actual, name)
        else:
            dest_raw, meta = add_exact_resource(dest_raw, src_raw, name)
        source_details[name] = {
            "container": src_container, "source": src_kind,
            "physical_resource": src_actual,
            "identity_source": meta.get("identity_source"),
            "identity_resolved_to": meta.get("identity_resolved_to"),
            "support_dependency": name in support_paints,
        }
        if meta.get("changed"):
            meta["source_container"] = src_container
            meta["source_kind"] = src_kind
            reports.append(meta)

    parsed = v10.parse_multi_arc(dest_raw)
    for name in mandatory:
        _v10_entry_for_logical(parsed, name, dest_name)
    for name in support_paints + [n for n in requested_paints if n in sources]:
        entry = v10.entry_by_name(parsed, name)
        fields = struct.unpack("<8I", entry.table_record)
        if int(fields[6]) != int(entry.name_ref):
            raise ValueError(f"{name} has an invalid public-name reference")
        actual_identity = _identity_name(parsed, entry)
        wanted_identity = expected_identity.get(name)
        if actual_identity != wanted_identity:
            raise ValueError(
                f"{name} identity changed during transfer: {actual_identity} != {wanted_identity}")
        root_name = _paint_identity_root(parsed, name)
        root_entry = v10.entry_by_name(parsed, root_name)
        if not _is_native_paint_identity(parsed, root_entry):
            raise ValueError(f"{name} exact identity chain does not reach a self-identifying root")

    if created:
        written = _write_new_container(archive, cdf, source_name, dest_name, dest_raw)
    elif dest_raw != v10.read_entry(archive, dest_row):
        written = _write_existing_container(archive, cdf, dest_name, dest_row, dest_raw)
    else:
        written = {"created": False, "container": dest_name,
                   "offset": dest_row.offset, "size": dest_row.size,
                   "readback_verified": True, "changed": False,
                   "cdf_repointed": False}
    return {
        "ok": True, "destination_team_uid": int(destination_team_uid),
        "source_team_uid": int(source_team_uid), "driver_uid": int(driver_uid),
        "container": dest_name, "container_created": bool(created),
        "resources_requested": mandatory + requested_paints,
        "identity_support_resources": support_paints,
        "resources_added": [x["resource"] for x in reports],
        "resources_repaired": [x["resource"] for x in reports if x.get("repaired")],
        "source_details": source_details,
        "source_preference": "live current-team bank, then pristine fallback",
        "missing_optional_resources": missing_optional,
        "resource_count": v10.parse_multi_arc(dest_raw).count,
        "exact_identity_dependencies_verified": True,
        "destination_rebased_from_exact_source_bank": False,
        "full_container_revision": bool(written.get("changed", True)),
        **written,
    }



def _select_expandable_team_base(candidates):
    """Choose the first unique bank accepted by the proven v1 directory writer."""
    seen = set()
    rejected = []
    for row, raw, parsed in candidates:
        key = (row.name.casefold(), _sha(raw))
        if key in seen:
            continue
        seen.add(key)
        try:
            footer_start, _footer, _order = v25._footer_bounds(raw, parsed)
            v25._validate_directory_header(raw, parsed, footer_start)
            return (row, raw, parsed), rejected
        except Exception as ex:
            rejected.append({'container': row.name, 'error': str(ex)})
    return None, rejected


def rebuild_team_bank_clean(game_arg, destination_team_uid: int,
                            driver_specs: Iterable[dict[str, Any]],
                            donor_team_uid: int | None = None,
                            unsafe_live_team_uids: Iterable[int] | None = None) -> dict[str, Any]:
    """Rebuild one complete team bank from a pristine native base.

    Unlike :func:`ensure_driver_assets`, this recovery path never uses the
    currently indexed destination bank as its starting point.  That matters
    after an older experimental writer has damaged an otherwise parseable bank:
    appending another revision of the damaged bytes merely preserves the fatal.

    ``driver_specs`` contains every current member that must be represented:
    ``driver_uid``, ``source_team_uid`` and the stock/native ``livery_uids`` to
    transfer.  App-created thumbnails should be recreated *after* this clean
    base is committed so their saved custom PNGs are encoded onto known-good
    native dependencies.
    """
    game, archive, cdf = game_paths(game_arg)
    destination_team_uid = int(destination_team_uid)
    dest_name = f"2DRIVERSELECTTD_{destination_team_uid}.ARC"
    specs = []
    for raw in driver_specs:
        driver_uid = int(raw['driver_uid'])
        source_team_uid = int(raw.get('source_team_uid', destination_team_uid))
        liveries = [int(x) for x in raw.get('livery_uids', [])]
        specs.append({
            'driver_uid': driver_uid,
            'source_team_uid': source_team_uid,
            'livery_uids': list(dict.fromkeys(liveries)),
        })
    if not specs:
        raise ValueError('clean team-bank rebuild requires at least one current driver')

    live_banks = _all_td(game)
    pristine_banks = _pristine_td(game)
    unsafe_live_names = {
        f"2DRIVERSELECTTD_{int(uid)}.ARC".casefold()
        for uid in (unsafe_live_team_uids or [])
    }
    # Never source recovery material from the bank currently being rebuilt.
    # A parseable destination bank can still contain the exact bad dependency
    # that caused Paint Select to fatal.
    unsafe_live_names.add(dest_name.casefold())
    _, rows = v10.parse_cdf_rows(cdf)
    row_by_name = {r.name.casefold(): r for r in rows}
    dest_row = row_by_name.get(dest_name.casefold())

    base_candidates = []
    base_candidates += _bank_named(pristine_banks, dest_name)
    if donor_team_uid is not None:
        base_candidates += _bank_named(
            pristine_banks, f"2DRIVERSELECTTD_{int(donor_team_uid)}.ARC")
    for spec in specs:
        base_candidates += _bank_named(
            pristine_banks, f"2DRIVERSELECTTD_{spec['source_team_uid']}.ARC")
    if donor_team_uid is not None:
        base_candidates += _bank_named(
            live_banks, f"2DRIVERSELECTTD_{int(donor_team_uid)}.ARC")
    for spec in specs:
        base_candidates += _bank_named(
            live_banks, f"2DRIVERSELECTTD_{spec['source_team_uid']}.ARC")
    # Last resort is a valid live destination.  It is intentionally last and is
    # reported so the caller knows the repair lacked a pristine reconstruction.
    base_candidates += _bank_named(live_banks, dest_name)
    # A native reserve bank may parse correctly but use an order-directory
    # dialect that cannot be expanded by the proven v1 append/repoint writer.
    # The old public transfer path worked by starting from an authored,
    # expandable bank and writing a complete destination revision. Preserve the
    # preference order above, then fall back to any trusted pristine authored
    # bank rather than rejecting the move or overwriting existing members.
    base_candidates += pristine_banks
    if not base_candidates:
        raise ValueError(f'no native base container is available for {dest_name}')
    selected, rejected_bases = _select_expandable_team_base(base_candidates)
    if selected is None:
        raise ValueError(
            'no expandable native base container is available for a complete team rebuild; ' +
            '; '.join(f"{x['container']}: {x['error']}" for x in rejected_bases[:4]))
    base_row, base_raw, base_parsed = selected
    dest_raw = bytes(base_raw)
    base_is_pristine = any(
        base_row.name.casefold() == row.name.casefold() and base_raw == raw
        for row, raw, _parsed in pristine_banks)

    def source_label(row, raw) -> str:
        return 'pristine' if any(
            row.name.casefold() == prow.name.casefold() and raw == praw
            for prow, praw, _ in pristine_banks) else 'validated_live'

    def locate(name: str, preferred_team_uid: int, family: str):
        preferred_name = f"2DRIVERSELECTTD_{int(preferred_team_uid)}.ARC"
        candidates = (
            _bank_named(pristine_banks, preferred_name)
            + _bank_named(live_banks, preferred_name)
            + pristine_banks + live_banks
        )
        seen = set()
        for row, raw, parsed in candidates:
            key = (row.name.casefold(), _sha(raw))
            if key in seen:
                continue
            seen.add(key)
            pristine = source_label(row, raw) == 'pristine'
            if not pristine and row.name.casefold() in unsafe_live_names:
                continue
            try:
                if family == 'driver':
                    entry = _v10_entry_for_logical(parsed, name, row.name)
                    ok, _reason = _driver_art_entry_valid(raw, parsed, name, row.name)
                    if not ok:
                        continue
                else:
                    entry = v10.entry_by_name(parsed, name)
                if family == 'paint':
                    identity = _identity_name(parsed, entry)
                    if identity is None or not identity.startswith('PAINTSCHEME_'):
                        continue
                    root_name = _paint_identity_root(parsed, name)
                    root = v10.entry_by_name(parsed, root_name)
                    if not _is_native_paint_identity(parsed, root):
                        continue
                return row, raw, parsed, entry.name
            except Exception:
                continue
        return None

    requested = []
    source_details = {}
    missing_optional = []
    expected_identity: dict[str, str] = {}
    support_order = []
    resources: dict[str, tuple[bytes, str, str, str]] = {}

    for spec in specs:
        driver_uid = spec['driver_uid']
        source_uid = spec['source_team_uid']
        mandatory = [
            f"DRIVERPAINT_{driver_uid}_25041",
            f"DRIVER_{driver_uid}_3DNUM_25041",
        ]
        paints = [f"PAINTSCHEME_{uid}" for uid in spec['livery_uids']]
        for name in mandatory:
            hit = locate(name, source_uid, 'driver')
            if hit is None:
                raise ValueError(f'required safe source resource is missing: {name}')
            row, raw, _parsed, actual = hit
            resources[name] = (raw, row.name, source_label(row, raw), actual)
            if name not in requested:
                requested.append(name)
        for name in paints:
            hit = locate(name, source_uid, 'paint')
            if hit is None:
                missing_optional.append(name)
                continue
            row, raw, parsed, actual = hit
            entry = v10.entry_by_name(parsed, actual)
            identity = _identity_name(parsed, entry)
            if identity is None:
                # Broken alias chain in this source bank. Nothing has been added
                # to `resources` or `requested` yet, so skipping is clean.
                missing_optional.append(name)
                continue
            expected_identity[name] = identity
            for dep in _paint_identity_chain(raw, name):
                dep_entry = v10.entry_by_name(parsed, dep)
                expected_identity[dep] = _identity_name(parsed, dep_entry) or dep
                resources.setdefault(dep, (raw, row.name, source_label(row, raw), dep))
                if dep not in support_order:
                    support_order.append(dep)
            resources[name] = (raw, row.name, source_label(row, raw), actual)
            if name not in requested:
                requested.append(name)

    # Root dependencies first, then driver art and aliases.  Existing native
    # resources in the pristine base remain untouched unless their structure or
    # identity differs from the exact source.
    ordered = support_order + requested
    reports = []
    for name in ordered:
        src_raw, src_container, src_kind, src_actual = resources[name]
        if (name.startswith('DRIVERPAINT_') or
                (name.startswith('DRIVER_') and '_3DNUM_' in name)) and src_actual != name:
            dest_raw, meta = add_logical_driver_resource(
                dest_raw, src_raw, src_actual, name)
        else:
            dest_raw, meta = add_exact_resource(dest_raw, src_raw, name)
        source_details[name] = {
            'container': src_container,
            'source': src_kind,
            'physical_resource': src_actual,
            'validated': True,
            'support_dependency': name in support_order,
            'identity_source': meta.get('identity_source'),
            'identity_resolved_to': meta.get('identity_resolved_to'),
        }
        if meta.get('changed'):
            meta['source_container'] = src_container
            meta['source_kind'] = src_kind
            reports.append(meta)

    parsed = v10.parse_multi_arc(dest_raw)
    names = [e.name for e in parsed.entries]
    if len(names) != len(set(names)):
        raise ValueError('clean team-bank rebuild produced duplicate resource names')
    for spec in specs:
        driver_uid = spec['driver_uid']
        for name in (f"DRIVERPAINT_{driver_uid}_25041",
                     f"DRIVER_{driver_uid}_3DNUM_25041"):
            ok, reason = _driver_art_entry_valid(dest_raw, parsed, name, dest_name)
            if not ok:
                raise ValueError(f'{name} failed native validation: {reason}')
    for name, wanted in expected_identity.items():
        entry = v10.entry_by_name(parsed, name)
        actual = _identity_name(parsed, entry)
        if actual != wanted:
            raise ValueError(f'{name} identity changed during clean rebuild: {actual} != {wanted}')
        root_name = _paint_identity_root(parsed, name)
        root = v10.entry_by_name(parsed, root_name)
        if not _is_native_paint_identity(parsed, root):
            raise ValueError(f'{name} does not reach a self-identifying native root')

    if dest_row is None:
        written = _write_new_container(
            archive, cdf, base_row.name, dest_name, dest_raw)
    else:
        written = _write_existing_container(
            archive, cdf, dest_name, dest_row, dest_raw)
    check_row = v10.find_row(v10.parse_cdf_rows(cdf)[1], dest_name)
    check_raw = v10.read_entry(archive, check_row)
    if check_raw != dest_raw:
        raise ValueError('clean team-bank indexed readback mismatch')
    v10.parse_multi_arc(check_raw)

    return {
        'ok': True,
        'destination_team_uid': destination_team_uid,
        'container': dest_name,
        'base_container': base_row.name,
        'base_source': 'pristine' if base_is_pristine else 'live_fallback',
        'base_rejected_nonexpandable': rejected_bases,
        'drivers': [x['driver_uid'] for x in specs],
        'resources_requested': ordered,
        'resources_added': [x['resource'] for x in reports],
        'resources_repaired': [x['resource'] for x in reports if x.get('repaired')],
        'missing_optional_resources': list(dict.fromkeys(missing_optional)),
        'source_details': source_details,
        'resource_count': parsed.count,
        'clean_base_rebuild': True,
        'exact_identity_dependencies_verified': True,
        **written,
    }

def _logical_resource_names(parsed: v10.MultiArc, container_name: str) -> list[str]:
    names = [entry.name for entry in parsed.entries]
    present = set(names)
    for logical, rows in _packaged_driver_aliases().items():
        for container, physical in rows:
            if (container.casefold() == str(container_name).casefold() and
                    physical in present and logical not in present):
                names.append(logical)
                break
    return names


def pristine_team_container_resource_names(game_arg, team_uid: int) -> list[str]:
    """Return resource names from the oldest trusted backup of one team bank.

    This is the authoritative Paint Select inventory for stock resources. A
    LIVERIE_c record can be valid without owning a PAINTSCHEME_<UID> resource;
    only names actually present here should be required during a moved/custom
    team rebuild.
    """
    game = Path(game_arg)
    wanted = f"2DRIVERSELECTTD_{int(team_uid)}.ARC".casefold()
    for row, _raw, parsed in _pristine_td(game):
        if row.name.casefold() == wanted:
            return _logical_resource_names(parsed, row.name)
    return []


def team_container_resource_names(game_arg, team_uid: int) -> list[str]:
    game, archive, cdf = game_paths(game_arg)
    name = f"2DRIVERSELECTTD_{int(team_uid)}.ARC"
    _, rows = v10.parse_cdf_rows(cdf)
    try:
        row = v10.find_row(rows, name)
    except Exception:
        return []
    try:
        parsed = v10.parse_multi_arc(v10.read_entry(archive, row))
        return _logical_resource_names(parsed, row.name)
    except Exception:
        return []


def resource_locations(game_arg, prefix: str | None = None) -> dict[str, list[str]]:
    game = Path(game_arg)
    out: dict[str, list[str]] = {}
    for row, _raw, parsed in _all_td(game):
        for name in _logical_resource_names(parsed, row.name):
            if prefix and not name.startswith(prefix):
                continue
            out.setdefault(name, []).append(row.name)
    return out


def _driver_art_entry_valid(raw: bytes, parsed: v10.MultiArc,
                            resource_name: str, container_name: str | None = None) -> tuple[bool, str | None]:
    try:
        entry = _canonical_entry(raw, resource_name, container_name)
        if str(entry["fmt"]) != 'DXT5' or int(entry["w"]) <= 0 or int(entry["h"]) <= 0:
            return False, f'unexpected {entry["w"]}x{entry["h"]} {entry["fmt"]}'
        if int(entry["payload_size"]) < int(entry["needed"]):
            return False, f'payload is {entry["payload_size"]} bytes; expected {entry["needed"]}'
        C.multi_read_png(raw, entry)
        return True, None
    except Exception as ex:
        return False, str(ex)


def driver_art_location_map(game_arg) -> dict[int, list[str]]:
    """Return banks containing two structurally complete native art resources."""
    partial: dict[int, dict[str, set[str]]] = {}
    for row, raw, parsed in _all_td(Path(game_arg)):
        candidate_uids = set()
        for name in _logical_resource_names(parsed, row.name):
            if name.startswith("DRIVERPAINT_") and name.endswith("_25041"):
                middle = name[len("DRIVERPAINT_"):-len("_25041")]
                if middle.isdigit(): candidate_uids.add(int(middle))
            elif name.startswith("DRIVER_") and name.endswith("_3DNUM_25041"):
                middle = name[len("DRIVER_"):-len("_3DNUM_25041")]
                if middle.isdigit(): candidate_uids.add(int(middle))
        for uid in candidate_uids:
            tile = f"DRIVERPAINT_{uid}_25041"
            number = f"DRIVER_{uid}_3DNUM_25041"
            tile_ok, _ = _driver_art_entry_valid(raw, parsed, tile, row.name)
            num_ok, _ = _driver_art_entry_valid(raw, parsed, number, row.name)
            kinds = set()
            if tile_ok: kinds.add('tile')
            if num_ok: kinds.add('number')
            if kinds:
                partial.setdefault(uid, {})[row.name] = kinds
    out: dict[int, list[str]] = {}
    for uid, banks in partial.items():
        complete = sorted(name for name, kinds in banks.items()
                          if {"tile", "number"} <= kinds)
        if complete:
            out[int(uid)] = complete
    return out


def resolve_driver_art_container(game_arg, preferred_team_uid: int,
                                 driver_uid: int) -> dict[str, Any]:
    """Resolve the live bank that actually owns both driver-art resources.

    DRIVERCONFIG team links and legacy menu-bank ownership are not guaranteed
    to stay numerically identical after transfers/repairs.  Prefer the current
    team bank when it contains both resources, then fall back to the unique
    live bank that does.  This also makes the editor resilient to old state
    files and renamed/repurposed roster records.
    """
    wanted = {
        f"DRIVERPAINT_{int(driver_uid)}_25041",
        f"DRIVER_{int(driver_uid)}_3DNUM_25041",
    }
    banks = _all_td(Path(game_arg))
    preferred_name = f"2DRIVERSELECTTD_{int(preferred_team_uid)}.ARC"
    candidates = []
    for row, raw, parsed in banks:
        logical_names = set(_logical_resource_names(parsed, row.name))
        for entry in parsed.entries:
            try:
                identity = _identity_name(parsed, entry)
                if identity:
                    logical_names.add(identity)
            except Exception:
                pass
        present = set()
        for name in wanted & logical_names:
            ok, _error = _driver_art_entry_valid(raw, parsed, name, row.name)
            if ok:
                present.add(name)
        if present:
            candidates.append((row, parsed, logical_names, present))
    preferred = next((x for x in candidates
                      if x[0].name.casefold() == preferred_name.casefold()
                      and wanted <= x[3]), None)
    chosen = preferred
    if chosen is None:
        complete = [x for x in candidates if wanted <= x[3]]
        if len(complete) == 1:
            chosen = complete[0]
        elif complete:
            # Prefer a stock/pristine-looking numeric team bank over a partial
            # or duplicate legacy copy, but report ambiguity to the caller.
            complete.sort(key=lambda x: (x[0].name.casefold() != preferred_name.casefold(),
                                         x[0].name.casefold()))
            chosen = complete[0]
    if chosen is None:
        locations = {name: [] for name in sorted(wanted)}
        for row, _parsed, _names, present in candidates:
            for name in present:
                locations[name].append(row.name)
        raise ValueError("driver art resources are not together in a live team bank: " +
                         "; ".join(f"{k} -> {v or ['missing']}" for k, v in locations.items()))
    row, parsed, _names, _present = chosen
    suffix = row.name[len("2DRIVERSELECTTD_"):-4] if row.name.upper().startswith("2DRIVERSELECTTD_") and row.name.upper().endswith(".ARC") else ""
    resolved_uid = int(suffix) if suffix.isdigit() else int(preferred_team_uid)
    return {
        "team_uid": resolved_uid,
        "container": row.name,
        "preferred_team_uid": int(preferred_team_uid),
        "used_fallback": row.name.casefold() != preferred_name.casefold(),
        "resource_count": int(parsed.count),
    }


def driver_art_resource_name(driver_uid: int, kind: str) -> str:
    kind = str(kind or '').strip().lower()
    if kind in ('tile', 'paint', 'driverpaint'):
        return f"DRIVERPAINT_{int(driver_uid)}_25041"
    if kind in ('number', '3dnum', 'card'):
        return f"DRIVER_{int(driver_uid)}_3DNUM_25041"
    raise ValueError("driver art kind must be tile or number")


def driver_art_spec(game_arg, team_uid: int, driver_uid: int, kind: str) -> dict[str, Any]:
    game, archive, cdf = game_paths(game_arg)
    container = f"2DRIVERSELECTTD_{int(team_uid)}.ARC"
    _, rows = v10.parse_cdf_rows(cdf)
    row = v10.find_row(rows, container)
    raw = v10.read_entry(archive, row)
    name = driver_art_resource_name(driver_uid, kind)
    entry = _canonical_entry(raw, name, container)
    return {"container": container, "entry": name, "physical_entry": entry.get("physical_name"), "width": int(entry["w"]),
            "height": int(entry["h"]), "format": str(entry["fmt"])}


def read_driver_art_image(game_arg, team_uid: int, driver_uid: int, kind: str):
    game, archive, cdf = game_paths(game_arg)
    container = f"2DRIVERSELECTTD_{int(team_uid)}.ARC"
    name = driver_art_resource_name(driver_uid, kind)
    _, rows = v10.parse_cdf_rows(cdf)
    row = v10.find_row(rows, container)
    raw = v10.read_entry(archive, row)
    entry = _canonical_entry(raw, name, container)
    if entry["fmt"] != 'DXT5':
        raise ValueError(f'unsupported driver art format: {entry["fmt"]}; expected DXT5')
    if int(entry["payload_size"]) < int(entry["needed"]):
        raise ValueError(
            f'{name} is not a complete native driver-art resource: '
            f'payload {entry["payload_size"]}, expected {entry["needed"]}'
        )
    return C.multi_read_png(raw, entry)


def replace_driver_art(game_arg, team_uid: int, driver_uid: int,
                       kind: str, image_path) -> dict[str, Any]:
    kind = str(kind or '').strip().lower()
    game, archive, cdf = game_paths(game_arg)
    container = f"2DRIVERSELECTTD_{int(team_uid)}.ARC"
    name = driver_art_resource_name(driver_uid, kind)
    _, rows = v10.parse_cdf_rows(cdf)
    row = v10.find_row(rows, container)
    original = v10.read_entry(archive, row)
    _v10_entry_for_logical(v10.parse_multi_arc(original), name)
    rebuilt, encoder, layout = _replace_driver_art_image(
        original, name, Path(image_path))
    written = _write_existing_container(archive, cdf, container, row, rebuilt)
    return {"ok": True, "changed": rebuilt != original, "entry": name,
            "encoder": encoder, "layout": layout, **written}


def repair_driver_art(game_arg, destination_team_uid: int, source_team_uid: int,
                      driver_uid: int) -> dict[str, Any]:
    """Restore the two driver-select art resources from canonical stock data."""
    return ensure_driver_assets(game_arg, destination_team_uid, source_team_uid,
                                driver_uid, [])

def team_asset_status(game_arg, team_uid: int) -> dict[str, Any]:
    game = Path(game_arg)
    _g0, archive0, cdf0 = archive_paths(game, 0)
    _g1, archive1, cdf1 = archive_paths(game, 1)
    _, rows0 = v10.parse_cdf_rows(cdf0)
    _, rows1 = v10.parse_cdf_rows(cdf1)
    row0 = {r.name.casefold(): r for r in rows0}
    row1 = {r.name.casefold(): r for r in rows1}
    td_name = f"2DRIVERSELECTTD_{int(team_uid)}.ARC"
    menu_row = row0.get(MENU_CONTAINER.casefold())
    logo = False
    if menu_row:
        try:
            menu = v10.parse_multi_arc(v10.read_entry(archive0, menu_row))
            logo = bool(_team_logo_name(menu.entries, int(team_uid)))
        except Exception:
            pass
    td = row1.get(td_name.casefold())
    count = 0
    if td:
        try:
            count = v10.parse_multi_arc(v10.read_entry(archive1, td)).count
        except Exception:
            pass
    return {"team_uid": int(team_uid), "logo_ready": logo,
            "paint_container_ready": bool(td), "paint_resource_count": count,
            "presentation_ready": bool(logo and td)}


def team_asset_statuses(game_arg, team_uids: Iterable[int]) -> dict[int, dict[str, Any]]:
    game = Path(game_arg)
    _g0, archive0, cdf0 = archive_paths(game, 0)
    _g1, archive1, cdf1 = archive_paths(game, 1)
    _, rows0 = v10.parse_cdf_rows(cdf0)
    _, rows1 = v10.parse_cdf_rows(cdf1)
    row0 = {r.name.casefold(): r for r in rows0}
    row1 = {r.name.casefold(): r for r in rows1}
    logo_names: set[str] = set()
    menu_entries = []
    menu_row = row0.get(MENU_CONTAINER.casefold())
    if menu_row:
        try:
            menu = v10.parse_multi_arc(v10.read_entry(archive0, menu_row))
            menu_entries = list(menu.entries)
            logo_names = {e.name for e in menu_entries}
        except Exception:
            pass
    out: dict[int, dict[str, Any]] = {}
    for value in team_uids:
        uid = int(value)
        td_name = f"2DRIVERSELECTTD_{uid}.ARC"
        row = row1.get(td_name.casefold())
        count = 0
        if row:
            try:
                count = v10.parse_multi_arc(v10.read_entry(archive1, row)).count
            except Exception:
                pass
        logo = bool(_team_logo_name(menu_entries, uid))
        out[uid] = {"team_uid": uid, "logo_ready": logo,
                    "paint_container_ready": bool(row),
                    "paint_resource_count": count,
                    "presentation_ready": bool(logo and row)}
    return out

