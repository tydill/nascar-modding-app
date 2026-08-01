#!/usr/bin/env python3
"""NASCAR 15 Native ApplyPatch Extra Scheme Probe v0.9.

This probe tests the game's native DLC-style livery insertion route instead of
adding a 337th record directly to the generated base __LIVERIE table.

Why this route matters
----------------------
Stock DLC scripts create new LIVERIE_c objects in a small patch dictionary and
call DB.ApplyPatch(DATA). The base database's ApplyPatch function then inserts
those objects into GameDB.DATA after the generated base tables are complete.

v0.9 reproduces that route inside DB_GAME_LOCAL_SCRIPT.PYC itself:

    base __LIVERIE table remains 336 records
    master DATA is fully built
    ApplyPatch({'LIVERIE': {25582: LIVERIE_c(...)}}) runs afterward

It also installs matching unique SD/HD paint entries copied from Brad
Keselowski's Indianapolis alternate. No preview is added; a blank thumbnail is
expected.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
V06_PATH = HERE / "nascar15_complete_asset_backed_extra_scheme_probe_v0_6.py"
if not V06_PATH.exists():
    raise FileNotFoundError(f"Missing dependency: {V06_PATH.name}")
spec = importlib.util.spec_from_file_location("n15_v06_helpers", str(V06_PATH))
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load v0.6 helpers")
v06 = importlib.util.module_from_spec(spec)
sys.modules["n15_v06_helpers"] = v06
spec.loader.exec_module(v06)
base = v06.base

VERSION = "0.9"
DEFAULT_NEW_SCRIPT = "15_47_AJ_EXTRA_SLOT_TEST"
DEFAULT_DONOR_SCRIPT = "15_2_BRAD_KESELOWSKI_BEER_1"
DEFAULT_NEW_UID = 25582
DEFAULT_DONOR_UID = 25580
DEFAULT_ORIGINAL_DRIVER_UID = 1115
DEFAULT_RECIPIENT_DRIVER_UID = 1083

MANIFEST_NAME = "native_applypatch_extra_scheme_v0_9_manifest.json"
ANALYSIS_NAME = "native_applypatch_extra_scheme_v0_9_analysis.json"
BACKUP_SUFFIX = ".native_applypatch_extra_scheme_v0_9.bak"
ARCHIVE0_ALIGNMENT = 16
ARCHIVE2_ALIGNMENT = 8


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
            block = f.read(min(chunk := 8 * 1024 * 1024, remaining))
            if not block:
                raise ValueError(f"Short read from {path.name} at 0x{offset:X}")
            h.update(block)
            remaining -= len(block)
    return h.hexdigest()


def marshal_interned_string(text: str) -> bytes:
    raw = text.encode("ascii", "strict")
    return b"t" + struct.pack("<i", len(raw)) + raw


def const_index(ctx: Any, value: Any) -> int:
    hits: list[int] = []
    for i, item in enumerate(ctx.root.value.consts):
        plain = ctx.mapper.value_plain_for_compare(item)
        if plain == value and type(plain) is type(value):
            hits.append(i)
    if not hits:
        raise ValueError(f"Root constant not found: {value!r}")
    return hits[0]


def name_index(ctx: Any, name: str) -> int:
    try:
        return ctx.root.value.names.index(name)
    except ValueError as exc:
        raise ValueError(f"Root name not found: {name}") from exc


def require_clean_base(ctx: Any, new_uid: int, new_script: str) -> None:
    liveries = base.records_of(ctx, "LIVERIE_c")
    if len(liveries) != 336:
        raise ValueError(
            f"Expected the restored 336-record base database; found {len(liveries)}. "
            "Restore the earlier extra-scheme probe first."
        )
    used = {base.pointer_int(ctx, r.uid) for r in liveries}
    if new_uid in used:
        raise ValueError(f"Livery UID {new_uid} is already used in the base database")
    scripts = [base.display(ctx, r.fields.get("ScriptName")) for r in liveries]
    if len(scripts) != len(set(scripts)):
        raise ValueError("The restored base database already has duplicate ScriptNames")
    if new_script in scripts:
        raise ValueError(f"ScriptName {new_script!r} is already active")


def locate_constructor(ctx: Any, donor: Any) -> tuple[list[dict[str, Any]], int, int]:
    co = ctx.root.value
    instructions = base.py2_instructions(ctx, co.code_bytes)
    call_i = next(
        (i for i, item in enumerate(instructions)
         if item["offset"] == donor.call_offset and item["opname"] == "CALL_FUNCTION"),
        None,
    )
    if call_i is None or instructions[call_i]["arg"] != 17:
        raise ValueError("Could not locate donor LIVERIE_c CALL_FUNCTION 17")
    donor_uid = base.pointer_int(ctx, donor.uid)
    start_i = None
    for i in range(call_i - 1, -1, -1):
        item = instructions[i]
        if item["opname"] != "LOAD_NAME" or item["arg"] is None:
            continue
        if co.names[item["arg"]] != "LIVERIE_c":
            continue
        if i + 1 < len(instructions):
            nxt = instructions[i + 1]
            if nxt["opname"] == "LOAD_CONST" and base.const_plain(ctx, nxt["arg"]) == donor_uid:
                start_i = i
                break
    if start_i is None:
        raise ValueError("Could not locate donor constructor start")
    return instructions[start_i:call_i + 1], start_i, call_i


def build_constructor_with_live_links(
    ctx: Any,
    donor: Any,
    uid_const_index: int,
    script_const_index: int,
    recipient_driver_uid: int,
    recipient_world_uid: int | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Clone donor constructor but supply Driver/Package/World/Season directly.

    The stock base constructor initially uses None for these four fields and
    patches them later. Because v0.8 executes after DATA is fully built, the
    native table objects are already available and can be passed directly.
    """
    co = ctx.root.value
    ctor_instructions, _start_i, _call_i = locate_constructor(ctx, donor)
    donor_uid = base.pointer_int(ctx, donor.uid)
    donor_script = base.display(ctx, donor.fields.get("ScriptName"))

    assignment_rows = base.post_assignment_blocks(ctx, donor_uid)
    by_field = {row["field"]: row for row in assignment_rows}
    required_order = ["Driver", "Package", "World", "Season"]
    if not all(field in by_field for field in required_order):
        raise ValueError(f"Donor post-assignment map is incomplete: {sorted(by_field)}")

    sources: list[tuple[str, int, str]] = []
    for field in required_order:
        row = by_field[field]
        if field == "Driver":
            source_uid = recipient_driver_uid
        elif field == "World" and recipient_world_uid is not None:
            # The donor is a stock DLC livery belonging to ANOTHER driver, and
            # its World was previously inherited wholesale. That stamped every
            # app-created scheme with the donor's track, which both hid it at
            # every other track and broke the donor's track on load, because
            # the record was enumerated for an event it had no valid entry for.
            source_uid = int(recipient_world_uid)
        else:
            source_uid = int(row["source_uid"])
        sources.append((str(row["source_table"]), source_uid, field))

    out = bytearray()
    uid_hits = 0
    script_hits = 0
    none_hits = 0
    replacements: list[dict[str, Any]] = []

    for item in ctor_instructions:
        size = 3 if item["arg"] is not None else 1
        raw = co.code_bytes[item["offset"]:item["offset"] + size]

        if item["opname"] == "LOAD_CONST":
            value = base.const_plain(ctx, item["arg"])
            if value == donor_uid:
                raw = base.emit(100, uid_const_index)
                uid_hits += 1
            elif value == donor_script:
                raw = base.emit(100, script_const_index)
                script_hits += 1

        elif item["opname"] == "LOAD_NAME" and item["arg"] is not None:
            if co.names[item["arg"]] == "None":
                none_hits += 1
                if none_hits <= 4:
                    table_name, source_uid, field = sources[none_hits - 1]
                    raw = (
                        base.emit(101, name_index(ctx, table_name))
                        + base.emit(100, const_index(ctx, source_uid))
                        + base.emit(25)  # BINARY_SUBSCR
                    )
                    replacements.append({
                        "field": field,
                        "source_table": table_name,
                        "source_uid": source_uid,
                    })

        out += raw

    if uid_hits != 1:
        raise ValueError(f"Expected one constructor UID operand; found {uid_hits}")
    if script_hits != 1:
        raise ValueError(f"Expected one constructor ScriptName operand; found {script_hits}")
    if none_hits != 5:
        raise ValueError(
            f"Expected five None arguments in donor constructor "
            f"(four links plus ManufacturerOverride); found {none_hits}"
        )
    if [x["field"] for x in replacements] != required_order:
        raise ValueError("Constructor link replacement order is not Driver/Package/World/Season")

    return bytes(out), {
        "donor_call_offset": donor.call_offset,
        "donor_uid": donor_uid,
        "donor_script_name": donor_script,
        "constructor_bytes": len(out),
        "uid_operand_count": uid_hits,
        "script_operand_count": script_hits,
        "none_operand_count": none_hits,
        "live_link_replacements": replacements,
    }


def build_applypatch_code(
    ctx: Any,
    donor: Any,
    new_uid: int,
    new_script: str,
    recipient_driver_uid: int,
    recipient_world_uid: int | None = None,
) -> tuple[bytes, dict[str, Any], bytes]:
    co = ctx.root.value
    if len(co.consts) + 2 >= 0xFFFF:
        raise ValueError("Root constant table is too large for two new 16-bit operands")
    uid_const_index = len(co.consts)
    script_const_index = len(co.consts) + 1

    constructor, constructor_meta = build_constructor_with_live_links(
        ctx,
        donor,
        uid_const_index,
        script_const_index,
        recipient_driver_uid,
        recipient_world_uid,
    )

    code = bytearray()

    # Critical stock-DLC behavior: once DB_GAME_LOCAL_SCRIPT finishes creating
    # its base records, BaseGDTObject_c.__setattr__ has been switched to
    # ErrorSetAttr.  A late-created LIVERIE_c therefore cannot initialize its
    # fields unless patch-mode assignment is enabled first.  Stock DLC modules
    # perform this exact switch before constructing their patch objects.
    code += base.emit(101, name_index(ctx, "PatchSetAttr"))
    code += base.emit(101, name_index(ctx, "BaseGDTObject_c"))
    code += base.emit(95, name_index(ctx, "__setattr__"))

    code += base.emit(101, name_index(ctx, "ApplyPatch"))
    code += base.emit(104, 0)  # BUILD_MAP outer
    code += base.emit(4)       # DUP_TOP outer
    code += base.emit(104, 0)  # BUILD_MAP inner
    code += base.emit(4)       # DUP_TOP inner
    code += constructor
    code += base.emit(2)       # ROT_TWO
    code += base.emit(100, uid_const_index)
    code += base.emit(60)      # inner[new_uid] = object
    code += base.emit(2)       # ROT_TWO
    code += base.emit(100, const_index(ctx, "LIVERIE"))
    code += base.emit(60)      # outer['LIVERIE'] = inner
    code += base.emit(131, 1)  # ApplyPatch(outer)
    code += base.emit(1)       # POP_TOP

    constants = base.marshal_int(new_uid) + marshal_interned_string(new_script)
    return bytes(code), {
        "new_uid": new_uid,
        "new_script_name": new_script,
        "uid_const_index": uid_const_index,
        "script_const_index": script_const_index,
        "patch_code_bytes": len(code),
        "constructor": constructor_meta,
        "route": "stock DLC patch-mode constructor followed by ApplyPatch after master DATA construction",
        "patch_mode_enabled_before_constructor": True,
        "patch_mode_sequence": "BaseGDTObject_c.__setattr__ = PatchSetAttr",
        "patch_mode_reset": "ApplyPatch restores ErrorSetAttr before returning",
    }, constants


def build_patched_pyc(
    ctx: Any,
    donor_uid: int,
    original_driver_uid: int,
    recipient_driver_uid: int,
    preferred_new_uid: int,
    new_script_name: str,
) -> tuple[bytes, dict[str, Any]]:
    new_uid = int(preferred_new_uid)
    if new_uid >= 25600:
        raise ValueError("Controlled test refuses livery UIDs 25600 or higher")
    require_clean_base(ctx, new_uid, new_script_name)

    donor = base.find_record(ctx, "LIVERIE_c", donor_uid)
    if base.field_uid(ctx, donor, "Driver") != original_driver_uid:
        raise ValueError("Donor livery is not restored to its original driver")

    patch_code, patch_meta, encoded_constants = build_applypatch_code(
        ctx, donor, new_uid, new_script_name, recipient_driver_uid
    )
    layout = base.root_layout(ctx.pyc)
    instructions = base.py2_instructions(ctx, ctx.root.value.code_bytes)

    if len(instructions) < 2:
        raise ValueError("Root bytecode is unexpectedly short")
    final_load = instructions[-2]
    final_return = instructions[-1]
    if final_load["opname"] != "LOAD_CONST" or base.const_plain(ctx, final_load["arg"]) is not None:
        raise ValueError("Root module does not end in LOAD_CONST None")
    if final_return["opname"] != "RETURN_VALUE":
        raise ValueError("Root module does not end in RETURN_VALUE")

    data_store = next(
        (x for x in instructions if x["opname"] == "STORE_NAME"
         and x["arg"] is not None and ctx.root.value.names[x["arg"]] == "DATA"),
        None,
    )
    applypatch_store = next(
        (x for x in instructions if x["opname"] == "STORE_NAME"
         and x["arg"] is not None and ctx.root.value.names[x["arg"]] == "ApplyPatch"),
        None,
    )
    if data_store is None or applypatch_store is None:
        raise ValueError("Could not locate DATA and ApplyPatch definitions")
    insert_code_offset = final_load["offset"]
    if not (data_store["offset"] < applypatch_store["offset"] < insert_code_offset):
        raise ValueError("ApplyPatch insertion point is not after DATA and ApplyPatch initialization")

    out = bytearray(ctx.pyc)
    absolute_insert = layout["code_off"] + insert_code_offset
    out[absolute_insert:absolute_insert] = patch_code
    struct.pack_into("<i", out, layout["code_len_pos"], layout["code_len"] + len(patch_code))

    count_pos = layout["count_pos"] + len(patch_code)
    const_end = layout["const_end"] + len(patch_code)
    struct.pack_into("<i", out, count_pos, layout["count"] + 2)
    out[const_end:const_end] = encoded_constants
    rebuilt = bytes(out)

    return rebuilt, {
        **patch_meta,
        "old_pyc_size": len(ctx.pyc),
        "new_pyc_size": len(rebuilt),
        "growth": len(rebuilt) - len(ctx.pyc),
        "insert_code_offset": insert_code_offset,
        "insert_after_DATA_store_offset": data_store["offset"],
        "insert_after_ApplyPatch_definition_offset": applypatch_store["offset"],
        "encoded_constant_bytes": len(encoded_constants),
        "base___LIVERIE_record_count": 336,
        "dynamic_patch_record_count": 1,
    }


def record_signature(ctx: Any, record: Any) -> dict[str, Any]:
    return base.record_signature(ctx, record)


def validate_rebuild(
    ctx: Any,
    rebuilt: bytes,
    donor_uid: int,
    original_driver_uid: int,
    recipient_driver_uid: int,
    new_uid: int,
    new_script_name: str,
) -> dict[str, Any]:
    mapper = ctx.mapper
    root2 = mapper.parse_pyc(rebuilt)
    schemas2 = mapper.build_schemas(root2)
    records2 = mapper.map_records(root2, schemas2)
    after = base.Context(
        ctx.game, ctx.archive, ctx.cdfiles, ctx.row, rebuilt,
        mapper, ctx.containers, root2, schemas2, records2,
    )

    before_liveries = {base.pointer_int(ctx, r.uid): r for r in base.records_of(ctx, "LIVERIE_c")}
    after_liveries = {base.pointer_int(after, r.uid): r for r in base.records_of(after, "LIVERIE_c")}
    if set(after_liveries) != set(before_liveries) | {new_uid}:
        raise ValueError("ApplyPatch rebuild did not add exactly one livery UID")
    if len(before_liveries) != 336 or len(after_liveries) != 337:
        raise ValueError("Unexpected livery counts during ApplyPatch validation")

    existing_changes: list[dict[str, Any]] = []
    for uid, old_record in before_liveries.items():
        old_sig = record_signature(ctx, old_record)
        new_sig = record_signature(after, after_liveries[uid])
        for field in sorted(set(old_sig) | set(new_sig)):
            if old_sig.get(field) != new_sig.get(field):
                existing_changes.append({
                    "uid": uid,
                    "field": field,
                    "before": old_sig.get(field),
                    "after": new_sig.get(field),
                })
    if existing_changes:
        raise ValueError(f"ApplyPatch route changed existing liveries: {existing_changes[:10]}")

    donor = after_liveries[donor_uid]
    clone = after_liveries[new_uid]
    if base.field_uid(after, donor, "Driver") != original_driver_uid:
        raise ValueError("Donor no longer belongs to its original driver")
    if base.field_uid(after, clone, "Driver") != recipient_driver_uid:
        raise ValueError("Dynamic clone does not belong to the recipient driver")
    if base.display(after, clone.fields.get("ScriptName")) != new_script_name:
        raise ValueError("Dynamic clone ScriptName is wrong")

    donor_sig = record_signature(after, donor)
    clone_sig = record_signature(after, clone)
    for field in after.schemas["LIVERIE_c"].fields:
        if field in ("UID", "Driver", "ScriptName"):
            continue
        if donor_sig.get(field) != clone_sig.get(field):
            raise ValueError(f"Dynamic clone field {field} differs from donor")

    scripts = [base.display(after, r.fields.get("ScriptName")) for r in base.records_of(after, "LIVERIE_c")]
    if len(scripts) != len(set(scripts)):
        raise ValueError("ScriptNames are not unique after ApplyPatch rebuild")

    before_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}
    for r in ctx.records:
        before_counts[r.class_name] = before_counts.get(r.class_name, 0) + 1
    for r in records2:
        after_counts[r.class_name] = after_counts.get(r.class_name, 0) + 1
    for class_name in set(before_counts) | set(after_counts):
        delta = after_counts.get(class_name, 0) - before_counts.get(class_name, 0)
        expected = 1 if class_name == "LIVERIE_c" else 0
        if delta != expected:
            raise ValueError(f"Unexpected {class_name} record-count delta {delta}")

    # Existing root constants must remain byte-for-byte semantically identical;
    # the two new constants are appended at the end.
    old_consts = ctx.root.value.consts
    new_consts = root2.value.consts
    if len(new_consts) != len(old_consts) + 2:
        raise ValueError("Root constant table did not gain exactly two constants")
    for i, old in enumerate(old_consts):
        if mapper.value_to_display(old) != mapper.value_to_display(new_consts[i]):
            raise ValueError(f"Existing root constant {i} changed")

    return {
        "verified": True,
        "before_livery_count": 336,
        "after_livery_count": 337,
        "base_generated_livery_count": 336,
        "dynamic_applypatch_livery_count": 1,
        "existing_livery_changes": existing_changes,
        "unique_script_names": True,
        "donor_after": base.livery_summary(after, donor),
        "clone": base.livery_summary(after, clone),
        "pyc_sha256": sha256_bytes(rebuilt),
    }


def patch_cdf0_bytes(cdf_path: Path, row: Any, new_offset: int, new_size: int) -> bytes:
    return v06.patch_cdf0_bytes(cdf_path, row, new_offset, new_size)


def atomic_write(path: Path, data: bytes) -> None:
    temp = Path(str(path) + ".v09.tmp")
    with temp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def ensure_backup(path: Path) -> Path:
    backup = Path(str(path) + BACKUP_SUFFIX)
    if backup.exists():
        if backup.read_bytes() != path.read_bytes():
            raise ValueError(
                f"Existing {backup.name} does not match the live index. "
                "Restore/remove the previous v0.8 test first."
            )
        return backup
    temp = Path(str(backup) + ".tmp")
    shutil.copyfile(path, temp)
    if temp.read_bytes() != path.read_bytes():
        temp.unlink(missing_ok=True)
        raise ValueError(f"Backup verification failed for {path.name}")
    os.replace(temp, backup)
    return backup


def append_payload(path: Path, planned_offset: int, payload: bytes, alignment: int) -> None:
    with path.open("ab") as f:
        actual = f.tell()
        expected = v06.align(actual, alignment)
        if expected != planned_offset:
            raise ValueError(
                f"{path.name} append offset changed: planned 0x{planned_offset:X}, live 0x{expected:X}"
            )
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


def read_region(path: Path, offset: int, size: int) -> bytes:
    return v06.read_region(path, offset, size)


def build_plan(args: argparse.Namespace) -> tuple[dict[str, Any], bytes, bytes, bytes, bytes]:
    ctx = base.load_context(args.game)
    rebuilt_pyc, build_meta = build_patched_pyc(
        ctx,
        args.donor_uid,
        args.original_driver_uid,
        args.recipient_driver_uid,
        args.new_uid,
        args.new_script_name,
    )
    validation = validate_rebuild(
        ctx,
        rebuilt_pyc,
        args.donor_uid,
        args.original_driver_uid,
        args.recipient_driver_uid,
        build_meta["new_uid"],
        args.new_script_name,
    )

    archive2, cdfiles2 = v06.archive2_paths(ctx.game)
    cdf2_raw = cdfiles2.read_bytes()
    cdf2 = v06.parse_cdf_v6(cdf2_raw)

    donor_sd_name = f"LIVERY_{args.donor_script_name}.ARC"
    donor_hd_name = f"HDLIVERY_{args.donor_script_name}.ARC"
    new_sd_name = f"LIVERY_{args.new_script_name}.ARC"
    new_hd_name = f"HDLIVERY_{args.new_script_name}.ARC"
    _, donor_sd = v06.find_v6_file(cdf2, donor_sd_name)
    _, donor_hd = v06.find_v6_file(cdf2, donor_hd_name)

    archive2_size = archive2.stat().st_size
    sd_offset = v06.align(archive2_size, ARCHIVE2_ALIGNMENT)
    hd_offset = v06.align(sd_offset + donor_sd.data_size, ARCHIVE2_ALIGNMENT)
    final_archive2_size = hd_offset + donor_hd.data_size
    if final_archive2_size >= 2**32:
        raise ValueError("ARCHIVE2 append would exceed the 32-bit CDF offset range")

    rebuilt_cdf2, asset_meta = v06.clone_asset_entries(
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
    pyc_offset = v06.align(archive0_size, ARCHIVE0_ALIGNMENT)
    rebuilt_cdf0 = patch_cdf0_bytes(ctx.cdfiles, ctx.row, pyc_offset, len(rebuilt_pyc))

    plan = {
        "version": VERSION,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "game": str(ctx.game),
        "archive0": str(ctx.archive),
        "cdfiles0": str(ctx.cdfiles),
        "archive2": str(archive2),
        "cdfiles2": str(cdfiles2),
        "new_uid": build_meta["new_uid"],
        "new_script_name": args.new_script_name,
        "donor_script_name": args.donor_script_name,
        "donor_uid": args.donor_uid,
        "original_driver_uid": args.original_driver_uid,
        "recipient_driver_uid": args.recipient_driver_uid,
        "build": build_meta,
        "validation": validation,
        "assets": asset_meta,
        "archive0_original_size": archive0_size,
        "archive0_planned_offset": pyc_offset,
        "archive0_final_size": pyc_offset + len(rebuilt_pyc),
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
        "expected_preview": f"blank; PAINTSCHEME_{build_meta['new_uid']} is intentionally absent",
        "test_question": "Does the native DLC-style ApplyPatch route add a usable livery beyond the 336 generated base records?",
    }
    return plan, rebuilt_pyc, rebuilt_cdf0, rebuilt_cdf2, sd_payload + hd_payload


def print_plan(plan: dict[str, Any]) -> None:
    assets = plan["assets"]
    build = plan["build"]
    print(f"NASCAR 15 Native ApplyPatch Extra Scheme Probe v{VERSION}")
    print(f"Game:                {plan['game']}")
    print(f"Base livery table:   {build['base___LIVERIE_record_count']} records (unchanged)")
    print(f"Dynamic patch:       +{build['dynamic_patch_record_count']} LIVERIE_c via ApplyPatch")
    print(f"Runtime map result:  {plan['validation']['before_livery_count']} -> {plan['validation']['after_livery_count']}")
    print(f"New livery UID:      {plan['new_uid']}")
    print(f"New ScriptName:      {plan['new_script_name']}")
    print(f"New SD entry:        {assets['new_sd']['basename']}")
    print(f"New HD entry:        {assets['new_hd']['basename']}")
    print(f"SD payload:          {plan['sd_payload_size']:,} bytes copied from donor")
    print(f"HD payload:          {plan['hd_payload_size']:,} bytes copied from donor")
    print(f"PYC growth:          {build['growth']} bytes")
    print("Insertion timing:    after DATA/ApplyPatch; patch mode enabled before constructor")
    print("Preview:             intentionally absent; blank thumbnail expected")


def write_analysis(plan: dict[str, Any]) -> Path:
    path = HERE / ANALYSIS_NAME
    path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return path


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
    messages: list[str] = []
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


def cmd_analyze(args: argparse.Namespace) -> int:
    plan, _pyc, _cdf0, _cdf2, _payloads = build_plan(args)
    print_plan(plan)
    path = write_analysis(plan)
    print(f"\n[+] Analysis written: {path}")
    print("[dry-run] Nothing was changed.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    manifest_path = HERE / MANIFEST_NAME
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("applied") and not old.get("restored"):
            raise ValueError("A v0.9 test is already active. Run RESTORE_PATCHMODE_APPLYPATCH_SLOT.bat first.")

    plan, rebuilt_pyc, rebuilt_cdf0, rebuilt_cdf2, payloads = build_plan(args)
    print_plan(plan)
    ctx = base.load_context(args.game)
    archive2, cdfiles2 = v06.archive2_paths(ctx.game)
    cdf0_backup = ensure_backup(ctx.cdfiles)
    cdf2_backup = ensure_backup(cdfiles2)
    archive0_size = ctx.archive.stat().st_size
    archive2_size = archive2.stat().st_size
    if archive0_size != plan["archive0_original_size"] or archive2_size != plan["archive2_original_size"]:
        raise ValueError("An archive size changed between planning and Apply")

    sd_size = plan["sd_payload_size"]
    sd_payload = payloads[:sd_size]
    hd_payload = payloads[sd_size:]
    try:
        append_payload(ctx.archive, plan["archive0_planned_offset"], rebuilt_pyc, ARCHIVE0_ALIGNMENT)
        append_payload(archive2, plan["archive2_sd_offset"], sd_payload, ARCHIVE2_ALIGNMENT)
        append_payload(archive2, plan["archive2_hd_offset"], hd_payload, ARCHIVE2_ALIGNMENT)
        atomic_write(ctx.cdfiles, rebuilt_cdf0)
        atomic_write(cdfiles2, rebuilt_cdf2)

        live_row = base.find_cdf_row(ctx.cdfiles, base.PYC_NAME)
        if live_row.offset != plan["archive0_planned_offset"] or live_row.size != len(rebuilt_pyc):
            raise ValueError("Live cdfiles.dat did not repoint DB_GAME_LOCAL_SCRIPT.PYC as planned")
        live_pyc = read_region(ctx.archive, live_row.offset, live_row.size)
        if live_pyc != rebuilt_pyc:
            raise ValueError("Live appended PYC differs from the validated rebuild")
        live_validation = validate_rebuild(
            ctx,
            live_pyc,
            args.donor_uid,
            args.original_driver_uid,
            args.recipient_driver_uid,
            plan["new_uid"],
            args.new_script_name,
        )

        live_cdf2 = v06.parse_cdf_v6(cdfiles2.read_bytes())
        for label, name, expected_off, expected_size, expected_hash in (
            ("SD", f"LIVERY_{args.new_script_name}.ARC", plan["archive2_sd_offset"], plan["sd_payload_size"], plan["sd_payload_sha256"]),
            ("HD", f"HDLIVERY_{args.new_script_name}.ARC", plan["archive2_hd_offset"], plan["hd_payload_size"], plan["hd_payload_sha256"]),
        ):
            _, item = v06.find_v6_file(live_cdf2, name)
            if item.data_offset != expected_off or item.data_size != expected_size:
                raise ValueError(f"Live {label} CDF mapping is wrong")
            if sha256_region(archive2, item.data_offset, item.data_size) != expected_hash:
                raise ValueError(f"Live {label} payload hash does not match donor copy")

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
            "cdfiles0_sha256_live": sha256_file(ctx.cdfiles),
            "cdfiles2_sha256_live": sha256_file(cdfiles2),
        })
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        write_analysis(plan)
    except Exception:
        messages = rollback(
            ctx.archive, archive0_size, archive2, archive2_size,
            ctx.cdfiles, cdf0_backup, cdfiles2, cdf2_backup,
        )
        print("\n[rollback] " + "; ".join(messages), file=sys.stderr)
        raise

    print("\n[+] Patch-mode ApplyPatch route installed and read back successfully.")
    print("[+] The generated base __LIVERIE table remains at 336 records.")
    print("[+] The late constructor now enables PatchSetAttr first, matching stock DLC behavior.")
    print("\nTEST IN GAME:")
    print("  1. Start NASCAR 15 and record whether it reaches the main menu.")
    print("  2. Select AJ Allmendinger and open Paint Schemes.")
    print("  3. Look for the second scheme; its thumbnail may be blank.")
    print("  4. Select it and start a race.")
    print("  5. Confirm Brad's Indianapolis paint loads on AJ's car.")
    print("  6. Confirm Brad keeps his original Indianapolis alternate.")
    print("  7. Close the game and run RESTORE_PATCHMODE_APPLYPATCH_SLOT.bat.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    manifest_path = HERE / MANIFEST_NAME
    if not manifest_path.exists():
        raise FileNotFoundError(f"{MANIFEST_NAME} was not found; Apply did not finish")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("restored"):
        print("[i] This v0.9 test is already restored.")
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
        raise ValueError("ARCHIVE0 changed after v0.9 Apply; refusing to truncate a later modification")
    if archive2.stat().st_size != int(manifest["archive2_final_size"]):
        raise ValueError("ARCHIVE2 changed after v0.9 Apply; refusing to truncate a later modification")
    if sha256_file(cdf0) != manifest["cdfiles0_sha256_after"]:
        raise ValueError("cdfiles.dat changed after v0.9 Apply; refusing to overwrite a later modification")
    if sha256_file(cdf2) != manifest["cdfiles2_sha256_after"]:
        raise ValueError("cdfiles2.dat changed after v0.9 Apply; refusing to overwrite a later modification")

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
    print("[+] Restored the exact pre-v0.9 cdfiles indexes.")
    print("[+] Removed only the bytes v0.9 appended to ARCHIVE0 and ARCHIVE2.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Test a 337th NASCAR 15 livery through the stock DLC patch-mode + ApplyPatch route")
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
