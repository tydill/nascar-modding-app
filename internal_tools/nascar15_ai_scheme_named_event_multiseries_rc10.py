#!/usr/bin/env python3
"""NASCAR 15 AI Scheme Named-Event Multi-Series Probe v0.5.

Corrects the v0.3/v0.4 runtime-bytecode design. v0.2 cloned WORLDSCRIPT/EVENT records, which made a
fake track selectable and used an invented WorldID that the engine could not
resolve reliably. v0.5 does not add or alter any track/world/event record.

Instead, it safely wraps EventInit.GetLiveryName. For the requested driver, it
asks the game's own GSRaceStoryFlowState for the live RACEDATA UID. At the target
named race it returns the requested livery ScriptName; otherwise it jumps back
into the original stock GetLiveryName bytecode unchanged.

Default proof:
  Named event key:   1002|S_EVT_DAYTONA_50
  RACEDATA UID set:  auto-discovered across 2012-2015
  Driver UID:        1083 (AJ Allmendinger)
  Livery UID:        25582

No WORLDSCRIPT, EVENT, RACEDATA, track menu, paint asset, or preview is changed.

v0.3/v0.4 also emitted opcode 114 as POP_JUMP_IF_FALSE. NASCAR 15 uses Python 2.5 magic 62131, where opcode 114 is undefined. v0.5 uses the native 2.5 JUMP_IF_FALSE + POP_TOP pattern and matches every RACEDATA record carrying the app's named-event key.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import struct
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / "nascar15_inrange_true_extra_scheme_probe_v0_4_base_rc10.py"
spec = importlib.util.spec_from_file_location("n15_event_runtime_base", str(BASE_PATH))
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load NASCAR 15 helpers")
base = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = base
spec.loader.exec_module(base)

VERSION = "0.5"
EVENTINIT = "EVENTINIT.PYC"
DBPYC = "DB_GAME_LOCAL_SCRIPT.PYC"
MANIFEST = "ai_scheme_named_event_multiseries_v0_5_manifest.json"
ANALYSIS = "ai_scheme_named_event_multiseries_v0_5_analysis.json"
ALIGNMENT = 16

DEFAULT_EVENT_UID = 1002
DEFAULT_EVENT_TOKEN = "S_EVT_DAYTONA_50"
DEFAULT_DRIVER_UID = 1083
DEFAULT_LIVERY_UID = 25582

# Python 2.5 opcodes used by this file.
POP_TOP = 1
RETURN_VALUE = 83
LOAD_CONST = 100
LOAD_NAME = 101
BUILD_TUPLE = 102
LOAD_ATTR = 105
COMPARE_OP = 106
IMPORT_NAME = 107
JUMP_ABSOLUTE = 113
JUMP_IF_FALSE = 111
LOAD_GLOBAL = 116
LOAD_FAST = 124
CALL_FUNCTION = 131
HAVE_ARGUMENT = 90


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def align_up(v: int, a: int = ALIGNMENT) -> int:
    return (v + a - 1) & ~(a - 1)


def emit(op: int, arg: int | None = None) -> bytes:
    if arg is None:
        return bytes([op])
    if not 0 <= int(arg) <= 0xFFFF:
        raise ValueError(f"Operand {arg} needs EXTENDED_ARG")
    return bytes([op, int(arg) & 0xFF, (int(arg) >> 8) & 0xFF])


def marshal_scalar(value: Any) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"T" if value else b"F"
    if isinstance(value, int):
        return b"i" + struct.pack("<i", value)
    if isinstance(value, str):
        raw = value.encode("ascii", "strict")
        # Deliberately non-interned: inserting this does not perturb the PYC's
        # existing marshal string-reference table.
        return b"s" + struct.pack("<i", len(raw)) + raw
    raise TypeError(f"Unsupported marshal scalar {value!r}")


def skip_obj(data: bytes, pos: int) -> tuple[int, int]:
    """Return (tag_start, end) for one Python-2 marshal object."""
    start = pos
    if pos >= len(data):
        raise EOFError("marshal EOF")
    tag = chr(data[pos] & 0x7F)
    pos += 1
    if tag in "0NFTS.":
        return start, pos
    if tag == "i":
        return start, pos + 4
    if tag in "Igxy":
        return start, pos + (8 if tag in "Ig" else 16)
    if tag == "f":
        n = data[pos]
        return start, pos + 1 + n
    if tag == "x":
        n1 = data[pos]
        pos += 1 + n1
        n2 = data[pos]
        return start, pos + 1 + n2
    if tag == "l":
        n = struct.unpack_from("<i", data, pos)[0]
        return start, pos + 4 + abs(n) * 2
    if tag in "stu":
        n = struct.unpack_from("<i", data, pos)[0]
        return start, pos + 4 + n
    if tag == "R":
        return start, pos + 4
    if tag in "([<>":
        n = struct.unpack_from("<i", data, pos)[0]
        pos += 4
        for _ in range(n):
            _, pos = skip_obj(data, pos)
        return start, pos
    if tag == ")":
        n = data[pos]
        pos += 1
        for _ in range(n):
            _, pos = skip_obj(data, pos)
        return start, pos
    if tag == "{":
        while True:
            kstart, kend = skip_obj(data, pos)
            if chr(data[kstart] & 0x7F) == "0":
                return start, kend
            _, pos = skip_obj(data, kend)
    if tag == "c":
        pos += 16  # argcount, nlocals, stacksize, flags
        for _ in range(9):  # code,consts,names,varnames,freevars,cellvars,filename,name,lnotab(with firstlineno before it)
            if _ == 8:
                pos += 4
            _, pos = skip_obj(data, pos)
        return start, pos
    raise ValueError(f"Unsupported marshal tag {tag!r} at 0x{start:X}")


def find_code_mval(mapper: Any, root: Any, name: str) -> Any:
    hits = []

    def walk(v: Any) -> None:
        if isinstance(v, mapper.MVal):
            if isinstance(v.value, mapper.CodeObj):
                if v.value.name == name:
                    hits.append(v)
                for c in v.value.consts:
                    walk(c)
            elif isinstance(v.value, list):
                for x in v.value:
                    walk(x)

    walk(root)
    if len(hits) != 1:
        raise ValueError(f"Expected one code object {name}; found {len(hits)}")
    return hits[0]


def code_layout(data: bytes, mapper: Any, code_name: str) -> dict[str, int]:
    root = mapper.parse_pyc(data)
    mv = find_code_mval(mapper, root, code_name)
    p = mv.tag_offset + 1
    argcount_pos = p
    p += 16
    code_tag, code_end = skip_obj(data, p)
    if chr(data[code_tag] & 0x7F) not in "st":
        raise ValueError("co_code is not a marshal string")
    code_len = struct.unpack_from("<i", data, code_tag + 1)[0]
    code_payload = code_tag + 5
    if code_payload + code_len != code_end:
        raise ValueError("co_code layout mismatch")
    consts_tag, consts_end = skip_obj(data, code_end)
    if chr(data[consts_tag] & 0x7F) != "(":
        raise ValueError("co_consts is not a tuple")
    names_tag, names_end = skip_obj(data, consts_end)
    if chr(data[names_tag] & 0x7F) != "(":
        raise ValueError("co_names is not a tuple")
    return {
        "code_mval_tag": mv.tag_offset,
        "argcount_pos": argcount_pos,
        "code_tag": code_tag,
        "code_len_pos": code_tag + 1,
        "code_payload": code_payload,
        "code_len": code_len,
        "code_end": code_end,
        "consts_tag": consts_tag,
        "consts_count_pos": consts_tag + 1,
        "consts_count": struct.unpack_from("<i", data, consts_tag + 1)[0],
        "consts_end": consts_end,
        "names_tag": names_tag,
        "names_count_pos": names_tag + 1,
        "names_count": struct.unpack_from("<i", data, names_tag + 1)[0],
        "names_end": names_end,
    }


def code_object(mapper: Any, data: bytes, name: str) -> Any:
    root = mapper.parse_pyc(data)
    return find_code_mval(mapper, root, name).value


def ensure_const(mapper: Any, data: bytes, code_name: str, value: Any) -> tuple[bytes, int, bool]:
    co = code_object(mapper, data, code_name)
    for i, c in enumerate(co.consts):
        plain = mapper.value_plain_for_compare(c)
        if type(plain) is type(value) and plain == value:
            return data, i, False
    layout = code_layout(data, mapper, code_name)
    idx = layout["consts_count"]
    if idx > 0xFFFF:
        raise ValueError("co_consts exceeded 16-bit operand range")
    enc = marshal_scalar(value)
    out = bytearray(data)
    struct.pack_into("<i", out, layout["consts_count_pos"], idx + 1)
    out[layout["consts_end"]:layout["consts_end"]] = enc
    # Full marshal reparse is the readback guard.
    co2 = code_object(mapper, bytes(out), code_name)
    if len(co2.consts) != idx + 1 or mapper.value_plain_for_compare(co2.consts[idx]) != value:
        raise ValueError(f"Failed to append constant {value!r}")
    return bytes(out), idx, True


def ensure_name(mapper: Any, data: bytes, code_name: str, value: str) -> tuple[bytes, int, bool]:
    co = code_object(mapper, data, code_name)
    if value in co.names:
        return data, co.names.index(value), False
    layout = code_layout(data, mapper, code_name)
    idx = layout["names_count"]
    if idx > 0xFFFF:
        raise ValueError("co_names exceeded 16-bit operand range")
    enc = marshal_scalar(value)
    out = bytearray(data)
    struct.pack_into("<i", out, layout["names_count_pos"], idx + 1)
    out[layout["names_end"]:layout["names_end"]] = enc
    co2 = code_object(mapper, bytes(out), code_name)
    if len(co2.names) != idx + 1 or co2.names[idx] != value:
        raise ValueError(f"Failed to append name {value}")
    return bytes(out), idx, True


def assemble_override(*, old_len: int, driver_fast: int, driver_const: int,
                      minus1_const: int, none_const: int, race_uid_consts: list[int],
                      script_const: int, module_name: int, class_name: int,
                      instance_name: int, current_name: int, getuid_name: int,
                      range_name: int) -> bytes:
    """Build a Python-2.5-compatible guard block.

    NASCAR 15 uses PYC magic 62131 (Python 2.5c2). Python 2.5 has
    JUMP_IF_FALSE (relative, leaves the tested value on the stack) and does
    NOT have POP_JUMP_IF_FALSE. Each successful guard therefore emits
    JUMP_IF_FALSE + POP_TOP, while all false paths land on one POP_TOP
    before resuming the untouched stock function.
    """
    chunks: list[bytes | tuple[str, str]] = []
    labels: dict[str, int] = {}

    def add(b: bytes) -> None:
        chunks.append(b)

    def guard_false(label: str = "fallback") -> None:
        chunks.append(("jump_false_rel", label))
        add(emit(POP_TOP))

    add(emit(LOAD_FAST, driver_fast))
    add(emit(LOAD_CONST, driver_const))
    add(emit(COMPARE_OP, 2))
    guard_false()

    add(emit(LOAD_CONST, minus1_const))
    add(emit(LOAD_CONST, none_const))
    add(emit(IMPORT_NAME, module_name))
    add(emit(LOAD_ATTR, class_name))
    add(emit(LOAD_ATTR, instance_name))
    add(emit(CALL_FUNCTION, 0))
    guard_false()

    add(emit(LOAD_CONST, minus1_const))
    add(emit(LOAD_CONST, none_const))
    add(emit(IMPORT_NAME, module_name))
    add(emit(LOAD_ATTR, class_name))
    add(emit(LOAD_ATTR, instance_name))
    add(emit(CALL_FUNCTION, 0))
    add(emit(LOAD_ATTR, current_name))
    add(emit(CALL_FUNCTION, 0))
    guard_false()

    add(emit(LOAD_CONST, minus1_const))
    add(emit(LOAD_CONST, none_const))
    add(emit(IMPORT_NAME, module_name))
    add(emit(LOAD_ATTR, class_name))
    add(emit(LOAD_ATTR, instance_name))
    add(emit(CALL_FUNCTION, 0))
    add(emit(LOAD_ATTR, current_name))
    add(emit(CALL_FUNCTION, 0))
    add(emit(LOAD_ATTR, getuid_name))
    add(emit(CALL_FUNCTION, 0))
    for ci in race_uid_consts:
        add(emit(LOAD_CONST, ci))
    add(emit(BUILD_TUPLE, len(race_uid_consts)))
    add(emit(COMPARE_OP, 6))  # in
    guard_false()

    add(emit(LOAD_CONST, script_const))
    add(emit(RETURN_VALUE))

    chunks.append(("label", "fallback"))
    # A failed Python-2.5 JUMP_IF_FALSE leaves its tested value on stack.
    add(emit(POP_TOP))
    add(emit(LOAD_GLOBAL, range_name))
    add(emit(JUMP_ABSOLUTE, 3))

    # Resolve labels and relative jump distances. The relative operand is
    # counted from the instruction immediately after JUMP_IF_FALSE.
    pos = 0
    positions: list[int] = []
    for c in chunks:
        positions.append(pos)
        if isinstance(c, tuple):
            if c[0] == "label":
                labels[c[1]] = pos
            else:
                pos += 3
        else:
            pos += len(c)
    out = bytearray()
    for c, rel_pos in zip(chunks, positions):
        if isinstance(c, tuple):
            if c[0] == "label":
                continue
            target = labels[c[1]]
            delta = target - (rel_pos + 3)
            if not 0 <= delta <= 0xFFFF:
                raise ValueError(f"Python 2.5 relative jump delta out of range: {delta}")
            out += emit(JUMP_IF_FALSE, delta)
        else:
            out += c
    return bytes(out)


def patch_eventinit(mapper: Any, pyc: bytes, driver_uid: int, racedata_uids: list[int],
                    script_name: str) -> tuple[bytes, dict[str, Any]]:
    original_root = mapper.parse_pyc(pyc)
    original_co = find_code_mval(mapper, original_root, "GetLiveryName").value
    if original_co.code_bytes[:3] != emit(LOAD_GLOBAL, original_co.names.index("range")):
        raise ValueError("GetLiveryName is already patched. Restore the active v0.3/v0.4 named-race probe first.")

    out = pyc
    appended_consts = []
    const_indices = {}
    for key, value in (
        ("driver", driver_uid),
        ("minus1", -1),
        ("none", None),
        ("script", script_name),
    ):
        out, idx, added = ensure_const(mapper, out, "GetLiveryName", value)
        const_indices[key] = idx
        if added:
            appended_consts.append(value)
    race_const_indices=[]
    for racedata_uid in racedata_uids:
        out, idx, added = ensure_const(mapper, out, "GetLiveryName", int(racedata_uid))
        race_const_indices.append(idx)
        if added:
            appended_consts.append(int(racedata_uid))

    appended_names = []
    name_indices = {}
    for key, value in (
        ("module", "GSRaceStoryFlowState"),
        ("class", "GSRaceStoryFlowState_c"),
        ("instance", "Instance"),
        ("current", "GetCurrentRaceData"),
        ("getuid", "GetUID"),
    ):
        out, idx, added = ensure_name(mapper, out, "GetLiveryName", value)
        name_indices[key] = idx
        if added:
            appended_names.append(value)

    co = code_object(mapper, out, "GetLiveryName")
    old_len = len(co.code_bytes)
    override = assemble_override(
        old_len=old_len,
        driver_fast=co.varnames.index("driverID"),
        driver_const=const_indices["driver"],
        minus1_const=const_indices["minus1"],
        none_const=const_indices["none"],
        race_uid_consts=race_const_indices,
        script_const=const_indices["script"],
        module_name=name_indices["module"],
        class_name=name_indices["class"],
        instance_name=name_indices["instance"],
        current_name=name_indices["current"],
        getuid_name=name_indices["getuid"],
        range_name=co.names.index("range"),
    )

    if pyc[:2] != struct.pack("<H", 62131):
        raise ValueError(f"Unexpected PYC magic {int.from_bytes(pyc[:2], 'little')}; expected Python 2.5c2 magic 62131")
    # Parse the injected block using the actual 2.5 instruction widths and
    # reject the undefined 114/115 opcodes that broke v0.3/v0.4.
    pcheck = 0
    seen_ops = []
    while pcheck < len(override):
        op = override[pcheck]
        seen_ops.append(op)
        pcheck += 3 if op >= HAVE_ARGUMENT else 1
    if pcheck != len(override):
        raise ValueError("Injected Python 2.5 bytecode has a truncated instruction")
    if 114 in seen_ops or 115 in seen_ops:
        raise ValueError("Injected block contains Python-2.7-only POP_JUMP opcodes")


    layout = code_layout(out, mapper, "GetLiveryName")
    code = bytearray(co.code_bytes)
    code[:3] = emit(JUMP_ABSOLUTE, old_len)
    code += override
    rebuilt = bytearray(out)
    struct.pack_into("<i", rebuilt, layout["code_len_pos"], len(code))
    rebuilt[layout["code_payload"]:layout["code_end"]] = code
    rebuilt = bytes(rebuilt)

    # Structural validation.
    new_root = mapper.parse_pyc(rebuilt)
    new_co = find_code_mval(mapper, new_root, "GetLiveryName").value
    if len(new_co.code_bytes) != old_len + len(override):
        raise ValueError("GetLiveryName code growth mismatch")
    if new_co.code_bytes[:3] != emit(JUMP_ABSOLUTE, old_len):
        raise ValueError("Entry redirect was not installed")
    if new_co.code_bytes[3:old_len] != original_co.code_bytes[3:]:
        raise ValueError("Original GetLiveryName body changed outside the 3-byte entry redirect")
    if new_co.code_bytes[old_len:] != override:
        raise ValueError("Override readback mismatch")

    # Confirm no other code object's bytecode changed.
    def code_map(root: Any) -> dict[str, bytes]:
        result = {}
        for co2 in mapper.walk_code_objects(root):
            result.setdefault(co2.name, co2.code_bytes)
        return result

    before_map = code_map(original_root)
    after_map = code_map(new_root)
    changed = [k for k in before_map if before_map[k] != after_map.get(k)]
    if changed != ["GetLiveryName"]:
        raise ValueError(f"Unexpected code objects changed: {changed}")

    return rebuilt, {
        "old_code_length": old_len,
        "new_code_length": len(new_co.code_bytes),
        "override_bytes": len(override),
        "appended_constants": appended_consts,
        "appended_names": appended_names,
        "driver_uid": driver_uid,
        "racedata_uids": list(racedata_uids),
        "script_name": script_name,
        "fallback": "original stock GetLiveryName resumes at byte 3",
        "bytecode_version": "Python 2.5c2 / magic 62131",
        "conditional_opcode": "JUMP_IF_FALSE (111) + POP_TOP",
    }


def load_eventinit(game_arg: str | None) -> tuple[Any, Path, Path, Any, bytes, Any]:
    game = base.detect_game(game_arg)
    archive, cdfiles = base.archive0_paths(game)
    mapper, patcher, _containers = base.load_modules()
    entries = mapper.load_entries(cdfiles, patcher)
    entry = mapper.find_entry(entries, EVENTINIT)
    row = base.find_cdf_row(cdfiles, EVENTINIT)
    if entry.offset != row.offset or entry.size != row.size:
        raise ValueError("Mapper and direct cdf parser disagree on EVENTINIT.PYC")
    with archive.open("rb") as f:
        f.seek(row.offset)
        pyc = f.read(row.size)
    if len(pyc) != row.size:
        raise ValueError("Short EVENTINIT.PYC read")
    mapper.parse_pyc(pyc)
    return game, archive, cdfiles, row, pyc, mapper


def validate_database(game_arg: str | None, args: argparse.Namespace) -> dict[str, Any]:
    ctx = base.load_context(game_arg)
    matches=[]
    for race in base.records_of(ctx, "RACEDATA_c"):
        try:
            token = base.display(ctx, race.fields.get("EventName"))
            event_uid = base.field_uid(ctx, race, "RaceEvent")
            if token == args.event_token and event_uid == args.event_uid:
                matches.append({
                    "racedata_uid": base.pointer_int(ctx, race.uid),
                    "series": base.display(ctx, race.fields.get("RaceSeries")),
                    "number_in_series": base.pointer_int(ctx, race.fields.get("NumberInSeries")),
                })
        except Exception:
            continue
    matches=sorted(matches,key=lambda x:x["racedata_uid"])
    if not matches:
        raise ValueError(f"No RACEDATA_c records match {args.event_uid}|{args.event_token}")

    liv = base.find_record(ctx, "LIVERIE_c", args.livery_uid)
    if base.field_uid(ctx, liv, "Driver") != args.driver_uid:
        raise ValueError("Requested livery does not belong to requested driver")
    script_name = base.display(ctx, liv.fields.get("ScriptName"))

    new_world = [r for r in base.records_of(ctx, "WORLDSCRIPT_c") if base.pointer_int(ctx, r.uid) == 24000]
    new_event = [r for r in base.records_of(ctx, "EVENT_c") if base.pointer_int(ctx, r.uid) == 24002]
    if new_world or new_event:
        raise ValueError(
            "The v0.2 cloned-world probe is still active. Run "
            "RESTORE_NAMED_RACE_AI_ASSIGNMENT.bat from v0.2 first."
        )
    return {
        "event_uid": args.event_uid,
        "event_token": args.event_token,
        "racedata_matches": matches,
        "racedata_uids": [x["racedata_uid"] for x in matches],
        "driver_uid": args.driver_uid,
        "livery_uid": args.livery_uid,
        "script_name": script_name,
    }


def manifest_path() -> Path:
    return HERE / MANIFEST


def analysis_path() -> Path:
    return HERE / ANALYSIS


def build_plan(args: argparse.Namespace) -> tuple[dict[str, Any], bytes, tuple[Any, Path, Path, Any, bytes, Any]]:
    db = validate_database(args.game, args)
    loaded = load_eventinit(args.game)
    game, archive, cdfiles, row, pyc, mapper = loaded
    rebuilt, patch_meta = patch_eventinit(
        mapper, pyc, args.driver_uid, db["racedata_uids"], db["script_name"]
    )
    plan = {
        "version": VERSION,
        "game": str(game),
        "database": db,
        "eventinit": {
            "old_offset": row.offset,
            "old_size": row.size,
            "old_sha256": sha256_bytes(pyc),
            "new_size": len(rebuilt),
            "new_sha256": sha256_bytes(rebuilt),
            "growth": len(rebuilt) - len(pyc),
        },
        "patch": patch_meta,
        "changes": [
            "EVENTINIT.GetLiveryName entry redirect + Python-2.5-safe race/driver override",
            "No DB records, worlds, events, tracks, paints, or previews changed",
        ],
        "test": {
            "target": "Daytona 500 named event across all stock Cup series",
            "expected": "AI AJ uses livery UID 25582 whenever current RACEDATA UID belongs to 1002|S_EVT_DAYTONA_50",
            "elsewhere": "Original stock GetLiveryName logic runs unchanged",
        },
    }
    return plan, rebuilt, loaded


def print_plan(plan: dict[str, Any]) -> None:
    db = plan["database"]
    ev = plan["eventinit"]
    print(f"NASCAR 15 AI Scheme Named-Event Multi-Series Probe v{VERSION}")
    print(f"Game:               {plan['game']}")
    print(f"Named race key:     {db['event_uid']}|{db['event_token']}")
    print(f"RACEDATA UID set:   {db['racedata_uids']}")
    print("Runtime selector:    GetCurrentRaceData().GetUID()")
    print("Bytecode branch:      Python 2.5 JUMP_IF_FALSE (opcode 111)")
    print(f"Driver UID:         {db['driver_uid']}")
    print(f"Livery UID:         {db['livery_uid']}")
    print(f"ScriptName:         {db['script_name']}")
    print(f"EVENTINIT.PYC:      {ev['old_size']} -> {ev['new_size']} bytes (+{ev['growth']})")
    print("Track/world records: unchanged")
    print("Fallback:           original GetLiveryName bytecode")


def cmd_analyze(args: argparse.Namespace) -> int:
    plan, _rebuilt, _loaded = build_plan(args)
    analysis_path().write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print_plan(plan)
    print(f"\n[+] Analysis passed. Report: {analysis_path().name}")
    print("[+] v0.2 cloned-world records are absent.")
    print("[+] No fake track slot will be created by this probe.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    if manifest_path().exists():
        raise ValueError("A v0.5 named-event test is already active. Run restore first.")
    plan, rebuilt, loaded = build_plan(args)
    game, archive, cdfiles, row, pyc, mapper = loaded
    old_archive_size = archive.stat().st_size
    new_offset = align_up(old_archive_size)
    with archive.open("ab") as f:
        f.write(b"\0" * (new_offset - old_archive_size))
        f.write(rebuilt)
        f.flush()
        os.fsync(f.fileno())
    base.write_cdf_row(cdfiles, row, new_offset, len(rebuilt))

    # Live readback from the repointed row.
    live_row = base.find_cdf_row(cdfiles, EVENTINIT)
    if live_row.offset != new_offset or live_row.size != len(rebuilt):
        raise ValueError("EVENTINIT cdf repoint readback failed")
    with archive.open("rb") as f:
        f.seek(live_row.offset)
        live = f.read(live_row.size)
    if live != rebuilt:
        raise ValueError("Live EVENTINIT readback mismatch")
    mapper.parse_pyc(live)

    manifest = {
        "version": VERSION,
        "game": str(game),
        "archive": str(archive),
        "cdfiles": str(cdfiles),
        "pre_row": {"offset": row.offset, "size": row.size},
        "pre_pyc_sha256": sha256_bytes(pyc),
        "post_row": {"offset": new_offset, "size": len(rebuilt)},
        "post_pyc_sha256": sha256_bytes(rebuilt),
        "archive_size_before": old_archive_size,
        "archive_size_after": archive.stat().st_size,
        "created": int(time.time()),
        "plan": plan,
    }
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print_plan(plan)
    print("\n[+] Python-2.5-safe EVENTINIT named-event override installed and read back successfully.")
    print("[+] No WORLDSCRIPT/EVENT/RACEDATA record was added or changed.")
    print("\nTEST IN GAME:")
    print("  1. Start a NEW Single Season in any available Cup year.")
    print("  2. Run the Daytona 500 as anyone except AJ.")
    print("  3. AI #47 should use added livery UID 25582.")
    print("  4. Check another race: AJ should use normal stock selection.")
    print("  5. There should be NO extra Daytona track slot.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    if not manifest_path().exists():
        raise ValueError("No active v0.5 manifest was found")
    m = json.loads(manifest_path().read_text(encoding="utf-8"))
    game = Path(m["game"])
    archive, cdfiles = base.archive0_paths(game)
    live_row = base.find_cdf_row(cdfiles, EVENTINIT)
    post = m["post_row"]
    if live_row.offset != post["offset"] or live_row.size != post["size"]:
        raise ValueError("EVENTINIT was repointed after v0.5; refusing to overwrite a later modification")
    pre = m["pre_row"]
    base.write_cdf_row(cdfiles, live_row, int(pre["offset"]), int(pre["size"]))
    check = base.find_cdf_row(cdfiles, EVENTINIT)
    if check.offset != pre["offset"] or check.size != pre["size"]:
        raise ValueError("Restore cdf readback failed")
    with archive.open("rb") as f:
        f.seek(check.offset)
        old = f.read(check.size)
    if sha256_bytes(old) != m["pre_pyc_sha256"]:
        raise ValueError("Restored EVENTINIT row does not match the pre-probe PYC")
    manifest_path().unlink()
    print("[+] EVENTINIT.PYC repointed back to its exact pre-v0.5 version.")
    print("[+] Archive0 was not truncated; appended test bytes are harmless orphaned data.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=("analyze", "apply", "restore"))
    ap.add_argument("--game")
    ap.add_argument("--event-uid", type=int, default=DEFAULT_EVENT_UID)
    ap.add_argument("--event-token", default=DEFAULT_EVENT_TOKEN)
    ap.add_argument("--driver-uid", type=int, default=DEFAULT_DRIVER_UID)
    ap.add_argument("--livery-uid", type=int, default=DEFAULT_LIVERY_UID)
    args = ap.parse_args()
    try:
        return {"analyze": cmd_analyze, "apply": cmd_apply, "restore": cmd_restore}[args.command](args)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
