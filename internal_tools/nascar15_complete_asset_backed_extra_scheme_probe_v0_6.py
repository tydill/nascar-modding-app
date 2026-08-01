#!/usr/bin/env python3
"""NASCAR 15 Complete Asset-Backed Extra Scheme Probe v0.6.

Creates one genuinely new LIVERIE_c record and the matching independent SD/HD
archive/index entries. This is the first controlled test that combines:

- a free in-range livery UID (25582 by default),
- a unique ScriptName,
- a new LIVERY_<ScriptName>.ARC index entry,
- a new HDLIVERY_<ScriptName>.ARC index entry,
- byte-identical donor SD/HD payloads copied to new aligned ARCHIVE2 offsets.

No preview entry is added. A blank carousel thumbnail is expected.

The probe is append-only for ARCHIVE0.AR and ARCHIVE2.AR. Restore truncates only
bytes appended by this probe and restores the two small cdfiles indexes exactly.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V05_PATH = HERE / "nascar15_unique_scriptname_extra_scheme_probe_v0_5.py"
if not V05_PATH.exists():
    raise FileNotFoundError(f"Missing dependency: {V05_PATH.name}")
spec = importlib.util.spec_from_file_location("n15_v05", str(V05_PATH))
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load v0.5 slot builder")
v05 = importlib.util.module_from_spec(spec)
sys.modules["n15_v05"] = v05
spec.loader.exec_module(v05)
base = v05.base

VERSION = "0.6"
DEFAULT_NEW_SCRIPT = "15_47_AJ_EXTRA_SLOT_TEST"
DEFAULT_DONOR_SCRIPT = "15_2_BRAD_KESELOWSKI_BEER_1"
DEFAULT_NEW_UID = 25582
DEFAULT_DONOR_UID = 25580
DEFAULT_ORIGINAL_DRIVER_UID = 1115
DEFAULT_RECIPIENT_DRIVER_UID = 1083

MANIFEST_NAME = "complete_asset_extra_scheme_v0_6_manifest.json"
ANALYSIS_NAME = "complete_asset_extra_scheme_v0_6_analysis.json"
CDF_BACKUP_SUFFIX = ".complete_asset_extra_scheme_probe_v0_6.bak"
ARCHIVE2_ALIGNMENT = 8
ARCHIVE0_ALIGNMENT = 16


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
        return struct.pack(
            "<7I4B",
            self.null0,
            self.folder_name_offset,
            self.file_name_offset,
            self.data_size,
            self.uncompressed_size,
            self.null1,
            self.data_offset,
            self.archive_index,
            self.entry_type,
            self.null2,
            self.unk4,
        )


@dataclasses.dataclass(frozen=True)
class V6TreeNode:
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
    def unpack(cls, raw: bytes, offset: int) -> "V6TreeNode":
        values = struct.unpack_from("<i7iII", raw, offset)
        return cls(*values)

    def pack(self) -> bytes:
        return struct.pack(
            "<i7iII",
            self.parent_node,
            self.unk0,
            self.unk1,
            self.unk2,
            _s32(self.name_hash),
            _s32(self.folded_hash),
            self.unk6,
            _s32(self.flags),
            self.tail_name_offset,
            self.file_index,
        )


@dataclasses.dataclass
class CDFV6:
    raw: bytes
    unknown: tuple[int, int, int, int, int]
    archives: list[tuple[int, int]]
    files: list[V6File]
    trees: list[V6TreeNode]
    strings: bytes

    def cstr(self, offset: int) -> str:
        if offset < 0 or offset >= len(self.strings):
            raise ValueError(f"String offset {offset} is outside the V6 string buffer")
        end = self.strings.find(b"\0", offset)
        if end < 0:
            raise ValueError(f"Unterminated V6 string at offset {offset}")
        return self.strings[offset:end].decode("ascii", "strict")

    def full_name(self, file: V6File) -> str:
        return self.cstr(file.folder_name_offset) + self.cstr(file.file_name_offset)

    def basename(self, file: V6File) -> str:
        return self.cstr(file.file_name_offset)

    def archive_name(self, file: V6File) -> str:
        if file.archive_index >= len(self.archives):
            raise ValueError("File archive index is outside the archive table")
        return self.cstr(self.archives[file.archive_index][0])



def _s32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) & ~(boundary - 1)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_region(path: Path, offset: int, size: int) -> str:
    h = hashlib.sha256()
    remaining = size
    with path.open("rb") as f:
        f.seek(offset)
        while remaining:
            block = f.read(min(8 * 1024 * 1024, remaining))
            if not block:
                raise ValueError(f"Short read from {path.name} at 0x{offset:X}")
            h.update(block)
            remaining -= len(block)
    return h.hexdigest()


def fnv1(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h = ((h * 0x01000193) & 0xFFFFFFFF) ^ b
    return h


def fnv1_mask_df(data: bytes) -> int:
    """Second native CDF V6 name hash: FNV-1 with each byte AND 0xDF."""
    h = 0x811C9DC5
    for b in data:
        h = ((h * 0x01000193) & 0xFFFFFFFF) ^ (b & 0xDF)
    return h


def parse_cdf_v6(raw: bytes) -> CDFV6:
    if len(raw) < 44:
        raise ValueError("cdfiles2.dat is too short")
    magic, version = struct.unpack_from("<II", raw, 0)
    if magic != 0x436C6966 or version != 6:
        raise ValueError("Expected little-endian filC version 6 index")
    values = struct.unpack_from("<9I", raw, 8)
    unknown = tuple(values[:5])
    num_archives, num_files, num_trees, string_size = values[5:]
    if not (1 <= num_archives <= 64):
        raise ValueError(f"Implausible archive count {num_archives}")
    pos = 44
    need = pos + num_archives * 8 + num_files * 32 + num_trees * 40 + string_size
    if need != len(raw):
        raise ValueError(f"V6 section sizes resolve to {need} bytes, file is {len(raw)} bytes")
    archives = []
    for _ in range(num_archives):
        archives.append(struct.unpack_from("<II", raw, pos))
        pos += 8
    files = []
    for _ in range(num_files):
        files.append(V6File.unpack(raw, pos))
        pos += 32
    trees = []
    for _ in range(num_trees):
        trees.append(V6TreeNode.unpack(raw, pos))
        pos += 40
    strings = raw[pos:pos + string_size]
    out = CDFV6(raw, unknown, archives, files, trees, strings)

    # Structural validation used by both Analyze and Apply.
    for archive_offset, _ in out.archives:
        out.cstr(archive_offset)
    for i, f in enumerate(out.files):
        out.full_name(f)
        # V6 indexes can contain non-stream pseudo records (for example the
        # archive-path record) whose archiveIndex is not an index into the
        # stream table. Only streamed entries resolve through archives[].
        if f.entry_type in (4, 5) and f.archive_index >= len(out.archives):
            raise ValueError(f"Streamed file record {i} has invalid archive index {f.archive_index}")
    real_file_indexes = [n.file_index for n in out.trees if n.file_index != 0xFFFFFFFF]
    if sorted(real_file_indexes) != list(range(len(out.files))):
        raise ValueError("V6 tree does not contain exactly one node for every file record")
    return out


def serialize_cdf_v6(cdf: CDFV6) -> bytes:
    out = bytearray()
    out += struct.pack("<II", 0x436C6966, 6)
    out += struct.pack(
        "<9I",
        *cdf.unknown,
        len(cdf.archives),
        len(cdf.files),
        len(cdf.trees),
        len(cdf.strings),
    )
    for item in cdf.archives:
        out += struct.pack("<II", *item)
    for item in cdf.files:
        out += item.pack()
    for item in cdf.trees:
        out += item.pack()
    out += cdf.strings
    return bytes(out)


def find_v6_file(cdf: CDFV6, basename: str) -> tuple[int, V6File]:
    key = basename.casefold()
    hits = [(i, f) for i, f in enumerate(cdf.files) if cdf.basename(f).casefold() == key]
    if len(hits) != 1:
        raise ValueError(f"Expected one {basename} entry in cdfiles2.dat; found {len(hits)}")
    return hits[0]


def find_tree_for_file(cdf: CDFV6, file_index: int) -> tuple[int, V6TreeNode]:
    hits = [(i, n) for i, n in enumerate(cdf.trees) if n.file_index == file_index]
    if len(hits) != 1:
        raise ValueError(f"Expected one tree node for file index {file_index}; found {len(hits)}")
    return hits[0]


def clone_asset_entries(
    cdf: CDFV6,
    donor_sd_name: str,
    donor_hd_name: str,
    new_sd_name: str,
    new_hd_name: str,
    new_sd_offset: int,
    new_hd_offset: int,
) -> tuple[bytes, dict[str, Any]]:
    existing = {cdf.basename(f).casefold() for f in cdf.files}
    for name in (new_sd_name, new_hd_name):
        if name.casefold() in existing:
            raise ValueError(f"New asset entry already exists: {name}")
        name.encode("ascii", "strict")

    donor_sd_index, donor_sd = find_v6_file(cdf, donor_sd_name)
    donor_hd_index, donor_hd = find_v6_file(cdf, donor_hd_name)
    sd_tree_index, sd_tree = find_tree_for_file(cdf, donor_sd_index)
    hd_tree_index, hd_tree = find_tree_for_file(cdf, donor_hd_index)

    if donor_sd.entry_type not in (4, 5) or donor_hd.entry_type not in (4, 5):
        raise ValueError("Donor paint entries are not streamed archive files")
    if cdf.archive_name(donor_sd).casefold() != "archive2.ar" or cdf.archive_name(donor_hd).casefold() != "archive2.ar":
        raise ValueError("Donor paint assets do not resolve to ARCHIVE2.AR")
    if sd_tree.parent_node != hd_tree.parent_node:
        raise ValueError("SD and HD donor assets are not in the same CDF folder node")
    for tree, name in ((sd_tree, donor_sd_name), (hd_tree, donor_hd_name)):
        if cdf.cstr(tree.tail_name_offset).casefold() != name.casefold():
            raise ValueError("Donor tree tail string does not match its file record")
        if _u32(tree.name_hash) != fnv1(name.encode("ascii")):
            raise ValueError(f"Unexpected native name hash for {name}")
        if _u32(tree.folded_hash) != fnv1_mask_df(name.encode("ascii")):
            raise ValueError(f"Unexpected native folded hash for {name}")

    strings = bytearray(cdf.strings)
    new_name_offsets = {}
    for name in (new_sd_name, new_hd_name):
        new_name_offsets[name] = len(strings)
        strings += name.encode("ascii") + b"\0"

    new_files = list(cdf.files)
    sd_new_index = len(new_files)
    new_files.append(dataclasses.replace(donor_sd, file_name_offset=new_name_offsets[new_sd_name], data_offset=new_sd_offset))
    hd_new_index = len(new_files)
    new_files.append(dataclasses.replace(donor_hd, file_name_offset=new_name_offsets[new_hd_name], data_offset=new_hd_offset))

    def new_tree(source: V6TreeNode, name: str, file_index: int) -> V6TreeNode:
        return dataclasses.replace(
            source,
            name_hash=_s32(fnv1(name.encode("ascii"))),
            folded_hash=_s32(fnv1_mask_df(name.encode("ascii"))),
            tail_name_offset=new_name_offsets[name],
            file_index=file_index,
        )

    new_trees = list(cdf.trees)
    new_trees.append(new_tree(sd_tree, new_sd_name, sd_new_index))
    new_trees.append(new_tree(hd_tree, new_hd_name, hd_new_index))
    rebuilt_obj = CDFV6(b"", cdf.unknown, list(cdf.archives), new_files, new_trees, bytes(strings))
    rebuilt = serialize_cdf_v6(rebuilt_obj)
    check = parse_cdf_v6(rebuilt)

    # Exact preservation check for every old record and node.
    if check.archives != cdf.archives or check.files[:len(cdf.files)] != cdf.files or check.trees[:len(cdf.trees)] != cdf.trees:
        raise ValueError("Rebuilt cdfiles2 changed an existing archive, file, or tree record")
    if check.strings[:len(cdf.strings)] != cdf.strings:
        raise ValueError("Rebuilt cdfiles2 changed the existing string buffer")
    for name, expected_offset, donor in (
        (new_sd_name, new_sd_offset, donor_sd),
        (new_hd_name, new_hd_offset, donor_hd),
    ):
        _, item = find_v6_file(check, name)
        if item.data_offset != expected_offset or item.data_size != donor.data_size:
            raise ValueError(f"Rebuilt cdfiles2 did not preserve the planned mapping for {name}")

    meta = {
        "cdf_version": 6,
        "before_file_count": len(cdf.files),
        "after_file_count": len(check.files),
        "before_tree_count": len(cdf.trees),
        "after_tree_count": len(check.trees),
        "before_string_size": len(cdf.strings),
        "after_string_size": len(check.strings),
        "donor_sd": _file_meta(cdf, donor_sd_index, donor_sd, sd_tree_index, sd_tree),
        "donor_hd": _file_meta(cdf, donor_hd_index, donor_hd, hd_tree_index, hd_tree),
        "new_sd": _file_meta(check, sd_new_index, check.files[sd_new_index], len(cdf.trees), check.trees[len(cdf.trees)]),
        "new_hd": _file_meta(check, hd_new_index, check.files[hd_new_index], len(cdf.trees) + 1, check.trees[len(cdf.trees) + 1]),
        "cdf_sha256_before": sha256_bytes(cdf.raw),
        "cdf_sha256_after": sha256_bytes(rebuilt),
    }
    return rebuilt, meta


def _file_meta(cdf: CDFV6, index: int, file: V6File, tree_index: int, tree: V6TreeNode) -> dict[str, Any]:
    return {
        "file_index": index,
        "tree_index": tree_index,
        "full_name": cdf.full_name(file),
        "basename": cdf.basename(file),
        "archive": cdf.archive_name(file),
        "data_offset": file.data_offset,
        "data_size": file.data_size,
        "entry_type": file.entry_type,
        "folder_name_offset": file.folder_name_offset,
        "file_name_offset": file.file_name_offset,
        "parent_tree_node": tree.parent_node,
        "name_hash": f"0x{_u32(tree.name_hash):08X}",
        "folded_hash": f"0x{_u32(tree.folded_hash):08X}",
        "tree_flags": f"0x{_u32(tree.flags):08X}",
    }


def archive2_paths(game: Path) -> tuple[Path, Path]:
    data = game / "data"
    arc = data / "ARCHIVE2.AR"
    cdf = data / "cdfiles2.dat"
    if not arc.exists() or not cdf.exists():
        raise FileNotFoundError("ARCHIVE2.AR/cdfiles2.dat was not found")
    return arc, cdf


def patch_cdf0_bytes(cdf_path: Path, row: Any, new_offset: int, new_size: int) -> bytes:
    raw = bytearray(cdf_path.read_bytes())
    if row.layout == "A":
        struct.pack_into("<I", raw, row.record_pos + 8, new_size)
        struct.pack_into("<I", raw, row.record_pos + 20, new_offset)
    else:
        struct.pack_into("<I", raw, row.record_pos + 16, new_size)
        struct.pack_into("<I", raw, row.record_pos + 28, new_offset)
    return bytes(raw)


def atomic_write(path: Path, data: bytes) -> None:
    temp = Path(str(path) + ".v06.tmp")
    with temp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def ensure_small_backup(path: Path) -> Path:
    backup = Path(str(path) + CDF_BACKUP_SUFFIX)
    if backup.exists():
        if backup.read_bytes() != path.read_bytes():
            raise ValueError(
                f"Existing {backup.name} does not match the current index. "
                "Restore/remove the older v0.6 test before applying again."
            )
        return backup
    temp = Path(str(backup) + ".tmp")
    shutil.copyfile(path, temp)
    if temp.read_bytes() != path.read_bytes():
        temp.unlink(missing_ok=True)
        raise ValueError(f"Backup verification failed for {path.name}")
    os.replace(temp, backup)
    return backup


def read_region(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(size)
    if len(data) != size:
        raise ValueError(f"Short read from {path.name}: wanted {size}, got {len(data)}")
    return data


def append_payload(path: Path, planned_offset: int, payload: bytes, alignment: int) -> None:
    with path.open("ab") as f:
        actual = f.tell()
        expected = align(actual, alignment)
        if expected != planned_offset:
            raise ValueError(f"{path.name} append offset changed: planned 0x{planned_offset:X}, live 0x{expected:X}")
        if expected > actual:
            f.write(b"\0" * (expected - actual))
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def truncate_exact(path: Path, size: int) -> None:
    with path.open("r+b") as f:
        f.truncate(size)
        f.flush()
        os.fsync(f.fileno())


def build_plan(args: argparse.Namespace) -> tuple[dict[str, Any], bytes, bytes, bytes, bytes]:
    v05.NEW_SCRIPT_NAME = args.new_script_name
    ctx = base.load_context(args.game)
    pyc_plan, rebuilt_pyc = base.make_plan(ctx, args)
    archive2, cdfiles2 = archive2_paths(ctx.game)
    cdf2_raw = cdfiles2.read_bytes()
    cdf2 = parse_cdf_v6(cdf2_raw)

    donor_sd_name = f"LIVERY_{args.donor_script_name}.ARC"
    donor_hd_name = f"HDLIVERY_{args.donor_script_name}.ARC"
    new_sd_name = f"LIVERY_{args.new_script_name}.ARC"
    new_hd_name = f"HDLIVERY_{args.new_script_name}.ARC"
    _, donor_sd = find_v6_file(cdf2, donor_sd_name)
    _, donor_hd = find_v6_file(cdf2, donor_hd_name)
    archive2_size = archive2.stat().st_size
    sd_offset = align(archive2_size, ARCHIVE2_ALIGNMENT)
    hd_offset = align(sd_offset + donor_sd.data_size, ARCHIVE2_ALIGNMENT)
    final_archive2_size = hd_offset + donor_hd.data_size
    if final_archive2_size >= 2**32:
        raise ValueError("ARCHIVE2 append would exceed the 32-bit CDF V6 offset range")
    rebuilt_cdf2, asset_meta = clone_asset_entries(
        cdf2,
        donor_sd_name,
        donor_hd_name,
        new_sd_name,
        new_hd_name,
        sd_offset,
        hd_offset,
    )
    sd_payload = read_region(archive2, donor_sd.data_offset, donor_sd.data_size)
    hd_payload = read_region(archive2, donor_hd.data_offset, donor_hd.data_size)
    if sd_payload[:4] != b"ARCC" or hd_payload[:4] != b"ARCC":
        raise ValueError("Donor SD/HD payload does not begin with ARCC")

    archive0_size = ctx.archive.stat().st_size
    pyc_offset = align(archive0_size, ARCHIVE0_ALIGNMENT)
    final_archive0_size = pyc_offset + len(rebuilt_pyc)
    rebuilt_cdf0 = patch_cdf0_bytes(ctx.cdfiles, ctx.row, pyc_offset, len(rebuilt_pyc))

    plan = {
        "version": VERSION,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game": str(ctx.game),
        "archive0": str(ctx.archive),
        "cdfiles0": str(ctx.cdfiles),
        "archive2": str(archive2),
        "cdfiles2": str(cdfiles2),
        "new_uid": pyc_plan["build"]["new_uid"],
        "new_script_name": args.new_script_name,
        "donor_script_name": args.donor_script_name,
        "donor_uid": args.donor_uid,
        "original_driver_uid": args.original_driver_uid,
        "recipient_driver_uid": args.recipient_driver_uid,
        "pyc": pyc_plan,
        "assets": asset_meta,
        "archive0_original_size": archive0_size,
        "archive0_planned_offset": pyc_offset,
        "archive0_final_size": final_archive0_size,
        "archive2_original_size": archive2_size,
        "archive2_sd_offset": sd_offset,
        "archive2_hd_offset": hd_offset,
        "archive2_final_size": final_archive2_size,
        "sd_payload_sha256": sha256_bytes(sd_payload),
        "hd_payload_sha256": sha256_bytes(hd_payload),
        "sd_payload_size": len(sd_payload),
        "hd_payload_size": len(hd_payload),
        "cdfiles0_sha256_before": sha256_file(ctx.cdfiles),
        "cdfiles0_sha256_after": sha256_bytes(rebuilt_cdf0),
        "cdfiles2_sha256_before": sha256_bytes(cdf2_raw),
        "cdfiles2_sha256_after": sha256_bytes(rebuilt_cdf2),
        "expected_preview": "blank; PAINTSCHEME_25582 is intentionally not added",
        "test_scope": "startup, menu exposure, selection, and race loading; preview is out of scope",
    }
    return plan, rebuilt_pyc, rebuilt_cdf0, rebuilt_cdf2, sd_payload + hd_payload


def print_plan(plan: dict[str, Any]) -> None:
    a = plan["assets"]
    print(f"NASCAR 15 Complete Asset-Backed Extra Scheme Probe v{VERSION}")
    print(f"Game:              {plan['game']}")
    print(f"New livery UID:    {plan['new_uid']}")
    print(f"New ScriptName:    {plan['new_script_name']}")
    print(f"New SD entry:      {a['new_sd']['basename']}")
    print(f"New HD entry:      {a['new_hd']['basename']}")
    print(f"SD payload:        {plan['sd_payload_size']:,} bytes copied from donor")
    print(f"HD payload:        {plan['hd_payload_size']:,} bytes copied from donor")
    print(f"Livery count:      {plan['pyc']['validation']['before_livery_count']} -> {plan['pyc']['validation']['after_livery_count']}")
    print(f"Archive2 entries:  {a['before_file_count']} -> {a['after_file_count']}")
    print("Preview:           intentionally absent; blank thumbnail expected")


def write_analysis(plan: dict[str, Any]) -> Path:
    path = HERE / ANALYSIS_NAME
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


def cmd_analyze(args: argparse.Namespace) -> int:
    plan, _pyc, _cdf0, _cdf2, _payloads = build_plan(args)
    print_plan(plan)
    path = write_analysis(plan)
    print(f"\n[+] Analysis written: {path}")
    print("[dry-run] Nothing was changed.")
    return 0


def rollback(
    archive0: Path,
    archive0_size: int,
    archive2: Path,
    archive2_size: int,
    cdf0: Path,
    cdf0_backup: Path,
    cdf2: Path,
    cdf2_backup: Path,
) -> list[str]:
    messages = []
    try:
        if archive0.exists() and archive0.stat().st_size >= archive0_size:
            truncate_exact(archive0, archive0_size)
            messages.append("ARCHIVE0 truncated")
    except Exception as exc:
        messages.append(f"ARCHIVE0 rollback failed: {exc}")
    try:
        if archive2.exists() and archive2.stat().st_size >= archive2_size:
            truncate_exact(archive2, archive2_size)
            messages.append("ARCHIVE2 truncated")
    except Exception as exc:
        messages.append(f"ARCHIVE2 rollback failed: {exc}")
    try:
        shutil.copyfile(cdf0_backup, cdf0)
        messages.append("cdfiles.dat restored")
    except Exception as exc:
        messages.append(f"cdfiles.dat rollback failed: {exc}")
    try:
        shutil.copyfile(cdf2_backup, cdf2)
        messages.append("cdfiles2.dat restored")
    except Exception as exc:
        messages.append(f"cdfiles2.dat rollback failed: {exc}")
    return messages


def cmd_apply(args: argparse.Namespace) -> int:
    manifest_path = HERE / MANIFEST_NAME
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("applied") and not old.get("restored"):
            raise ValueError("A v0.6 test is already marked active. Run RESTORE_COMPLETE_SLOT.bat first.")

    plan, rebuilt_pyc, rebuilt_cdf0, rebuilt_cdf2, payloads = build_plan(args)
    print_plan(plan)
    ctx = base.load_context(args.game)
    archive2, cdfiles2 = archive2_paths(ctx.game)
    cdf0_backup = ensure_small_backup(ctx.cdfiles)
    cdf2_backup = ensure_small_backup(cdfiles2)
    archive0_size = ctx.archive.stat().st_size
    archive2_size = archive2.stat().st_size
    if archive0_size != plan["archive0_original_size"] or archive2_size != plan["archive2_original_size"]:
        raise ValueError("An archive size changed between Analyze and Apply planning")

    sd_size = plan["sd_payload_size"]
    sd_payload = payloads[:sd_size]
    hd_payload = payloads[sd_size:]
    try:
        append_payload(ctx.archive, plan["archive0_planned_offset"], rebuilt_pyc, ARCHIVE0_ALIGNMENT)
        append_payload(archive2, plan["archive2_sd_offset"], sd_payload, ARCHIVE2_ALIGNMENT)
        append_payload(archive2, plan["archive2_hd_offset"], hd_payload, ARCHIVE2_ALIGNMENT)
        atomic_write(ctx.cdfiles, rebuilt_cdf0)
        atomic_write(cdfiles2, rebuilt_cdf2)

        # Live readback: database entry and semantic map.
        live_row = base.find_cdf_row(ctx.cdfiles, base.PYC_NAME)
        if live_row.offset != plan["archive0_planned_offset"] or live_row.size != len(rebuilt_pyc):
            raise ValueError("Live cdfiles.dat did not repoint DB_GAME_LOCAL_SCRIPT.PYC as planned")
        live_pyc = read_region(ctx.archive, live_row.offset, live_row.size)
        if live_pyc != rebuilt_pyc:
            raise ValueError("Live appended PYC differs from the validated rebuild")
        live_validation = base.validate_rebuild(
            ctx,
            live_pyc,
            args.donor_uid,
            args.original_driver_uid,
            args.recipient_driver_uid,
            plan["new_uid"],
        )

        # Live readback: exact CDF V6 entries and payload hashes.
        live_cdf2 = parse_cdf_v6(cdfiles2.read_bytes())
        for label, name, expected_off, expected_size, expected_hash in (
            ("SD", f"LIVERY_{args.new_script_name}.ARC", plan["archive2_sd_offset"], plan["sd_payload_size"], plan["sd_payload_sha256"]),
            ("HD", f"HDLIVERY_{args.new_script_name}.ARC", plan["archive2_hd_offset"], plan["hd_payload_size"], plan["hd_payload_sha256"]),
        ):
            _, item = find_v6_file(live_cdf2, name)
            if item.data_offset != expected_off or item.data_size != expected_size:
                raise ValueError(f"Live {label} CDF mapping is wrong")
            if sha256_region(archive2, item.data_offset, item.data_size) != expected_hash:
                raise ValueError(f"Live {label} payload hash does not match the donor copy")
        if ctx.archive.stat().st_size != plan["archive0_final_size"]:
            raise ValueError("ARCHIVE0 final size mismatch")
        if archive2.stat().st_size != plan["archive2_final_size"]:
            raise ValueError("ARCHIVE2 final size mismatch")

        manifest = dict(plan)
        manifest.update({
            "applied": True,
            "restored": False,
            "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cdfiles0_backup": str(cdf0_backup),
            "cdfiles2_backup": str(cdf2_backup),
            "live_validation": live_validation,
            "archive0_sha256_appended_pyc": sha256_region(ctx.archive, plan["archive0_planned_offset"], len(rebuilt_pyc)),
            "cdfiles0_sha256_live": sha256_file(ctx.cdfiles),
            "cdfiles2_sha256_live": sha256_file(cdfiles2),
        })
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        write_analysis(plan)
    except Exception:
        messages = rollback(ctx.archive, archive0_size, archive2, archive2_size, ctx.cdfiles, cdf0_backup, cdfiles2, cdf2_backup)
        print("\n[rollback] " + "; ".join(messages), file=sys.stderr)
        raise

    print("\n[+] Complete 337th livery slot installed and read back successfully.")
    print("[+] The new SD and HD names are indexed independently in cdfiles2.dat.")
    print("[+] Their payloads are byte-identical copies of Brad's Indianapolis paint.")
    print("\nTEST IN GAME:")
    print("  1. Start NASCAR 15. First record whether it reaches the main menu.")
    print("  2. Select AJ Allmendinger and open Paint Schemes.")
    print("  3. Look for a second scheme. Its thumbnail may be blank.")
    print("  4. Select it and start a race.")
    print("  5. Confirm Brad's Indianapolis paint loads on AJ's car.")
    print("  6. Confirm Brad still retains his own Indianapolis alternate.")
    print("  7. Close the game, then run RESTORE_COMPLETE_SLOT.bat.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    manifest_path = HERE / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"{MANIFEST_NAME} was not found; Apply did not finish")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("restored"):
        print("[i] This v0.6 test is already restored.")
        return 0
    archive0 = Path(manifest["archive0"])
    cdf0 = Path(manifest["cdfiles0"])
    archive2 = Path(manifest["archive2"])
    cdf2 = Path(manifest["cdfiles2"])
    cdf0_backup = Path(manifest["cdfiles0_backup"])
    cdf2_backup = Path(manifest["cdfiles2_backup"])
    for path in (archive0, cdf0, archive2, cdf2, cdf0_backup, cdf2_backup):
        if not path.exists():
            raise FileNotFoundError(f"Restore file is missing: {path}")
    if archive0.stat().st_size != int(manifest["archive0_final_size"]):
        raise ValueError("ARCHIVE0 size changed after v0.6 Apply; refusing to truncate a later modification")
    if archive2.stat().st_size != int(manifest["archive2_final_size"]):
        raise ValueError("ARCHIVE2 size changed after v0.6 Apply; refusing to truncate a later modification")
    if sha256_file(cdf0) != manifest["cdfiles0_sha256_after"]:
        raise ValueError("cdfiles.dat changed after v0.6 Apply; refusing to overwrite a later modification")
    if sha256_file(cdf2) != manifest["cdfiles2_sha256_after"]:
        raise ValueError("cdfiles2.dat changed after v0.6 Apply; refusing to overwrite a later modification")

    atomic_write(cdf0, cdf0_backup.read_bytes())
    atomic_write(cdf2, cdf2_backup.read_bytes())
    truncate_exact(archive0, int(manifest["archive0_original_size"]))
    truncate_exact(archive2, int(manifest["archive2_original_size"]))
    if sha256_file(cdf0) != manifest["cdfiles0_sha256_before"]:
        raise ValueError("Restored cdfiles.dat hash mismatch")
    if sha256_file(cdf2) != manifest["cdfiles2_sha256_before"]:
        raise ValueError("Restored cdfiles2.dat hash mismatch")
    manifest["restored"] = True
    manifest["restored_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("[+] Restored the exact pre-v0.6 cdfiles indexes.")
    print("[+] Removed only the bytes v0.6 appended to ARCHIVE0 and ARCHIVE2.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create a complete asset-backed 337th NASCAR 15 livery slot")
    p.add_argument("command", choices=["analyze", "apply", "restore"])
    p.add_argument("--game", default=None, help="NASCAR 15 installation folder")
    p.add_argument("--donor-uid", type=int, default=DEFAULT_DONOR_UID)
    p.add_argument("--original-driver-uid", type=int, default=DEFAULT_ORIGINAL_DRIVER_UID)
    p.add_argument("--recipient-driver-uid", type=int, default=DEFAULT_RECIPIENT_DRIVER_UID)
    p.add_argument("--new-uid", type=int, default=DEFAULT_NEW_UID)
    p.add_argument("--donor-script-name", default=DEFAULT_DONOR_SCRIPT)
    p.add_argument("--new-script-name", default=DEFAULT_NEW_SCRIPT)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.new_uid >= 25600:
        raise ValueError("This controlled test refuses livery UIDs 25600 or higher")
    return {
        "analyze": cmd_analyze,
        "apply": cmd_apply,
        "restore": cmd_restore,
    }[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
