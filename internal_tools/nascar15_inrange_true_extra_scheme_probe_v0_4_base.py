#!/usr/bin/env python3
r"""
NASCAR 15 In-Range True Extra Scheme Isolation Probe v0.4

This probe creates ONE genuinely new LIVERIE_c record. It does not steal the
existing donor record from its original driver.

Default controlled test
-----------------------
Donor livery UID:         25580 (Brad Keselowski Indianapolis alternate)
Original donor driver:    1115  (Brad Keselowski)
Recipient driver:         1083  (AJ Allmendinger)
Preferred new livery UID: 25582

The clone intentionally reuses the donor ScriptName and therefore the donor's
existing SD/HD paint files. That isolates the first question:

    Does a newly appended LIVERIE_c record become a real carousel/race slot?

A blank preview is expected because PAINTSCHEME_<new UID> does not exist yet.
No preview container is modified by this probe.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "0.4"
PYC_NAME = "DB_GAME_LOCAL_SCRIPT.PYC"
DEFAULT_DONOR_UID = 25580
DEFAULT_ORIGINAL_DRIVER_UID = 1115
DEFAULT_RECIPIENT_DRIVER_UID = 1083
DEFAULT_NEW_UID = 25582
BACKUP_SUFFIX = ".true_extra_scheme_probe_v0_4.bak"
MANIFEST_NAME = "true_extra_scheme_slot_v0_4_manifest.json"
ANALYSIS_JSON = "true_extra_scheme_slot_v0_4_analysis.json"
CONNECTION_MD = "true_extra_scheme_connection_map_v0_4.md"
CONNECTION_CSV = "true_extra_scheme_asset_locations_v0_4.csv"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def import_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def load_config_candidates() -> list[Path]:
    out = [script_dir() / "config.json"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "NASCAR15ModdingApp" / "config.json")
    return out


def detect_game(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for cfg in load_config_candidates():
        try:
            if cfg.exists():
                game = (json.loads(cfg.read_text(encoding="utf-8")) or {}).get("game")
                if game:
                    candidates.append(Path(game))
        except Exception:
            pass
    candidates.extend([
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\NASCAR 15"),
        Path(r"D:\SteamLibrary\steamapps\common\NASCAR 15"),
        Path(r"E:\SteamLibrary\steamapps\common\NASCAR 15"),
    ])
    seen: set[str] = set()
    for p in candidates:
        key = str(p).casefold()
        if key in seen:
            continue
        seen.add(key)
        if (p / "data" / "ARCHIVE0.AR").exists():
            return p
    raise FileNotFoundError("NASCAR 15 was not found. Pass --game with the game folder.")


def archive0_paths(game: Path) -> tuple[Path, Path]:
    data = game / "data"
    arc = data / "ARCHIVE0.AR"
    cdf = next((p for p in (data / "cdfiles.dat", data / "cdfiles0.dat") if p.exists()), None)
    if not arc.exists() or cdf is None:
        raise FileNotFoundError("ARCHIVE0.AR/cdfiles.dat was not found")
    return arc, cdf


@dataclass
class CDFRow:
    name: str
    offset: int
    size: int
    record_pos: int
    layout: str


def parse_cdf_rows(path: Path) -> tuple[list[CDFRow], str]:
    data = path.read_bytes()
    if len(data) < 48 or struct.unpack_from("<I", data, 0)[0] != 0x436C6966:
        raise ValueError(f"{path.name} is not a filC index")
    header = struct.unpack_from("<12I", data, 0)
    count, strtab = header[8], header[10]
    string_base = len(data) - strtab

    def name_at(off: int) -> str:
        if off < 0 or off >= strtab:
            return ""
        p = string_base + off
        e = data.find(b"\0", p)
        if e < p:
            return ""
        return data[p:e].decode("ascii", "replace")

    for start, layout in ((0x40, "A"), (0x50, "B")):
        rows: list[CDFRow] = []
        valid = 0
        pos = start
        for _ in range(count):
            if pos + 32 > string_base:
                break
            fields = struct.unpack_from("<8I", data, pos)
            if layout == "A":
                name_off, size, offset = fields[1], fields[2], fields[5]
            else:
                name_off, size, offset = fields[3], fields[4], fields[7]
            name = name_at(name_off)
            if name and all(32 <= ord(c) < 127 for c in name):
                valid += 1
                rows.append(CDFRow(name, int(offset), int(size), pos, layout))
            pos += 32
        if count and valid > count * 0.8:
            return rows, layout
    raise ValueError(f"Unrecognized cdfiles layout in {path}")


def find_cdf_row(path: Path, target: str) -> CDFRow:
    rows, _ = parse_cdf_rows(path)
    hits = [r for r in rows if r.name.casefold() == target.casefold()]
    if len(hits) != 1:
        raise ValueError(f"Expected one {target} row in {path.name}; found {len(hits)}")
    return hits[0]


def write_cdf_row(path: Path, row: CDFRow, offset: int, size: int) -> None:
    with path.open("r+b") as f:
        if row.layout == "A":
            f.seek(row.record_pos + 8)
            f.write(struct.pack("<I", size))
            f.seek(row.record_pos + 20)
            f.write(struct.pack("<I", offset))
        else:
            f.seek(row.record_pos + 16)
            f.write(struct.pack("<I", size))
            f.seek(row.record_pos + 28)
            f.write(struct.pack("<I", offset))
        f.flush()
        os.fsync(f.fileno())


def registry(game: Path) -> dict[str, tuple[Path, Path]]:
    data = game / "data"
    out: dict[str, tuple[Path, Path]] = {}
    for cdf in data.glob("cdfiles*.dat"):
        m = re.match(r"cdfiles(\d*)\.dat$", cdf.name, re.I)
        if not m:
            continue
        suffix = m.group(1) or "0"
        arc = data / f"ARCHIVE{suffix}.AR"
        if arc.exists():
            out[suffix] = (arc, cdf)
    return out


# The record mapper has shipped under two byte-identical names. app.py loads the
# _teams one via MAPPER_NAME, so accept either and prefer that, instead of
# hard-failing on a filename that may not be present.
MAPPER_CANDIDATES = (
    "nascar15_pyc_record_mapper_v5_teams.py",
    "nascar15_pyc_record_mapper_v5_fixed.py",
)


def load_modules():
    root = script_dir()
    mapper_path = next((root / n for n in MAPPER_CANDIDATES if (root / n).exists()), None)
    patcher_path = root / "nascar15_v11_probe_patcher.py"
    if mapper_path is None or not patcher_path.exists():
        missing = []
        if mapper_path is None:
            missing.append(" or ".join(MAPPER_CANDIDATES))
        if not patcher_path.exists():
            missing.append("nascar15_v11_probe_patcher.py")
        raise FileNotFoundError(
            "Put this probe in the Modding App folder beside " + " and ".join(missing)
        )
    mapper = import_module(mapper_path, "n15_true_slot_mapper")
    containers_path = root / "containers.py"
    containers = import_module(containers_path, "n15_true_slot_containers") if containers_path.exists() else None
    return mapper, patcher_path, containers


@dataclass
class Context:
    game: Path
    archive: Path
    cdfiles: Path
    row: CDFRow
    pyc: bytes
    mapper: Any
    containers: Any
    root: Any
    schemas: dict[str, Any]
    records: list[Any]


def load_context(game_arg: str | None) -> Context:
    game = detect_game(game_arg)
    archive, cdfiles = archive0_paths(game)
    mapper, patcher, containers = load_modules()
    # Use the mapper's tolerant cdf reader to confirm the entry, but retain our
    # exact row position for append/repoint installation.
    entries = mapper.load_entries(cdfiles, patcher)
    entry = mapper.find_entry(entries, PYC_NAME)
    row = find_cdf_row(cdfiles, PYC_NAME)
    if entry.offset != row.offset or entry.size != row.size:
        raise ValueError("Mapper and direct cdfiles parser disagree about the PYC row")
    with archive.open("rb") as f:
        f.seek(row.offset)
        pyc = f.read(row.size)
    if len(pyc) != row.size:
        raise ValueError("Short DB_GAME_LOCAL_SCRIPT.PYC read")
    root = mapper.parse_pyc(pyc)
    schemas = mapper.build_schemas(root)
    records = mapper.map_records(root, schemas)
    return Context(game, archive, cdfiles, row, pyc, mapper, containers, root, schemas, records)


def pointer_int(ctx: Context, value: Any) -> int | None:
    return ctx.mapper.pointer_to_int(value)


def records_of(ctx: Context, class_name: str) -> list[Any]:
    return [r for r in ctx.records if r.class_name == class_name]


def find_record(ctx: Context, class_name: str, uid: int) -> Any:
    hits = [r for r in records_of(ctx, class_name) if pointer_int(ctx, r.uid) == int(uid)]
    if len(hits) != 1:
        raise ValueError(f"Expected one {class_name} UID {uid}; found {len(hits)}")
    return hits[0]


def display(ctx: Context, value: Any) -> str:
    return ctx.mapper.value_to_display(value)


def field_uid(ctx: Context, rec: Any, field: str) -> int | None:
    value = rec.fields.get(field)
    if isinstance(value, ctx.mapper.ObjCall) and value.args:
        return pointer_int(ctx, value.args[0])
    return pointer_int(ctx, value)


def livery_summary(ctx: Context, rec: Any) -> dict[str, Any]:
    fields = ctx.schemas["LIVERIE_c"].fields
    return {
        "uid": pointer_int(ctx, rec.uid),
        "call_offset": rec.call_offset,
        "fields": {name: display(ctx, rec.fields.get(name)) for name in fields},
        "driver_uid": field_uid(ctx, rec, "Driver"),
        "package_uid": field_uid(ctx, rec, "Package"),
        "world_uid": field_uid(ctx, rec, "World"),
        "season_uid": field_uid(ctx, rec, "Season"),
    }


def py2_instructions(ctx: Context, code: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = 0
    ext = 0
    while i < len(code):
        off = i
        op = code[i]
        i += 1
        arg = None
        arg_pos = None
        if op >= ctx.mapper.HAVE_ARGUMENT:
            if i + 2 > len(code):
                raise ValueError(f"Truncated bytecode operand at 0x{off:X}")
            arg_pos = i
            raw = code[i] | (code[i + 1] << 8)
            i += 2
            arg = raw | ext
            if op == 143:  # EXTENDED_ARG
                ext = arg << 16
            else:
                ext = 0
        out.append({
            "offset": off,
            "op": op,
            "opname": ctx.mapper.OP.get(op, f"OP_{op}"),
            "arg": arg,
            "arg_pos": arg_pos,
        })
    return out


def const_plain(ctx: Context, index: int | None) -> Any:
    co = ctx.root.value
    if index is None or index < 0 or index >= len(co.consts):
        return None
    return ctx.mapper.value_plain_for_compare(co.consts[index])


def find_const_index(ctx: Context, value: int, same_tag: str | None = None) -> int:
    hits = []
    for i, item in enumerate(ctx.root.value.consts):
        if ctx.mapper.value_plain_for_compare(item) == int(value) and (same_tag is None or item.tag == same_tag):
            hits.append(i)
    if not hits:
        raise ValueError(f"No root constant exists for {value}")
    return hits[0]


def emit(op: int, arg: int | None = None) -> bytes:
    if arg is None:
        return bytes([op])
    if not 0 <= int(arg) <= 0xFFFF:
        raise ValueError(f"Bytecode operand {arg} needs EXTENDED_ARG; probe refuses")
    return bytes([op, int(arg) & 0xFF, (int(arg) >> 8) & 0xFF])


class MarshalSkip:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> int:
        if self.pos + n > len(self.data):
            raise ValueError("Truncated Python-2 marshal object")
        old = self.pos
        self.pos += n
        return old

    def i32(self) -> int:
        off = self.take(4)
        return struct.unpack_from("<i", self.data, off)[0]

    def obj(self, depth: int = 0) -> None:
        if depth > 400:
            raise ValueError("Marshal nesting is too deep")
        tag = chr(self.data[self.take(1)] & 0x7F)
        if tag in ("N", "T", "F", "S", ".", "0"):
            return
        if tag == "i":
            self.take(4); return
        if tag == "I":
            self.take(8); return
        if tag == "g":
            self.take(8); return
        if tag == "y":
            self.take(16); return
        if tag == "f":
            self.take(self.data[self.take(1)]); return
        if tag == "x":
            self.take(self.data[self.take(1)]); self.take(self.data[self.take(1)]); return
        if tag == "l":
            self.take(abs(self.i32()) * 2); return
        if tag in ("s", "t", "u"):
            n = self.i32()
            if n < 0:
                raise ValueError("Negative marshal string length")
            self.take(n); return
        if tag == "R":
            self.take(4); return
        if tag in ("(", "["):
            n = self.i32()
            if n < 0 or n > 10_000_000:
                raise ValueError("Invalid marshal sequence length")
            for _ in range(n):
                self.obj(depth + 1)
            return
        if tag == "<" or tag == ">":
            n = self.i32()
            for _ in range(n):
                self.obj(depth + 1)
            return
        if tag == "{":
            while True:
                if self.pos >= len(self.data):
                    raise ValueError("Unterminated marshal dict")
                if chr(self.data[self.pos] & 0x7F) == "0":
                    self.pos += 1
                    break
                self.obj(depth + 1)
                self.obj(depth + 1)
            return
        if tag == "c":
            self.take(16)
            for _ in range(8):
                self.obj(depth + 1)
            self.take(4)
            self.obj(depth + 1)
            return
        raise ValueError(f"Unsupported Python-2 marshal tag {tag!r}")


def root_layout(pyc: bytes) -> dict[str, int]:
    if len(pyc) < 31 or (pyc[8] & 0x7F) != ord("c"):
        raise ValueError("Unexpected PYC root layout")
    r = MarshalSkip(pyc)
    r.pos = 9
    r.take(16)
    code_tag = chr(pyc[r.take(1)] & 0x7F)
    if code_tag not in ("s", "t"):
        raise ValueError("Root bytecode is not a marshal string")
    code_len_pos = r.pos
    code_len = r.i32()
    code_off = r.take(code_len)
    if chr(pyc[r.take(1)] & 0x7F) != "(":
        raise ValueError("Root constants are not a tuple")
    count_pos = r.pos
    count = r.i32()
    const_start = r.pos
    for _ in range(count):
        r.obj(1)
    return {
        "code_len_pos": code_len_pos,
        "code_len": code_len,
        "code_off": code_off,
        "count_pos": count_pos,
        "count": count,
        "const_start": const_start,
        "const_end": r.pos,
    }


def marshal_int(value: int) -> bytes:
    if not -(2**31) <= int(value) < 2**31:
        return b"I" + struct.pack("<q", int(value))
    return b"i" + struct.pack("<i", int(value))


def constructor_block(ctx: Context, donor: Any, new_const_index: int) -> tuple[bytes, dict[str, Any]]:
    co = ctx.root.value
    instructions = py2_instructions(ctx, co.code_bytes)
    call_index = next((i for i, x in enumerate(instructions)
                       if x["offset"] == donor.call_offset and x["opname"] == "CALL_FUNCTION"), None)
    if call_index is None or instructions[call_index]["arg"] != 17:
        raise ValueError("Could not find donor LIVERIE_c CALL_FUNCTION 17")
    donor_uid = pointer_int(ctx, donor.uid)
    start_index = None
    for i in range(call_index - 1, -1, -1):
        x = instructions[i]
        if x["opname"] != "LOAD_NAME" or x["arg"] is None:
            continue
        if co.names[x["arg"]] != "LIVERIE_c":
            continue
        if i + 1 < len(instructions) and instructions[i + 1]["opname"] == "LOAD_CONST":
            if const_plain(ctx, instructions[i + 1]["arg"]) == donor_uid:
                start_index = i
                break
    if start_index is None:
        raise ValueError("Could not locate donor constructor start")
    if call_index + 3 >= len(instructions):
        raise ValueError("Donor constructor tail is truncated")
    tail = instructions[call_index:call_index + 4]
    if [x["opname"] for x in tail] != ["CALL_FUNCTION", "ROT_TWO", "LOAD_CONST", "STORE_SUBSCR"]:
        raise ValueError("Unexpected donor constructor tail")
    end = tail[-1]["offset"] + 1
    start = instructions[start_index]["offset"]
    raw = bytearray(co.code_bytes[start:end])
    # The stock generator keeps the __LIVERIE dictionary on the stack and uses
    # DUP_TOP for each constructor. Our appended block is independent, so load
    # the dictionary explicitly before cloning the constructor body.
    try:
        livery_table_name = co.names.index("__LIVERIE")
    except ValueError:
        raise ValueError("Root bytecode has no __LIVERIE table name")
    clone = bytearray(emit(101, livery_table_name) + raw)  # LOAD_NAME
    replaced = []
    for ins in py2_instructions(ctx, bytes(clone)):
        if ins["opname"] == "LOAD_CONST" and const_plain(ctx, ins["arg"]) == donor_uid:
            struct.pack_into("<H", clone, ins["arg_pos"], new_const_index)
            replaced.append(ins["offset"])
    if len(replaced) != 2:
        raise ValueError(f"Expected two donor UID operands in constructor; found {len(replaced)}")
    return bytes(clone), {
        "source_start": start,
        "source_end": end,
        "source_call": donor.call_offset,
        "uid_operand_count": len(replaced),
        "clone_bytes": len(clone),
    }


def post_assignment_blocks(ctx: Context, donor_uid: int) -> list[dict[str, Any]]:
    co = ctx.root.value
    instructions = py2_instructions(ctx, co.code_bytes)
    out = []
    allowed_shapes = {
        ("LOAD_NAME", "LOAD_CONST", "BINARY_SUBSCR", "LOAD_NAME", "LOAD_CONST", "BINARY_SUBSCR", "STORE_ATTR"),
        ("LOAD_GLOBAL", "LOAD_CONST", "BINARY_SUBSCR", "LOAD_GLOBAL", "LOAD_CONST", "BINARY_SUBSCR", "STORE_ATTR"),
    }
    for i, item in enumerate(instructions):
        if item["opname"] != "STORE_ATTR" or i < 6:
            continue
        seq = instructions[i - 6:i + 1]
        if tuple(x["opname"] for x in seq) not in allowed_shapes:
            continue
        target_table = seq[3]
        target_uid = seq[4]
        if target_table["arg"] is None or target_table["arg"] >= len(co.names):
            continue
        if co.names[target_table["arg"]] != "__LIVERIE":
            continue
        if const_plain(ctx, target_uid["arg"]) != donor_uid:
            continue
        source_table = co.names[seq[0]["arg"]] if seq[0]["arg"] is not None else ""
        field = co.names[item["arg"]] if item["arg"] is not None else ""
        out.append({
            "field": field,
            "source_table": source_table,
            "source_uid": const_plain(ctx, seq[1]["arg"]),
            "source_const_index": seq[1]["arg"],
            "target_const_index": target_uid["arg"],
            "start": seq[0]["offset"],
            "end": item["offset"] + 3,
            "raw": co.code_bytes[seq[0]["offset"]:item["offset"] + 3],
        })
    return out


def donor_uid_references(ctx: Context, donor_uid: int) -> list[dict[str, Any]]:
    co = ctx.root.value
    refs = []
    for item in py2_instructions(ctx, co.code_bytes):
        if item["opname"] == "LOAD_CONST" and const_plain(ctx, item["arg"]) == donor_uid:
            refs.append({"offset": item["offset"], "const_index": item["arg"]})
    return refs


def choose_new_uid(ctx: Context, preferred: int) -> int:
    used_record_uids = {pointer_int(ctx, r.uid) for r in ctx.records}
    candidate = int(preferred)
    while candidate in used_record_uids:
        candidate += 1
    if candidate >= 2**31:
        raise ValueError("Could not find a free 32-bit record UID")
    return candidate


def build_clone_code(ctx: Context, donor: Any, new_uid: int, recipient_uid: int) -> tuple[bytes, dict[str, Any]]:
    co = ctx.root.value
    if len(co.consts) >= 0xFFFF:
        raise ValueError("Root constant table has reached the 16-bit LOAD_CONST limit")
    new_const_index = len(co.consts)
    ctor, ctor_meta = constructor_block(ctx, donor, new_const_index)
    assignments = post_assignment_blocks(ctx, pointer_int(ctx, donor.uid))
    if not assignments:
        raise ValueError("No donor post-constructor assignments were found")
    assignment_fields = [a["field"] for a in assignments]
    required = {"Driver", "Package", "World", "Season"}
    if not required.issubset(set(assignment_fields)):
        raise ValueError(f"Donor assignment map is incomplete: {assignment_fields}")
    recipient_const = find_const_index(ctx, recipient_uid)
    clone_assignments = bytearray()
    assignment_meta = []
    for assignment in assignments:
        raw = bytearray(assignment["raw"])
        target_replaced = 0
        source_replaced = 0
        for item in py2_instructions(ctx, bytes(raw)):
            if item["opname"] != "LOAD_CONST":
                continue
            value = const_plain(ctx, item["arg"])
            if value == pointer_int(ctx, donor.uid):
                struct.pack_into("<H", raw, item["arg_pos"], new_const_index)
                target_replaced += 1
            elif assignment["field"] == "Driver" and value == assignment["source_uid"]:
                struct.pack_into("<H", raw, item["arg_pos"], recipient_const)
                source_replaced += 1
        if target_replaced != 1:
            raise ValueError(f"{assignment['field']} clone did not replace exactly one target UID")
        if assignment["field"] == "Driver" and source_replaced != 1:
            raise ValueError("Driver clone did not replace exactly one source driver UID")
        clone_assignments += raw
        assignment_meta.append({k: assignment[k] for k in (
            "field", "source_table", "source_uid", "start", "end"
        )})
    return bytes(ctor + clone_assignments), {
        "new_uid": new_uid,
        "new_const_index": new_const_index,
        "constructor": ctor_meta,
        "assignments": assignment_meta,
        "total_code_bytes": len(ctor) + len(clone_assignments),
    }


def locate_driver_assignment_operand(ctx: Context, donor_uid: int) -> dict[str, Any]:
    blocks = [b for b in post_assignment_blocks(ctx, donor_uid) if b["field"] == "Driver"]
    if len(blocks) != 1:
        raise ValueError(f"Expected one donor Driver assignment; found {len(blocks)}")
    block = blocks[0]
    instructions = py2_instructions(ctx, ctx.root.value.code_bytes)
    target = next(x for x in instructions if x["offset"] == block["start"] + 3 and x["opname"] == "LOAD_CONST")
    layout = root_layout(ctx.pyc)
    return {
        "code_argument_offset": target["arg_pos"],
        "pyc_argument_offset": layout["code_off"] + target["arg_pos"],
        "const_index": target["arg"],
        "driver_uid": const_plain(ctx, target["arg"]),
    }


def build_patched_pyc(ctx: Context, donor_uid: int, original_driver_uid: int,
                      recipient_uid: int, preferred_new_uid: int) -> tuple[bytes, dict[str, Any]]:
    donor = find_record(ctx, "LIVERIE_c", donor_uid)
    current_driver = field_uid(ctx, donor, "Driver")
    if current_driver not in (original_driver_uid, recipient_uid):
        raise ValueError(
            f"Donor is currently assigned to driver UID {current_driver}; expected "
            f"original {original_driver_uid} or prior probe recipient {recipient_uid}"
        )
    new_uid = choose_new_uid(ctx, preferred_new_uid)
    if new_uid >= 25600:
        raise ValueError("Isolation probe refuses UID 25600 or higher; use a free in-range UID")
    if any(pointer_int(ctx, r.uid) == new_uid for r in ctx.records):
        raise ValueError(f"Chosen new UID {new_uid} is already used")
    code_to_insert, clone_meta = build_clone_code(ctx, donor, new_uid, recipient_uid)
    layout = root_layout(ctx.pyc)
    if layout["count"] != len(ctx.root.value.consts):
        raise ValueError("Root constant count does not match parser")
    instructions = py2_instructions(ctx, ctx.root.value.code_bytes)
    if not instructions or instructions[-1]["opname"] != "RETURN_VALUE":
        raise ValueError("Root module does not end in RETURN_VALUE")

    # Critical v0.2 fix: the module builds a master DATA dictionary near the end
    # by inserting every class table, including __LIVERIE. v0.1 appended the
    # clone after DATA had already been built. The carousel could enumerate
    # __LIVERIE, but race/session code resolving the UID through DATA could not,
    # producing an endless loading screen. Insert immediately before BUILD_MAP
    # for DATA so the new record is included in both lookups.
    data_store_index = next((i for i, x in enumerate(instructions)
                             if x["opname"] == "STORE_NAME" and x["arg"] is not None
                             and ctx.root.value.names[x["arg"]] == "DATA"), None)
    if data_store_index is None:
        raise ValueError("Could not locate the master DATA registry build")
    build_map_index = next((i for i in range(data_store_index - 1, -1, -1)
                            if instructions[i]["opname"] == "BUILD_MAP"), None)
    if build_map_index is None:
        raise ValueError("Could not locate BUILD_MAP for the master DATA registry")
    insert_code_offset = instructions[build_map_index]["offset"]
    if any(("JUMP" in x["opname"] or x["opname"] in
            ("FOR_ITER", "SETUP_LOOP", "SETUP_EXCEPT", "SETUP_FINALLY", "CONTINUE_LOOP"))
           for x in instructions):
        raise ValueError("Root module contains control-flow jumps; safe insertion needs relocation")
    out = bytearray(ctx.pyc)

    # If the earlier donor-reassignment probe is still applied, restore the donor
    # to Brad inside the rebuilt PYC. AJ receives the new clone instead.
    donor_restore = None
    if current_driver != original_driver_uid:
        operand = locate_driver_assignment_operand(ctx, donor_uid)
        original_const = find_const_index(ctx, original_driver_uid)
        if original_const > 0xFFFF:
            raise ValueError("Original driver constant needs EXTENDED_ARG")
        struct.pack_into("<H", out, operand["pyc_argument_offset"], original_const)
        donor_restore = {
            "from_driver_uid": current_driver,
            "to_driver_uid": original_driver_uid,
            "operand": operand,
            "new_const_index": original_const,
        }

    absolute_insert = layout["code_off"] + insert_code_offset
    out[absolute_insert:absolute_insert] = code_to_insert
    code_delta = len(code_to_insert)
    struct.pack_into("<i", out, layout["code_len_pos"], layout["code_len"] + code_delta)
    shifted_count_pos = layout["count_pos"] + code_delta
    shifted_const_end = layout["const_end"] + code_delta
    struct.pack_into("<i", out, shifted_count_pos, layout["count"] + 1)
    encoded_uid = marshal_int(new_uid)
    out[shifted_const_end:shifted_const_end] = encoded_uid
    rebuilt = bytes(out)
    meta = {
        "new_uid": new_uid,
        "old_pyc_size": len(ctx.pyc),
        "new_pyc_size": len(rebuilt),
        "growth": len(rebuilt) - len(ctx.pyc),
        "insert_code_offset": insert_code_offset,
        "insert_before_master_DATA": True,
        "insert_code_bytes": code_delta,
        "marshal_constant_bytes": len(encoded_uid),
        "clone": clone_meta,
        "donor_restore": donor_restore,
    }
    return rebuilt, meta


def record_signature(ctx: Context, rec: Any) -> dict[str, str]:
    return {k: display(ctx, v) for k, v in rec.fields.items()}


def validate_rebuild(ctx: Context, rebuilt: bytes, donor_uid: int, original_driver_uid: int,
                     recipient_uid: int, new_uid: int) -> dict[str, Any]:
    mapper = ctx.mapper
    root2 = mapper.parse_pyc(rebuilt)
    schemas2 = mapper.build_schemas(root2)
    records2 = mapper.map_records(root2, schemas2)
    after = Context(ctx.game, ctx.archive, ctx.cdfiles, ctx.row, rebuilt,
                    mapper, ctx.containers, root2, schemas2, records2)
    before_liv = {pointer_int(ctx, r.uid): r for r in records_of(ctx, "LIVERIE_c")}
    after_liv = {pointer_int(after, r.uid): r for r in records_of(after, "LIVERIE_c")}
    expected_uids = set(before_liv) | {new_uid}
    if set(after_liv) != expected_uids:
        raise ValueError("LIVERIE_c UID set did not gain exactly the requested new UID")
    donor_after = after_liv[donor_uid]
    clone = after_liv[new_uid]
    if field_uid(after, donor_after, "Driver") != original_driver_uid:
        raise ValueError("Original donor was not retained by its original driver")
    if field_uid(after, clone, "Driver") != recipient_uid:
        raise ValueError("New clone was not assigned to the recipient driver")
    donor_sig = record_signature(after, donor_after)
    clone_sig = record_signature(after, clone)
    for field in after.schemas["LIVERIE_c"].fields:
        if field in ("UID", "Driver"):
            continue
        if donor_sig.get(field) != clone_sig.get(field):
            raise ValueError(f"Clone field {field} differs from donor")

    existing_changes = []
    for uid, old in before_liv.items():
        new = after_liv[uid]
        old_sig = record_signature(ctx, old)
        new_sig = record_signature(after, new)
        for field in sorted(set(old_sig) | set(new_sig)):
            if old_sig.get(field) != new_sig.get(field):
                existing_changes.append({
                    "uid": uid, "field": field,
                    "before": old_sig.get(field), "after": new_sig.get(field)
                })
    allowed = []
    before_driver = field_uid(ctx, before_liv[donor_uid], "Driver")
    if before_driver != original_driver_uid:
        allowed = [{"uid": donor_uid, "field": "Driver"}]
    actual = [{"uid": x["uid"], "field": x["field"]} for x in existing_changes]
    if actual != allowed:
        raise ValueError(f"Unexpected existing-record changes: {existing_changes[:20]}")

    before_class_counts: dict[str, int] = {}
    after_class_counts: dict[str, int] = {}
    for r in ctx.records:
        before_class_counts[r.class_name] = before_class_counts.get(r.class_name, 0) + 1
    for r in after.records:
        after_class_counts[r.class_name] = after_class_counts.get(r.class_name, 0) + 1
    for class_name in set(before_class_counts) | set(after_class_counts):
        delta = after_class_counts.get(class_name, 0) - before_class_counts.get(class_name, 0)
        expected = 1 if class_name == "LIVERIE_c" else 0
        if delta != expected:
            raise ValueError(f"Unexpected {class_name} record-count delta {delta}")
    return {
        "verified": True,
        "before_livery_count": len(before_liv),
        "after_livery_count": len(after_liv),
        "existing_record_changes": existing_changes,
        "donor_after": livery_summary(after, donor_after),
        "clone": livery_summary(after, clone),
        "pyc_sha256": sha256_bytes(rebuilt),
    }


def scan_outer_assets(ctx: Context, script_name: str) -> list[dict[str, Any]]:
    targets = {
        f"LIVERY_{script_name}.ARC".casefold(),
        f"HDLIVERY_{script_name}.ARC".casefold(),
    }
    token = script_name.casefold()
    rows_out = []
    for archive_id, (archive, cdf) in registry(ctx.game).items():
        try:
            rows, _ = parse_cdf_rows(cdf)
        except Exception as exc:
            rows_out.append({"kind": "index_error", "archive": archive_id, "name": cdf.name, "error": str(exc)})
            continue
        for row in rows:
            low = row.name.casefold()
            if low in targets or token in low:
                rows_out.append({
                    "kind": "outer_asset",
                    "archive": archive_id,
                    "index": cdf.name,
                    "archive_file": archive.name,
                    "name": row.name,
                    "offset": row.offset,
                    "size": row.size,
                })
    return rows_out


def scan_preview_assets(ctx: Context, donor_uid: int, new_uid: int,
                        donor_driver_uid: int, recipient_uid: int) -> list[dict[str, Any]]:
    if ctx.containers is None:
        return [{"kind": "preview_scan", "error": "containers.py was not available"}]
    targets = {
        f"PAINTSCHEME_{donor_uid}",
        f"PAINTSCHEME_{new_uid}",
        f"DRIVERPAINT_{donor_driver_uid}_25041",
        f"DRIVERPAINT_{recipient_uid}_25041",
    }
    found = []
    for archive_id, (archive, cdf) in registry(ctx.game).items():
        try:
            rows, _ = parse_cdf_rows(cdf)
        except Exception:
            continue
        candidates = [r for r in rows if "DRIVERSELECTTD" in r.name.upper()]
        if not candidates:
            continue
        with archive.open("rb") as f:
            for row in candidates:
                try:
                    f.seek(row.offset)
                    blob = f.read(row.size)
                    if len(blob) != row.size:
                        continue
                    entries, _base = ctx.containers.parse_multi_arc(blob)
                    for entry in entries:
                        if entry.get("name") in targets:
                            found.append({
                                "kind": "inner_preview",
                                "archive": archive_id,
                                "container": row.name,
                                "container_offset": row.offset,
                                "container_size": row.size,
                                "entry": entry.get("name"),
                                "entry_index": entry.get("index"),
                                "width": entry.get("w"),
                                "height": entry.get("h"),
                                "format": entry.get("fmt"),
                                "payload_size": entry.get("payload_size"),
                            })
                except Exception as exc:
                    # Keep scan failures out of normal output; these containers
                    # are heterogeneous and a failed parse is not proof of damage.
                    continue
    for target in sorted(targets):
        if not any(x.get("entry") == target for x in found):
            found.append({"kind": "inner_preview_missing", "entry": target})
    return found


def linked_record_summary(ctx: Context, class_name: str, uid: int | None) -> dict[str, Any] | None:
    if uid is None:
        return None
    try:
        rec = find_record(ctx, class_name, uid)
    except Exception:
        return {"class": class_name, "uid": uid, "found": False}
    return {
        "class": class_name,
        "uid": uid,
        "found": True,
        "fields": {k: display(ctx, v) for k, v in rec.fields.items()},
    }


def connection_map(ctx: Context, donor: Any, new_uid: int, recipient_uid: int) -> dict[str, Any]:
    summary = livery_summary(ctx, donor)
    script_name = summary["fields"]["ScriptName"]
    refs = donor_uid_references(ctx, pointer_int(ctx, donor.uid))
    assignments = post_assignment_blocks(ctx, pointer_int(ctx, donor.uid))
    outer = scan_outer_assets(ctx, script_name)
    previews = scan_preview_assets(ctx, pointer_int(ctx, donor.uid), new_uid,
                                   summary["driver_uid"], recipient_uid)
    return {
        "database": {
            "pyc": PYC_NAME,
            "livery": summary,
            "uid_bytecode_references": refs,
            "post_assignments": [{k: x[k] for k in (
                "field", "source_table", "source_uid", "start", "end"
            )} for x in assignments],
        },
        "linked_records": {
            "driver": linked_record_summary(ctx, "DRIVER_c", summary["driver_uid"]),
            "recipient_driver": linked_record_summary(ctx, "DRIVER_c", recipient_uid),
            "package": linked_record_summary(ctx, "DLCPACKAGE_c", summary["package_uid"]),
            "world": linked_record_summary(ctx, "WORLDSCRIPT_c", summary["world_uid"]),
            "season": linked_record_summary(ctx, "RACESERIES_c", summary["season_uid"]),
        },
        "paint_assets": outer,
        "preview_assets": previews,
        "expected_new_preview_name": f"PAINTSCHEME_{new_uid}",
        "connection_conclusions": [
            "LIVERIE_c.Driver controls carousel ownership.",
            "LIVERIE_c.ScriptName resolves the SD/HD LIVERY_<ScriptName>.ARC paint assets.",
            "Package, World and Season are post-constructor links and must be cloned with the record.",
            "PAINTSCHEME_<Livery UID> is a separate front-end preview resource.",
            "The new record must exist before the module builds its master DATA registry.",
            "This probe intentionally creates no PAINTSCHEME_<new UID>, so a blank preview is expected.",
        ],
    }


def write_connection_files(plan: dict[str, Any]) -> None:
    base = script_dir()
    conn = plan["connections"]
    rows = conn["paint_assets"] + conn["preview_assets"]
    fields = sorted({k for row in rows for k in row.keys()}) if rows else ["kind"]
    with (base / CONNECTION_CSV).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    livery = conn["database"]["livery"]
    lines = [
        "# NASCAR 15 Extra Scheme Connection Map",
        "",
        f"- Donor livery UID: `{livery['uid']}`",
        f"- Script name: `{livery['fields']['ScriptName']}`",
        f"- Current driver UID: `{livery['driver_uid']}`",
        f"- New clone UID: `{plan['build']['new_uid']}`",
        f"- Recipient driver UID: `{plan['recipient_driver_uid']}`",
        "",
        "## Database links cloned",
        "",
    ]
    for a in conn["database"]["post_assignments"]:
        lines.append(f"- `{a['field']}` from `{a['source_table']}[{a['source_uid']}]`")
    lines += ["", "## Paint files", ""]
    for row in conn["paint_assets"]:
        if row.get("kind") == "outer_asset":
            lines.append(f"- Archive {row['archive']}: `{row['name']}` ({row['size']} bytes)")
    lines += ["", "## Preview resources", ""]
    for row in conn["preview_assets"]:
        if row.get("kind") == "inner_preview":
            lines.append(f"- Archive {row['archive']} / `{row['container']}` / `{row['entry']}`")
        elif row.get("kind") == "inner_preview_missing":
            lines.append(f"- Missing: `{row['entry']}`")
    lines += ["", "## What this test isolates", ""]
    lines += [f"- {text}" for text in conn["connection_conclusions"]]
    (base / CONNECTION_MD).write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_plan(ctx: Context, args: argparse.Namespace) -> tuple[dict[str, Any], bytes]:
    donor = find_record(ctx, "LIVERIE_c", args.donor_uid)
    rebuilt, build = build_patched_pyc(
        ctx, args.donor_uid, args.original_driver_uid,
        args.recipient_driver_uid, args.new_uid,
    )
    validation = validate_rebuild(
        ctx, rebuilt, args.donor_uid, args.original_driver_uid,
        args.recipient_driver_uid, build["new_uid"],
    )
    connections = connection_map(ctx, donor, build["new_uid"], args.recipient_driver_uid)
    plan = {
        "version": VERSION,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game": str(ctx.game),
        "archive": str(ctx.archive),
        "cdfiles": str(ctx.cdfiles),
        "pyc_entry": PYC_NAME,
        "pyc_original_offset": ctx.row.offset,
        "pyc_original_size": ctx.row.size,
        "pyc_sha256_before": sha256_bytes(ctx.pyc),
        "archive_sha256_before": sha256_file(ctx.archive),
        "donor_uid": args.donor_uid,
        "original_driver_uid": args.original_driver_uid,
        "recipient_driver_uid": args.recipient_driver_uid,
        "build": build,
        "validation": validation,
        "connections": connections,
    }
    return plan, rebuilt


def print_plan(plan: dict[str, Any]) -> None:
    clone = plan["validation"]["clone"]
    donor = plan["validation"]["donor_after"]
    print(f"NASCAR 15 In-Range True Extra Scheme Isolation Probe v{VERSION}")
    print(f"Game:             {plan['game']}")
    print(f"Original donor:   livery {donor['uid']} -> driver {donor['driver_uid']}")
    print(f"New clone:        livery {clone['uid']} -> driver {clone['driver_uid']}")
    print(f"ScriptName:       {clone['fields']['ScriptName']}")
    print(f"PYC growth:       {plan['build']['growth']} bytes")
    print(f"Registry timing:  clone inserted before master DATA build")
    print(f"Cloned links:     {', '.join(x['field'] for x in plan['build']['clone']['assignments'])}")
    if plan["build"].get("donor_restore"):
        print("Prior donor move: detected; the rebuilt PYC restores the donor to Brad and gives AJ the clone")
    print("Preview:          PAINTSCHEME_<new UID> is not created; blank preview is expected")


def ensure_backup(path: Path) -> Path:
    backup = Path(str(path) + BACKUP_SUFFIX)
    if backup.exists():
        return backup
    temp = Path(str(backup) + ".tmp")
    shutil.copyfile(path, temp)
    if temp.stat().st_size != path.stat().st_size:
        temp.unlink(missing_ok=True)
        raise ValueError("Backup size mismatch")
    os.replace(temp, backup)
    return backup


def cmd_analyze(args: argparse.Namespace) -> int:
    ctx = load_context(args.game)
    plan, _rebuilt = make_plan(ctx, args)
    print_plan(plan)
    (script_dir() / ANALYSIS_JSON).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    write_connection_files(plan)
    print(f"\n[+] Analysis: {script_dir() / ANALYSIS_JSON}")
    print(f"[+] Connection map: {script_dir() / CONNECTION_MD}")
    print(f"[+] Asset locations: {script_dir() / CONNECTION_CSV}")
    print("[dry-run] Nothing was changed.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    ctx = load_context(args.game)
    plan, rebuilt = make_plan(ctx, args)
    print_plan(plan)
    if find_cdf_row(ctx.cdfiles, PYC_NAME).offset != ctx.row.offset:
        raise ValueError("PYC cdfiles row changed during analysis")
    archive_backup = ensure_backup(ctx.archive)
    cdf_backup = ensure_backup(ctx.cdfiles)
    original_archive_size = ctx.archive.stat().st_size
    with ctx.archive.open("ab") as f:
        pad = (-f.tell()) % 16
        if pad:
            f.write(b"\0" * pad)
        new_offset = f.tell()
        f.write(rebuilt)
        f.flush()
        os.fsync(f.fileno())
    write_cdf_row(ctx.cdfiles, ctx.row, new_offset, len(rebuilt))

    # Verify through the live repointed entry and parse/map it again.
    live_row = find_cdf_row(ctx.cdfiles, PYC_NAME)
    if live_row.offset != new_offset or live_row.size != len(rebuilt):
        shutil.copyfile(cdf_backup, ctx.cdfiles)
        raise ValueError("cdfiles readback failed; cdfiles backup restored")
    with ctx.archive.open("rb") as f:
        f.seek(live_row.offset)
        live_pyc = f.read(live_row.size)
    if live_pyc != rebuilt:
        shutil.copyfile(cdf_backup, ctx.cdfiles)
        raise ValueError("Appended PYC readback failed; cdfiles backup restored")
    validation = validate_rebuild(
        ctx, live_pyc, args.donor_uid, args.original_driver_uid,
        args.recipient_driver_uid, plan["build"]["new_uid"],
    )
    manifest = dict(plan)
    manifest.update({
        "applied": True,
        "archive_backup": str(archive_backup),
        "cdfiles_backup": str(cdf_backup),
        "archive_original_size": original_archive_size,
        "pyc_new_offset": new_offset,
        "pyc_new_size": len(rebuilt),
        "live_validation": validation,
        "archive_sha256_after": sha256_file(ctx.archive),
        "cdfiles_sha256_after": sha256_file(ctx.cdfiles),
    })
    (script_dir() / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (script_dir() / ANALYSIS_JSON).write_text(json.dumps(plan, indent=2), encoding="utf-8")
    write_connection_files(plan)
    print("\n[+] New LIVERIE_c record inserted before DATA registry build and verified.")
    print(f"[+] Original donor remains with driver UID {args.original_driver_uid}.")
    print(f"[+] New livery UID {plan['build']['new_uid']} belongs to driver UID {args.recipient_driver_uid}.")
    print("\nTEST IN GAME:")
    print("  1. Start NASCAR 15 and select AJ Allmendinger.")
    print("  2. Open Paint Schemes. Look for a second scheme; its preview may be blank.")
    print("  3. Select the new scheme and start a race.")
    print("  4. Confirm the Brad Indianapolis paint loads.")
    print("  5. Check Brad still has his Indianapolis alternate.")
    print("  6. Close the game before Restore.")
    print("\nDo not run the old donor-slot restore while this test is applied.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    path = script_dir() / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"{MANIFEST_NAME} was not found; Apply has not completed")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    cdfiles = Path(manifest["cdfiles"])
    archive = Path(manifest["archive"])
    row = find_cdf_row(cdfiles, manifest["pyc_entry"])
    original_offset = int(manifest["pyc_original_offset"])
    original_size = int(manifest["pyc_original_size"])
    applied_offset = int(manifest["pyc_new_offset"])
    applied_size = int(manifest["pyc_new_size"])
    if row.offset == original_offset and row.size == original_size:
        print("[i] The original PYC is already active.")
        return 0
    if row.offset != applied_offset or row.size != applied_size:
        raise ValueError(
            "The live DB_GAME_LOCAL_SCRIPT.PYC row no longer matches this probe. "
            "Refusing to overwrite a later app edit."
        )
    write_cdf_row(cdfiles, row, original_offset, original_size)
    check = find_cdf_row(cdfiles, manifest["pyc_entry"])
    if check.offset != original_offset or check.size != original_size:
        raise ValueError("Restore cdfiles readback failed")
    with archive.open("rb") as f:
        f.seek(original_offset)
        old_pyc = f.read(original_size)
    if sha256_bytes(old_pyc) != manifest["pyc_sha256_before"]:
        raise ValueError("Original PYC bytes no longer match the pre-probe hash")
    print("[+] Restored the pre-probe DB_GAME_LOCAL_SCRIPT.PYC mapping.")
    print("[i] Appended test bytes remain orphaned at the end of ARCHIVE0; they are no longer indexed or loaded.")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    # Alias for analyze, useful when the user wants only the connection report.
    return cmd_analyze(args)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create one true new NASCAR 15 paint scheme record")
    p.add_argument("command", choices=["map", "analyze", "apply", "restore"])
    p.add_argument("--game", default=None, help="NASCAR 15 installation folder")
    p.add_argument("--donor-uid", type=int, default=DEFAULT_DONOR_UID)
    p.add_argument("--original-driver-uid", type=int, default=DEFAULT_ORIGINAL_DRIVER_UID)
    p.add_argument("--recipient-driver-uid", type=int, default=DEFAULT_RECIPIENT_DRIVER_UID)
    p.add_argument("--new-uid", type=int, default=DEFAULT_NEW_UID)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return {
        "map": cmd_map,
        "analyze": cmd_analyze,
        "apply": cmd_apply,
        "restore": cmd_restore,
    }[args.command](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise
