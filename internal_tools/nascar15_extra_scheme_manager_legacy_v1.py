#!/usr/bin/env python3
"""NASCAR 15 true extra-scheme + named-race AI paint manager.

This module is intentionally UI-agnostic. The Flask app supplies prepared SD/HD
ARCC payloads and calls the safe append/repoint functions here.

Proven routes used:
* LIVERIE_c creation: BaseGDTObject_c.__setattr__ = PatchSetAttr, construct,
  then ApplyPatch(DATA).
* SD/HD resources: clone native CDF v6 file/tree records and append wrappers.
* AI choice: Python-2.5-safe EVENTINIT.GetLiveryName runtime guard using
  GetCurrentRaceData().GetUID().
* Preview: v2.5 native ARCC expansion adds independent PAINTSCHEME resources
  while rebuilding both the outer table and the first-wrapper inner directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import time
from pathlib import Path
from typing import Any

import nascar15_inrange_true_extra_scheme_probe_v0_4_base as base
import nascar15_complete_asset_backed_extra_scheme_probe_v0_6 as v06
import nascar15_patchmode_applypatch_extra_scheme_probe_v0_9 as v09
import nascar15_true_extra_scheme_preview_persistence_probe_v0_10 as v10
import nascar15_ai_scheme_named_event_multiseries_probe_v0_5 as ev5

VERSION = "1.6"
DBPYC = "DB_GAME_LOCAL_SCRIPT.PYC"
EVENTINIT = "EVENTINIT.PYC"
PROVEN_EXTRA_DONOR_UID = 25580
PROVEN_EXTRA_DONOR_SCRIPT = "15_2_BRAD_KESELOWSKI_BEER_1"
ALIGN0 = 16
ALIGN1 = 16
ALIGN2 = 8
STATE_FORMAT = "nascar15-extra-schemes-v1"
AI_BASE_NAME = "ai_paint_eventinit_base_v1.pyc"

# Paint Select's proven dynamic-livery range stops below 25600.  UID 25582
# was replayed successfully through the exact v0.9 + v0.10 path on a clean
# 336-livery database.  Never allocate 25600+; those records can exist in the
# DB and on disk while remaining invisible to the front-end selector.
VERIFIED_SAFE_EXTRA_UIDS = (
    25582, 25599, 25598, 25597, 25591, 25578, 25574, 25570,
)
VERIFIED_BLOCKED_EXTRA_UIDS = (
    25603, 25602, 25601, 25600, 25596, 25577, 25576, 25575,
)
MAX_CREATED_SCHEMES_PER_DRIVER = 8


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path, off: int, size: int) -> bytes:
    with path.open("rb") as f:
        f.seek(off)
        data = f.read(size)
    if len(data) != size:
        raise ValueError(f"short read from {path.name} at 0x{off:X}")
    return data


def _atomic(path: Path, data: bytes) -> None:
    tmp = Path(str(path) + ".extra_scheme.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _append(path: Path, offset: int, payload: bytes, alignment: int) -> None:
    with path.open("ab") as f:
        actual = f.tell()
        planned = v06.align(actual, alignment)
        if planned != offset:
            raise ValueError(
                f"{path.name} changed during planning: expected append 0x{offset:X}, got 0x{planned:X}"
            )
        if offset > actual:
            f.write(b"\0" * (offset - actual))
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


def _truncate(path: Path, size: int) -> None:
    with path.open("r+b") as f:
        f.truncate(size)
        f.flush()
        os.fsync(f.fileno())


def load_state(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"format": STATE_FORMAT, "version": 1, "schemes": [], "assignments": {}, "ai": {}}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not read {p.name}: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{p.name} is not a JSON object")
    obj.setdefault("format", STATE_FORMAT)
    obj.setdefault("version", 1)
    obj.setdefault("schemes", [])
    obj.setdefault("assignments", {})
    obj.setdefault("ai", {})
    return obj


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["format"] = STATE_FORMAT
    state["version"] = 1
    tmp = Path(str(p) + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _plain(ctx: Any, v: Any) -> Any:
    return ctx.mapper.value_plain_for_compare(v)


def _token_label(token: str) -> str:
    s = str(token or "")
    s = re.sub(r"^(S_DRIVER_|S_LIV_|S_WORLD_|S_EVENT_|S_EVT_)", "", s)
    words = [w for w in re.split(r"_+", s) if w]
    out = []
    for w in words:
        if w.isdigit():
            out.append(w)
        elif len(w) <= 3:
            out.append(w.upper())
        else:
            out.append(w[0].upper() + w[1:].lower())
    return " ".join(out) or token or "Unknown"


def _friendly_livery_label(script: str, name_value: str, managed_name: str = "") -> str:
    """Return a public paint name without exposing script/database tokens."""
    if managed_name:
        return str(managed_name)
    name_value = str(name_value or "").strip()
    if name_value and name_value not in ("None", "—"):
        label = _token_label(name_value)
        if label and label != "Unknown":
            return label
    script = str(script or "")
    upper = script.upper()
    m = re.match(r"^(?:14|15)_[0-9]+[A-Z]?_.+?_(PRIMARY|SECONDARY|TERTIARY|BEER_[0-9]+|THROWBACK|ALT(?:ERNATE)?_[0-9]+)$", upper)
    if m:
        return m.group(1).replace("_", " ").title()
    m = re.match(r"^DLC_LIV_[0-9]+_.+?_20[0-9]{2}(?:_([0-9]+))?$", upper)
    if m:
        number = int(m.group(1) or 1)
        return f"DLC Scheme {number}"
    m = re.match(r"^LENOVO0*([123])_(CHEVY|FORD|TOYOTA)$", upper)
    if m:
        make = {"CHEVY": "Chevrolet", "FORD": "Ford", "TOYOTA": "Toyota"}[m.group(2)]
        return f"Bonus {make}"
    label = _token_label(script)
    return label if label != "Unknown" else "Paint Scheme"


def _year_from_series(ctx: Any, rec: Any) -> int | None:
    try:
        value = rec.fields.get("Season")
        text = base.display(ctx, value)
        m = re.search(r",\s*(20\d\d)\s*,", text)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _world_info(ctx: Any, rec: Any) -> tuple[int | None, str]:
    try:
        value = rec.fields.get("World")
        uid = base.field_uid(ctx, rec, "World")
        text = base.display(ctx, value)
        token = ""
        m = re.search(r"\b(S_WORLD_[A-Z0-9_]+)\b", text)
        if m:
            token = m.group(1)
        return uid, token
    except Exception:
        return None, ""


def _asset_index(game: Path) -> tuple[Any, dict[str, Any]]:
    """Index every installed V6 archive, not only ARCHIVE2.

    Stock DLC livery records use ScriptName values such as
    DLC_LIV_48_JOHNSON_2013_3 while their physical files are named
    LIVERY_DLC_48_JOHNSON_4.ARC. Scanning every installed cdfiles index lets the
    public schedule distinguish real installed paints from unused raw slots.
    """
    data = game / "data"
    by_name: dict[str, Any] = {}
    primary = None
    for cdf_path in sorted(data.glob("cdfiles*.dat")):
        try:
            cdf = v06.parse_cdf_v6(cdf_path.read_bytes())
        except Exception:
            continue
        is_archive2 = cdf_path.name.casefold() == "cdfiles2.dat"
        if is_archive2:
            primary = cdf
        for f in cdf.files:
            try:
                key = cdf.basename(f).casefold()
            except Exception:
                continue
            # Prefer ARCHIVE2 for duplicate names because it is the only proven
            # source for creating a new app-managed slot.
            current = by_name.get(key)
            if current is None or (is_archive2 and not current[2]):
                by_name[key] = (f, cdf_path.name, is_archive2)
    if primary is None:
        _arc2, cdf2 = v06.archive2_paths(game)
        primary = v06.parse_cdf_v6(cdf2.read_bytes())
    return primary, by_name


def _asset_pair_names(script: str) -> list[tuple[str, str]]:
    out = [(f"LIVERY_{script}.ARC", f"HDLIVERY_{script}.ARC")]
    m = re.match(r"^DLC_LIV_0*([0-9]+)_([A-Z0-9]+)_20[0-9]{2}(?:_([0-9]+))?$", str(script or ""), re.I)
    if m:
        number = str(int(m.group(1)))
        surname = m.group(2).upper()
        # The first database DLC record maps to physical alternate slot 2;
        # suffix _2 maps to slot 3, and so on. Extra physical slots without a
        # matching database record remain replacement-only and are not offered
        # by the race scheduler.
        alt = int(m.group(3) or 1) + 1
        stem = f"DLC_{number}_{surname}_{alt}"
        out.append((f"LIVERY_{stem}.ARC", f"HDLIVERY_{stem}.ARC"))
    return out


def _has_pair(by_name: dict[str, Any], script: str) -> tuple[bool, int | None, int | None, bool]:
    for sd_name, hd_name in _asset_pair_names(script):
        sd_info = by_name.get(sd_name.casefold())
        hd_info = by_name.get(hd_name.casefold())
        if not (sd_info and hd_info):
            continue
        sd, _sd_cdf, sd_archive2 = sd_info
        hd, _hd_cdf, hd_archive2 = hd_info
        return True, int(sd.data_size), int(hd.data_size), bool(sd_archive2 and hd_archive2)
    return False, None, None, False


def catalog(game_arg: str | Path, state_path: str | Path) -> dict[str, Any]:
    game = base.detect_game(str(game_arg))
    ctx = base.load_context(str(game))
    state = load_state(state_path)
    managed = {int(x.get("uid")): x for x in state.get("schemes", []) if x.get("uid") is not None}
    cdf2, by_name = _asset_index(game)

    drivers: dict[int, dict[str, Any]] = {}
    for d in base.records_of(ctx, "DRIVER_c"):
        uid = base.pointer_int(ctx, d.uid)
        if uid is None:
            continue
        token = base.display(ctx, d.fields.get("Name"))
        drivers[int(uid)] = {
            "uid": int(uid),
            "token": token,
            "label": _token_label(token),
            "schemes": [],
        }

    used_all = {base.pointer_int(ctx, r.uid) for r in ctx.records}
    used_all.update(
        int(x.get("uid")) for x in state.get("schemes", [])
        if x.get("uid") is not None
    )
    for r in base.records_of(ctx, "LIVERIE_c"):
        uid = base.pointer_int(ctx, r.uid)
        driver_uid = base.field_uid(ctx, r, "Driver")
        script = base.display(ctx, r.fields.get("ScriptName"))
        if uid is None or driver_uid is None or not script:
            continue
        pair, sd_size, hd_size, pair_in_archive2 = _has_pair(by_name, script)
        year = _year_from_series(ctx, r)
        world_uid, world_token = _world_info(ctx, r)
        name_token = base.display(ctx, r.fields.get("Name"))
        item = {
            "uid": int(uid),
            "driver_uid": int(driver_uid),
            "script_name": script,
            "name_token": name_token,
            "label": _friendly_livery_label(script, name_token, managed.get(int(uid), {}).get("name", "")),
            "year": year,
            "world_uid": world_uid,
            "world_token": world_token,
            "has_assets": pair,
            "sd_size": sd_size,
            "hd_size": hd_size,
            "managed": int(uid) in managed,
            "superseded": bool(managed.get(int(uid), {}).get("superseded_by")),
            "superseded_by": managed.get(int(uid), {}).get("superseded_by"),
            "legacy_identity": bool(int(uid) in managed and str(script).upper().startswith("CUSTOM_")),
            "preview_status": managed.get(int(uid), {}).get("preview_status", "stock" if int(uid) not in managed else "unknown"),
            "native_runtime_layout_version": int(managed.get(int(uid), {}).get("native_runtime_layout_version", 0) or 0),
            "structure_donor_uid": managed.get(int(uid), {}).get("structure_donor_uid"),
            "database_recipe": managed.get(int(uid), {}).get("database_recipe", "stock" if int(uid) not in managed else "legacy_app"),
            "donor_eligible": bool(pair_in_archive2 and sd_size == 1458529 and hd_size == 5652833 and base.post_assignment_blocks(ctx, int(uid))),
        }
        drivers.setdefault(int(driver_uid), {
            "uid": int(driver_uid), "token": str(driver_uid), "label": f"Driver {driver_uid}", "schemes": []
        })["schemes"].append(item)

    # A driver belongs in the public 2015 roster when it has a current-season
    # or app-created livery. Once the driver qualifies, include every stock
    # database-backed Cup/DLC paint for that driver so the named-race scheduler
    # can use old alternates as well as the 2015 primary. Raw archive slots that
    # have no LIVERIE_c record remain replacement-only and are intentionally not
    # presented as assignable paints.
    public = []
    for d in drivers.values():
        active_driver = any(
            s["managed"] or s["year"] == 2015 or str(s["script_name"]).startswith("15_")
            for s in d["schemes"]
        )
        if not active_driver:
            continue
        useful = [
            s for s in d["schemes"]
            if not s.get("superseded") and (
                s["managed"]
                or (s["has_assets"] and (
                s["year"] in (2013, 2014, 2015)
                or str(s["script_name"]).startswith(("DLC_LIV_", "14_", "15_"))
                ))
            )
        ]
        useful.sort(key=lambda x: (not x["managed"], -(x["year"] or 0), x["label"].casefold(), x["uid"]))
        if useful:
            d["schemes"] = useful
            public.append(d)
    public.sort(key=lambda x: x["label"].casefold())

    free_uid = next((candidate for candidate in VERIFIED_SAFE_EXTRA_UIDS if candidate not in used_all), None)
    preview = preview_audit(game, state_path)
    return {
        "version": VERSION,
        "game": str(game),
        "drivers": public,
        "livery_count": len(base.records_of(ctx, "LIVERIE_c")),
        "assignable_count": sum(len(d.get("schemes", [])) for d in public),
        "managed_count": len(managed),
        "next_uid": free_uid,
        "verified_safe_uid_pool": list(VERIFIED_SAFE_EXTRA_UIDS),
        "verified_safe_uid_remaining": [x for x in VERIFIED_SAFE_EXTRA_UIDS if x not in used_all],
        "blocked_uids": list(VERIFIED_BLOCKED_EXTRA_UIDS),
        "created_limit_per_driver": MAX_CREATED_SCHEMES_PER_DRIVER,
        "assignments": state.get("assignments", {}),
        "registry_finalizer": state.get("registry_finalizer", {}),
        "needs_registry_finalize": False,
        "preview_audit": preview,
        "needs_preview_repair": bool(preview.get("missing_count")),
    }


def _native_identity_base(driver: dict[str, Any], donor_uid: int | None = None) -> str:
    """Choose a stock-style ScriptName stem for a new public livery.

    The first integrated app build used CUSTOM_<driver uid> names. Those names
    are valid database/assets identities, but the only in-game-confirmed extra
    scheme used the normal 15_<number>_<driver> naming family. Keep new slots in
    that proven family instead of exposing an app-internal naming convention to
    the game front end.
    """
    schemes = list(driver.get("schemes", []))
    donor = next((s for s in schemes if donor_uid is not None and int(s.get("uid", -1)) == int(donor_uid)), None)
    candidates = [s for s in schemes if str(s.get("script_name") or "").upper().startswith("15_")]
    candidates.sort(key=lambda s: (
        0 if str(s.get("script_name") or "").upper().endswith("_PRIMARY") else 1,
        0 if s.get("year") == 2015 else 1,
        int(s.get("uid", 999999)),
    ))
    source = (candidates[0] if candidates else donor) or (schemes[0] if schemes else None)
    script = str((source or {}).get("script_name") or "")
    if script.upper().startswith("15_"):
        # Strip only the scheme-role tail, preserving number + driver identity.
        script = re.sub(
            r"_(?:PRIMARY|SECONDARY|TERTIARY|BEER(?:_[0-9]+)?|THROWBACK|ALT(?:ERNATE)?(?:_[0-9]+)?|SPECIAL|TEST|DAY|NIGHT)$",
            "", script, flags=re.I,
        )
        return script.upper()
    # Conservative fallback for a driver without a standard 2015 script.
    return f"15_DRIVER_{int(driver.get('uid', 0))}"


def suggest_identity(
    game_arg: str | Path,
    driver_uid: int,
    name: str,
    state_path: str | Path,
    donor_uid: int | None = None,
) -> dict[str, Any]:
    """Return the exact v0.9 stock-style identity family.

    The successful historical replay used UID 25582 and
    ``15_47_AJ_EXTRA_SLOT_TEST``.  New stock-team slots therefore stay below
    25600 and use the same ``15_<car/driver>_EXTRA_SLOT_<uid>`` family.
    Display names remain app metadata and do not alter the runtime identity.
    """
    cat = catalog(game_arg, state_path)
    uid = cat.get("next_uid")
    if uid is None:
        raise ValueError("The proven sub-25600 livery UID pool is exhausted; no 25600+ UID was used")
    driver = next((d for d in cat.get("drivers", []) if int(d.get("uid", -1)) == int(driver_uid)), None)
    if driver is None:
        raise ValueError(f"Driver UID {driver_uid} is not in the assignable catalog")
    stem = _native_identity_base(driver, donor_uid)
    script = f"{stem}_EXTRA_SLOT_{int(uid)}"
    if len(script) > 64:
        suffix = f"_EXTRA_SLOT_{int(uid)}"
        script = stem[:max(8, 64-len(suffix))] + suffix
    return {"uid": int(uid), "script_name": script, "identity_family": "exact_v09_stock_style"}


def _find_final_insert(ctx: Any) -> tuple[dict[str, int], int, int, int]:
    layout = base.root_layout(ctx.pyc)
    ins = base.py2_instructions(ctx, ctx.root.value.code_bytes)
    if len(ins) < 2 or ins[-1]["opname"] != "RETURN_VALUE":
        raise ValueError("DB module does not end in RETURN_VALUE")
    final_load = ins[-2]
    if final_load["opname"] != "LOAD_CONST" or base.const_plain(ctx, final_load["arg"]) is not None:
        raise ValueError("DB module does not end in LOAD_CONST None")
    data_store = next((x for x in ins if x["opname"] == "STORE_NAME" and x["arg"] is not None and ctx.root.value.names[x["arg"]] == "DATA"), None)
    apply_store = next((x for x in ins if x["opname"] == "STORE_NAME" and x["arg"] is not None and ctx.root.value.names[x["arg"]] == "ApplyPatch"), None)
    if not data_store or not apply_store or not (data_store["offset"] < apply_store["offset"] < final_load["offset"]):
        raise ValueError("Could not locate safe post-DATA ApplyPatch insertion point")
    return layout, int(final_load["offset"]), int(data_store["offset"]), int(apply_store["offset"])


def _record_sig(ctx: Any, rec: Any) -> dict[str, Any]:
    return base.record_signature(ctx, rec)


def _build_livery_registry_flush_code(ctx: Any) -> bytes:
    """Emit a harmless final ApplyPatch({'LIVERIE': {}}) call.

    In-game testing showed a repeatable one-slot delay: the newest dynamically
    patched livery was not enumerated until the next ApplyPatch call.  A final
    empty LIVERIE patch gives the stock database code one more registry refresh
    without constructing or changing any record.
    """
    code = bytearray()
    code += base.emit(101, v09.name_index(ctx, "ApplyPatch"))
    code += base.emit(104, 0)  # BUILD_MAP outer
    code += base.emit(4)       # DUP_TOP outer
    code += base.emit(104, 0)  # BUILD_MAP empty inner
    code += base.emit(2)       # ROT_TWO
    code += base.emit(100, v09.const_index(ctx, "LIVERIE"))
    code += base.emit(60)      # outer['LIVERIE'] = {}
    code += base.emit(131, 1)  # ApplyPatch(outer)
    code += base.emit(1)       # POP_TOP
    return bytes(code)


def recipient_world_uid(ctx: Any, driver_uid: int) -> int | None:
    """World the created livery should belong to: the driver's own primary.

    build_constructor_with_live_links used to inherit World from the donor, a
    stock DLC livery belonging to a different driver. Donor 25580 is Brad
    Keselowski's Indianapolis record, so every app-created scheme was stamped
    S_WORLD_INDIANAPOLIS_SS. Two consequences, both observed in-game:

      * the scheme never appeared at the driver's actual races, because Paint
        Select is filtered by the selected event's World
      * loading THAT track crashed for every driver, because the record was
        enumerated for an event it had no valid entry for - a clean install
        loaded Indianapolis fine until the first scheme was created

    Prefer the recipient's newest primary; fall back to their newest record of
    any kind. Returning None preserves the old donor-inherited behaviour.
    """
    best_rank = None
    best_uid = None
    for r in base.records_of(ctx, "LIVERIE_c"):
        try:
            if int(base.field_uid(ctx, r, "Driver")) != int(driver_uid):
                continue
        except Exception:
            continue
        wuid, _token = _world_info(ctx, r)
        if wuid is None:
            continue
        script = (base.display(ctx, r.fields.get("ScriptName")) or "").upper()
        if "EXTRA_SLOT" in script:
            continue          # never seed from a previously created slot
        year = _year_from_series(ctx, r) or 0
        rank = (1 if script.endswith("_PRIMARY") else 0, int(year))
        if best_rank is None or rank > best_rank:
            best_rank, best_uid = rank, int(wuid)
    return best_uid


def build_db_with_scheme(ctx: Any, donor_uid: int, driver_uid: int, new_uid: int, script_name: str) -> tuple[bytes, dict[str, Any]]:
    used = {base.pointer_int(ctx, r.uid) for r in ctx.records}
    if int(new_uid) in used:
        raise ValueError(f"UID {new_uid} is already used by a database record")
    scripts = [base.display(ctx, r.fields.get("ScriptName")) for r in base.records_of(ctx, "LIVERIE_c")]
    if script_name in scripts:
        raise ValueError(f"ScriptName {script_name} already exists")
    if int(new_uid) < 0 or int(new_uid) > 0x7FFFFFFF:
        raise ValueError("The requested livery UID is outside the supported signed 32-bit range")
    donor = base.find_record(ctx, "LIVERIE_c", int(donor_uid))
    # The proven 337th-livery recipe deliberately uses a stock DLC donor from
    # another driver, then replaces only the Driver link in the constructor.
    # Do not require donor.Driver == recipient driver here.
    if not base.post_assignment_blocks(ctx, int(donor_uid)):
        raise ValueError("That scheme cannot be used as a constructor donor; select the driver's primary stock scheme")

    world_uid = recipient_world_uid(ctx, int(driver_uid))
    patch_code, patch_meta, encoded_constants = v09.build_applypatch_code(
        ctx, donor, int(new_uid), script_name, int(driver_uid), world_uid
    )
    # Match the in-game-confirmed v0.9 probe exactly. The later empty
    # ApplyPatch registry flush was an app-only experiment and did not fix
    # visibility; it is intentionally omitted.
    combined_code = patch_code
    layout, insert_code_offset, data_off, apply_off = _find_final_insert(ctx)
    out = bytearray(ctx.pyc)
    absolute = layout["code_off"] + insert_code_offset
    out[absolute:absolute] = combined_code
    struct.pack_into("<i", out, layout["code_len_pos"], layout["code_len"] + len(combined_code))
    count_pos = layout["count_pos"] + len(combined_code)
    const_end = layout["const_end"] + len(combined_code)
    struct.pack_into("<i", out, count_pos, layout["count"] + 2)
    out[const_end:const_end] = encoded_constants
    rebuilt = bytes(out)

    mapper = ctx.mapper
    root2 = mapper.parse_pyc(rebuilt)
    schemas2 = mapper.build_schemas(root2)
    records2 = mapper.map_records(root2, schemas2)
    after = base.Context(ctx.game, ctx.archive, ctx.cdfiles, ctx.row, rebuilt, mapper, ctx.containers, root2, schemas2, records2)
    before_liv = {base.pointer_int(ctx, r.uid): r for r in base.records_of(ctx, "LIVERIE_c")}
    after_liv = {base.pointer_int(after, r.uid): r for r in base.records_of(after, "LIVERIE_c")}
    if set(after_liv) != set(before_liv) | {int(new_uid)}:
        raise ValueError("Rebuild did not add exactly the requested livery")
    for uid, old in before_liv.items():
        if _record_sig(ctx, old) != _record_sig(after, after_liv[uid]):
            raise ValueError(f"Existing livery UID {uid} changed during rebuild")
    clone = after_liv[int(new_uid)]
    if base.field_uid(after, clone, "Driver") != int(driver_uid):
        raise ValueError("New livery driver link failed validation")
    if base.display(after, clone.fields.get("ScriptName")) != script_name:
        raise ValueError("New livery ScriptName failed validation")
    donor_after = after_liv[int(donor_uid)]
    a = _record_sig(after, donor_after)
    b = _record_sig(after, clone)
    # World is deliberately retargeted to the recipient driver's own track, so
    # it is expected to differ from the donor. Replace the "must match donor"
    # check with a positive assertion that it matches what we intended - same
    # rigour, correct expectation.
    exempt = {"UID", "Driver", "ScriptName"}
    if world_uid is not None:
        exempt.add("World")
        got_world = base.field_uid(after, clone, "World")
        if got_world is None or int(got_world) != int(world_uid):
            raise ValueError(
                f"New livery World link failed validation: wanted {world_uid}, got {got_world}")
    for field in after.schemas["LIVERIE_c"].fields:
        if field in exempt:
            continue
        if a.get(field) != b.get(field):
            raise ValueError(f"New livery field {field} differs from donor")

    # All non-livery record counts remain stable.
    bc: dict[str, int] = {}
    ac: dict[str, int] = {}
    for r in ctx.records:
        bc[r.class_name] = bc.get(r.class_name, 0) + 1
    for r in records2:
        ac[r.class_name] = ac.get(r.class_name, 0) + 1
    for cls in set(bc) | set(ac):
        expected = 1 if cls == "LIVERIE_c" else 0
        if ac.get(cls, 0) - bc.get(cls, 0) != expected:
            raise ValueError(f"Unexpected {cls} count change")

    return rebuilt, {
        "before_liveries": len(before_liv),
        "after_liveries": len(after_liv),
        "new_uid": int(new_uid),
        "script_name": script_name,
        "driver_uid": int(driver_uid),
        "donor_uid": int(donor_uid),
        "growth": len(rebuilt) - len(ctx.pyc),
        "insert_code_offset": insert_code_offset,
        "data_store_offset": data_off,
        "applypatch_store_offset": apply_off,
        "patch": patch_meta,
        "registry_flush": {
            "enabled": False,
            "code_bytes": 0,
            "route": "disabled; exact v0.9 ApplyPatch route",
        },
        "sha256": _sha(rebuilt),
    }


def finalize_livery_registry(game_arg: str | Path, state_path: str | Path) -> dict[str, Any]:
    """Append a DB copy with only the final empty LIVERIE ApplyPatch refresh.

    This repairs schemes created by builds that wrote the livery and assets but
    left the newest record one ApplyPatch behind the game's front-end registry.
    No database record, asset, assignment, or existing bytecode constant changes.
    """
    game = base.detect_game(str(game_arg))
    ctx = base.load_context(str(game))
    layout, insert_code_offset, data_off, apply_off = _find_final_insert(ctx)
    flush_code = _build_livery_registry_flush_code(ctx)
    out = bytearray(ctx.pyc)
    absolute = layout["code_off"] + insert_code_offset
    out[absolute:absolute] = flush_code
    struct.pack_into("<i", out, layout["code_len_pos"], layout["code_len"] + len(flush_code))
    rebuilt = bytes(out)

    mapper = ctx.mapper
    root2 = mapper.parse_pyc(rebuilt)
    schemas2 = mapper.build_schemas(root2)
    records2 = mapper.map_records(root2, schemas2)
    after = base.Context(ctx.game, ctx.archive, ctx.cdfiles, ctx.row, rebuilt, mapper, ctx.containers, root2, schemas2, records2)
    before = [(r.class_name, base.pointer_int(ctx, r.uid), _record_sig(ctx, r)) for r in ctx.records]
    after_rows = [(r.class_name, base.pointer_int(after, r.uid), _record_sig(after, r)) for r in records2]
    if before != after_rows:
        raise ValueError("Registry finalizer changed database records; write refused")

    old_size = ctx.archive.stat().st_size
    old_cdf = ctx.cdfiles.read_bytes()
    new_off = v06.align(old_size, ALIGN0)
    rebuilt_cdf = v06.patch_cdf0_bytes(ctx.cdfiles, ctx.row, new_off, len(rebuilt))
    try:
        _append(ctx.archive, new_off, rebuilt, ALIGN0)
        _atomic(ctx.cdfiles, rebuilt_cdf)
        live_ctx = base.load_context(str(game))
        if live_ctx.pyc != rebuilt:
            raise ValueError("Live DB finalizer readback mismatch")
        live_records = [(r.class_name, base.pointer_int(live_ctx, r.uid), _record_sig(live_ctx, r)) for r in live_ctx.records]
        if live_records != before:
            raise ValueError("Live registry finalizer changed database records")
    except Exception as original_error:
        rollback_errors = []
        try:
            _truncate(ctx.archive, old_size)
            _atomic(ctx.cdfiles, old_cdf)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise

    state = load_state(state_path)
    state["registry_finalizer"] = {
        "version": 1,
        "applied": int(time.time()),
        "db_sha256": _sha(rebuilt),
        "repair_only": True,
    }
    save_state(state_path, state)
    return {
        "ok": True,
        "archive0_offset": new_off,
        "pyc_size": len(rebuilt),
        "code_bytes": len(flush_code),
        "record_count": len(records2),
        "records_changed": 0,
        "sha256": _sha(rebuilt),
        "insert_code_offset": insert_code_offset,
        "data_store_offset": data_off,
        "applypatch_store_offset": apply_off,
    }


def proven_extra_donor(game_arg: str | Path) -> dict[str, Any]:
    """Return and validate the exact stock DLC donor used by the working probe."""
    game = base.detect_game(str(game_arg))
    ctx = base.load_context(str(game))
    rec = base.find_record(ctx, "LIVERIE_c", PROVEN_EXTRA_DONOR_UID)
    script = base.display(ctx, rec.fields.get("ScriptName"))
    if script != PROVEN_EXTRA_DONOR_SCRIPT:
        raise ValueError(
            f"Proven donor UID {PROVEN_EXTRA_DONOR_UID} is {script!r}, expected "
            f"{PROVEN_EXTRA_DONOR_SCRIPT!r}"
        )
    if not base.post_assignment_blocks(ctx, PROVEN_EXTRA_DONOR_UID):
        raise ValueError("The proven extra-scheme donor constructor is unavailable")
    pair = donor_asset_pair(game, script)
    if pair["sd_size"] != 1458529 or pair["hd_size"] != 5652833:
        raise ValueError("The proven donor paint wrappers have unexpected sizes")
    return {
        "uid": PROVEN_EXTRA_DONOR_UID,
        "script_name": script,
        "driver_uid": base.field_uid(ctx, rec, "Driver"),
        "record": _record_sig(ctx, rec),
        "sd_size": pair["sd_size"],
        "hd_size": pair["hd_size"],
    }


def donor_asset_pair(game_arg: str | Path, donor_script: str) -> dict[str, Any]:
    game = base.detect_game(str(game_arg))
    arc2, cdf2_path = v06.archive2_paths(game)
    cdf = v06.parse_cdf_v6(cdf2_path.read_bytes())
    sd_name = f"LIVERY_{donor_script}.ARC"
    hd_name = f"HDLIVERY_{donor_script}.ARC"
    _, sd = v06.find_v6_file(cdf, sd_name)
    _, hd = v06.find_v6_file(cdf, hd_name)
    return {
        "sd_name": sd_name,
        "hd_name": hd_name,
        "sd": _read(arc2, int(sd.data_offset), int(sd.data_size)),
        "hd": _read(arc2, int(hd.data_offset), int(hd.data_size)),
        "sd_size": int(sd.data_size),
        "hd_size": int(hd.data_size),
    }


def _context_from_indexed_pyc(game: Path, archive: Path, cdf: Path) -> Any:
    mapper, _patcher, _schemas = base.load_modules()
    row = base.find_cdf_row(cdf, DBPYC)
    pyc = _read(archive, int(row.offset), int(row.size))
    root = mapper.parse_pyc(pyc)
    schemas = mapper.build_schemas(root)
    records = mapper.map_records(root, schemas)
    return base.Context(game, archive, cdf, row, pyc, mapper, {}, root, schemas, records)


def _rows_without_managed(ctx: Any, managed_uids: set[int]) -> list[tuple[str, int | None, dict[str, Any]]]:
    out = []
    for rec in ctx.records:
        uid = base.pointer_int(ctx, rec.uid)
        if rec.class_name == "LIVERIE_c" and uid in managed_uids:
            continue
        out.append((rec.class_name, uid, _record_sig(ctx, rec)))
    return out


def rebuild_managed_database_from_clean_base(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    backup_archive: str | Path,
    backup_cdf: str | Path,
    donor_uid: int = PROVEN_EXTRA_DONOR_UID,
) -> dict[str, Any]:
    """Rebuild only app-managed livery patches from the clean indexed DB copy.

    Safety rule: every non-managed record in the clean source must match the live
    database exactly. This prevents the repair from erasing unrelated race or
    schedule edits.
    """
    game = base.detect_game(str(game_arg))
    state = load_state(state_path)
    active = [x for x in state.get("schemes", []) if not x.get("superseded_by")]
    if not active:
        raise ValueError("There are no active app-created schemes to rebuild")
    managed_uids = {int(x["uid"]) for x in active}
    live = base.load_context(str(game))
    clean = _context_from_indexed_pyc(game, Path(backup_archive), Path(backup_cdf))
    if _rows_without_managed(live, managed_uids) != _rows_without_managed(clean, set()):
        raise ValueError(
            "The clean DB copy differs from live non-scheme records. Repair refused so unrelated edits are not lost."
        )
    donor = base.find_record(clean, "LIVERIE_c", int(donor_uid))
    donor_script = base.display(clean, donor.fields.get("ScriptName"))
    if int(donor_uid) != PROVEN_EXTRA_DONOR_UID or donor_script != PROVEN_EXTRA_DONOR_SCRIPT:
        raise ValueError("The requested database donor is not the proven v0.9 donor")

    ctx = clean
    steps = []
    for item in sorted(active, key=lambda x: int(x.get("created", 0))):
        rebuilt, meta = build_db_with_scheme(
            ctx,
            int(donor_uid),
            int(item["driver_uid"]),
            int(item["uid"]),
            str(item["script_name"]),
        )
        root = ctx.mapper.parse_pyc(rebuilt)
        schemas = ctx.mapper.build_schemas(root)
        records = ctx.mapper.map_records(root, schemas)
        ctx = base.Context(game, clean.archive, clean.cdfiles, clean.row, rebuilt,
                           clean.mapper, {}, root, schemas, records)
        steps.append(meta)

    repaired = ctx.pyc
    expected_liveries = len(base.records_of(clean, "LIVERIE_c")) + len(active)
    if len(base.records_of(ctx, "LIVERIE_c")) != expected_liveries:
        raise ValueError("Managed database rebuild produced the wrong livery count")
    if _rows_without_managed(ctx, managed_uids) != _rows_without_managed(clean, set()):
        raise ValueError("Managed database rebuild changed a non-managed record")

    old_size = live.archive.stat().st_size
    old_cdf = live.cdfiles.read_bytes()
    new_off = v06.align(old_size, ALIGN0)
    rebuilt_cdf = v06.patch_cdf0_bytes(live.cdfiles, live.row, new_off, len(repaired))
    try:
        _append(live.archive, new_off, repaired, ALIGN0)
        _atomic(live.cdfiles, rebuilt_cdf)
        check = base.load_context(str(game))
        if check.pyc != repaired:
            raise ValueError("Repaired database readback mismatch")
        if _rows_without_managed(check, managed_uids) != _rows_without_managed(clean, set()):
            raise ValueError("Live repaired database changed a non-managed record")
    except Exception as original_error:
        rollback_errors = []
        try:
            _truncate(live.archive, old_size)
            _atomic(live.cdfiles, old_cdf)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise

    for item in state.get("schemes", []):
        if int(item.get("uid", -1)) in managed_uids:
            item["structure_donor_uid"] = int(donor_uid)
            item["structure_donor_script_name"] = donor_script
            item["database_recipe"] = "exact_v0_9_applypatch"
            item["database_repaired"] = int(time.time())
    state.pop("registry_finalizer", None)
    save_state(state_path, state)
    return {
        "ok": True,
        "scheme_count": len(active),
        "livery_count": expected_liveries,
        "donor_uid": int(donor_uid),
        "donor_script_name": donor_script,
        "offset": new_off,
        "size": len(repaired),
        "sha256": _sha(repaired),
        "steps": steps,
    }


def install_scheme(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    driver_uid: int,
    donor_uid: int,
    new_uid: int,
    script_name: str,
    display_name: str,
    sd_payload: bytes,
    hd_payload: bytes,
    source_png_name: str = "",
) -> dict[str, Any]:
    game = base.detect_game(str(game_arg))
    preflight_state = load_state(state_path)
    if any(int(x.get("uid", -1)) == int(new_uid) for x in preflight_state.get("schemes", [])):
        raise ValueError("State already contains the new scheme UID (preflight; no game files changed)")
    ctx = base.load_context(str(game))
    donor = base.find_record(ctx, "LIVERIE_c", int(donor_uid))
    donor_script = base.display(ctx, donor.fields.get("ScriptName"))
    rebuilt_pyc, db_meta = build_db_with_scheme(ctx, donor_uid, driver_uid, new_uid, script_name)

    arc2, cdf2_path = v06.archive2_paths(game)
    cdf2_raw = cdf2_path.read_bytes()
    cdf2 = v06.parse_cdf_v6(cdf2_raw)
    donor_sd = f"LIVERY_{donor_script}.ARC"
    donor_hd = f"HDLIVERY_{donor_script}.ARC"
    new_sd = f"LIVERY_{script_name}.ARC"
    new_hd = f"HDLIVERY_{script_name}.ARC"
    _, sd_rec = v06.find_v6_file(cdf2, donor_sd)
    _, hd_rec = v06.find_v6_file(cdf2, donor_hd)
    if len(sd_payload) != int(sd_rec.data_size) or len(hd_payload) != int(hd_rec.data_size):
        raise ValueError("Prepared SD/HD wrappers do not match donor slot sizes")
    if sd_payload[:4] != b"ARCC" or hd_payload[:4] != b"ARCC":
        raise ValueError("Prepared SD/HD wrappers are not ARCC files")

    arc0_size = ctx.archive.stat().st_size
    arc2_size = arc2.stat().st_size
    pyc_off = v06.align(arc0_size, ALIGN0)
    sd_off = v06.align(arc2_size, ALIGN2)
    hd_off = v06.align(sd_off + len(sd_payload), ALIGN2)
    rebuilt_cdf0 = v06.patch_cdf0_bytes(ctx.cdfiles, ctx.row, pyc_off, len(rebuilt_pyc))
    rebuilt_cdf2, asset_meta = v06.clone_asset_entries(
        cdf2, donor_sd, donor_hd, new_sd, new_hd, sd_off, hd_off
    )
    old_cdf0 = ctx.cdfiles.read_bytes()
    old_cdf2 = cdf2_raw
    try:
        _append(ctx.archive, pyc_off, rebuilt_pyc, ALIGN0)
        _append(arc2, sd_off, sd_payload, ALIGN2)
        _append(arc2, hd_off, hd_payload, ALIGN2)
        _atomic(ctx.cdfiles, rebuilt_cdf0)
        _atomic(cdf2_path, rebuilt_cdf2)

        live_ctx = base.load_context(str(game))
        live = base.find_record(live_ctx, "LIVERIE_c", int(new_uid))
        if base.field_uid(live_ctx, live, "Driver") != int(driver_uid):
            raise ValueError("Live livery readback driver mismatch")
        if base.display(live_ctx, live.fields.get("ScriptName")) != script_name:
            raise ValueError("Live livery readback ScriptName mismatch")
        live_cdf2 = v06.parse_cdf_v6(cdf2_path.read_bytes())
        for name, off, payload in ((new_sd, sd_off, sd_payload), (new_hd, hd_off, hd_payload)):
            _, rec = v06.find_v6_file(live_cdf2, name)
            if int(rec.data_offset) != int(off) or int(rec.data_size) != len(payload):
                raise ValueError(f"Live CDF mapping failed for {name}")
            if _read(arc2, off, len(payload)) != payload:
                raise ValueError(f"Live payload readback failed for {name}")
    except Exception as original_error:
        rollback_errors = []
        try:
            _truncate(ctx.archive, arc0_size)
            _truncate(arc2, arc2_size)
            _atomic(ctx.cdfiles, old_cdf0)
            _atomic(cdf2_path, old_cdf2)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise

    state = load_state(state_path)
    if any(int(x.get("uid", -1)) == int(new_uid) for x in state.get("schemes", [])):
        raise ValueError("State already contains the new scheme UID")
    entry = {
        "uid": int(new_uid),
        "driver_uid": int(driver_uid),
        "donor_uid": int(donor_uid),
        "donor_script_name": donor_script,
        "script_name": script_name,
        "name": str(display_name or script_name),
        "sd_entry": new_sd,
        "hd_entry": new_hd,
        "source_png": source_png_name,
        "created": int(time.time()),
        "preview_status": "disabled_unproven",
        "structure_donor_uid": int(donor_uid),
        "structure_donor_script_name": donor_script,
        "database_recipe": "exact_v0_9_applypatch",
        "db_sha256": db_meta["sha256"],
    }
    state.setdefault("schemes", []).append(entry)
    state["registry_finalizer"] = {
        "version": 1,
        "applied": int(time.time()),
        "newest_uid": int(new_uid),
        "db_sha256": db_meta["sha256"],
    }
    save_state(state_path, state)
    return {
        "ok": True,
        "scheme": entry,
        "database": db_meta,
        "assets": asset_meta,
        "archive0_offset": pyc_off,
        "archive2_sd_offset": sd_off,
        "archive2_hd_offset": hd_off,
    }


def _entry_category(name: str) -> str:
    if name.startswith("PAINTSCHEME_"):
        return "paint"
    if name.startswith("DRIVERPAINT_"):
        return "driverpaint"
    if name.startswith("DRIVER_") and "_3DNUM_" in name:
        return "number"
    return "other"


def _profile(e: Any) -> tuple[str, int, int, str, int]:
    return (_entry_category(e.name), len(e.name.encode("latin1")), int(e.width), int(e.height), str(e.fmt))


def _copy_entry_as(out: bytearray, template: Any, source_arc: Any, template_name: str, source_name: str, new_name: str) -> None:
    target = v10.entry_by_name(template, template_name)
    source = v10.entry_by_name(source_arc, source_name)
    if len(template_name.encode("latin1")) != len(new_name.encode("latin1")):
        raise ValueError("preview slot and requested name lengths differ")
    if (source.width, source.height, source.fmt) != (target.width, target.height, target.fmt):
        raise ValueError("preview texture profiles differ")
    expected = v10.expected_texture_bytes(source)
    src_room = source.chunk_end - source.payload_abs
    dst_room = target.chunk_end - target.payload_abs
    block = 16 if source.fmt == "DXT5" else 8
    src_n = min(src_room, expected)
    dst_n = min(dst_room, expected)
    if src_n % block or dst_n % block or expected - src_n > 64 or expected - dst_n > 64:
        raise ValueError("unsupported preview native truncation layout")
    pixels = source_arc.raw[source.payload_abs:source.payload_abs + src_n]
    pixels = (pixels + b"\0" * (expected - src_n))[:dst_n]
    record = v10.identity_record(source.table_record, source, target)
    out[target.table_start:target.table_start + 32] = record
    out[target.payload_abs:target.payload_abs + dst_n] = pixels
    name_abs = template.name_blob + target.name_ref
    if bytes(out[name_abs:name_abs + len(template_name)]).decode("latin1") != template_name:
        raise ValueError("preview template name mismatch")
    out[name_abs:name_abs + len(template_name)] = new_name.encode("latin1")


def _preview_livery_driver_map(game: Path) -> dict[int, int]:
    """Map PAINTSCHEME livery UIDs to their owning driver from the live DB."""
    ctx = base.load_context(str(game))
    out: dict[int, int] = {}
    for rec in base.records_of(ctx, "LIVERIE_c"):
        uid = base.pointer_int(ctx, rec.uid)
        driver_uid = base.field_uid(ctx, rec, "Driver")
        if uid is not None and driver_uid is not None:
            out[int(uid)] = int(driver_uid)
    return out


def _preview_entry_uid(name: str) -> int | None:
    m = re.fullmatch(r"PAINTSCHEME_([0-9]+)", str(name or ""))
    return int(m.group(1)) if m else None


def _preview_represented_drivers(mar: Any) -> set[int]:
    """Drivers whose car/number identities are actually transplanted into a TD container."""
    out: set[int] = set()
    for entry in mar.entries:
        name = str(entry.name or "")
        m = re.match(r"DRIVERPAINT_([0-9]+)_", name)
        if not m:
            m = re.match(r"DRIVER_([0-9]+)_3DNUM_", name)
        if m:
            out.add(int(m.group(1)))
    return out


def _write_preview_container(archive: Path, cdf: Path, target_row: Any, rebuilt: bytes) -> int:
    """Append/repoint one validated TD container, rolling back on any failure."""
    old_arc_size = archive.stat().st_size
    old_cdf = cdf.read_bytes()
    new_off = v06.align(old_arc_size, ALIGN1)
    try:
        _append(archive, new_off, rebuilt, ALIGN1)
        live_raw, live_rows = v10.parse_cdf_rows(cdf)
        live_row = v10.find_row(live_rows, target_row.name)
        if live_row.offset != target_row.offset or live_row.size != target_row.size:
            raise ValueError("Target preview container changed during planning")
        struct.pack_into("<I", live_raw, live_row.offset_pos, new_off)
        struct.pack_into("<I", live_raw, live_row.size_pos, len(rebuilt))
        _atomic(cdf, bytes(live_raw))
        _, check_rows = v10.parse_cdf_rows(cdf)
        check_row = v10.find_row(check_rows, target_row.name)
        check_raw = v10.read_entry(archive, check_row)
        if check_raw != rebuilt:
            raise ValueError("Preview container readback mismatch")
        v10.parse_multi_arc(check_raw)
        return int(new_off)
    except Exception as original_error:
        rollback_errors = []
        try:
            _truncate(archive, old_arc_size)
            _atomic(cdf, old_cdf)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise


def preview_audit(game_arg: str | Path, state_path: str | Path) -> dict[str, Any]:
    """Report whether every active app-created livery owns a PAINTSCHEME entry."""
    game = base.detect_game(str(game_arg))
    archive, cdf = v10.paths_for_game(game)
    _raw, rows = v10.parse_cdf_rows(cdf)
    indexed: dict[int, str] = {}
    for row in rows:
        if not row.name.startswith("2DRIVERSELECTTD_"):
            continue
        try:
            mar = v10.parse_multi_arc(v10.read_entry(archive, row))
        except Exception:
            continue
        for entry in mar.entries:
            uid = _preview_entry_uid(entry.name)
            if uid is not None:
                indexed[int(uid)] = row.name
    state = load_state(state_path)
    active = [x for x in state.get("schemes", []) if not x.get("superseded_by") and x.get("uid") is not None]
    missing = [int(x["uid"]) for x in active if int(x["uid"]) not in indexed]
    present = [int(x["uid"]) for x in active if int(x["uid"]) in indexed]
    return {
        "ok": True,
        "managed": len(active),
        "present": present,
        "missing": missing,
        "missing_count": len(missing),
        "containers": {str(uid): indexed[uid] for uid in present},
    }


def install_preview_clone(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    driver_uid: int,
    donor_livery_uid: int,
    new_livery_uid: int,
) -> dict[str, Any]:
    """Install a native donor thumbnail for one app-created livery.

    The first integrated allocator expanded a driver's 6-entry container to a
    stock 10-entry template, but then incorrectly treated all four template paint
    entries as occupied.  Three of those entries still belong to the template's
    unrelated stock drivers and are native spare slots after the transplant.
    Reuse those slots before looking for a larger template.
    """
    game = base.detect_game(str(game_arg))
    archive, cdf = v10.paths_for_game(game)
    _raw, rows = v10.parse_cdf_rows(cdf)
    containers = []
    for row in rows:
        if not row.name.startswith("2DRIVERSELECTTD_"):
            continue
        try:
            mar = v10.parse_multi_arc(v10.read_entry(archive, row))
            containers.append((row, mar))
        except Exception:
            continue
    donor_name = f"PAINTSCHEME_{int(donor_livery_uid)}"
    new_name = f"PAINTSCHEME_{int(new_livery_uid)}"
    donor_hits = [(r, m) for r, m in containers if any(e.name == donor_name for e in m.entries)]
    if not donor_hits:
        raise ValueError("The donor scheme has no indexed PAINTSCHEME preview")
    donor_row, donor_arc = donor_hits[0]
    target_row, target_arc = donor_row, donor_arc
    if any(e.name == new_name for e in target_arc.entries):
        state = load_state(state_path)
        for item in state.get("schemes", []):
            if int(item.get("uid", -1)) == int(new_livery_uid):
                item["preview_status"] = "already_present"
                item["preview_container"] = target_row.name
        save_state(state_path, state)
        return {"ok": True, "status": "already_present", "container": target_row.name, "entry": new_name}

    donor_src = v10.entry_by_name(donor_arc, donor_name)
    driver_map = _preview_livery_driver_map(game)
    represented = _preview_represented_drivers(target_arc)
    represented.add(int(driver_uid))

    # First choice: repurpose a native PAINTSCHEME entry that belongs to a driver
    # no longer represented by this transplanted container.  This is exactly what
    # the proven v0.10 preview probe did; only the equal-length lookup name and
    # donor pixels change, while the stock wrapper/tail remain native.
    reusable = []
    expected_profile = (_entry_category(donor_src.name), len(new_name), donor_src.width, donor_src.height, donor_src.fmt)
    for entry in target_arc.entries:
        uid = _preview_entry_uid(entry.name)
        if uid is None or _profile(entry) != expected_profile:
            continue
        owner = driver_map.get(int(uid))
        if owner is None or owner in represented:
            continue
        reusable.append((int(uid), entry))
    if reusable:
        _old_uid, spare = sorted(reusable, key=lambda x: x[0], reverse=True)[0]
        out = bytearray(target_arc.raw)
        _copy_entry_as(out, target_arc, donor_arc, spare.name, donor_name, new_name)
        rebuilt = bytes(out)
        parsed = v10.parse_multi_arc(rebuilt)
        names = [e.name for e in parsed.entries]
        if len(names) != len(set(names)):
            raise ValueError("Preview repair produced duplicate entry names")
        v10.entry_by_name(parsed, new_name)
        new_off = _write_preview_container(archive, cdf, target_row, rebuilt)
        state = load_state(state_path)
        for item in state.get("schemes", []):
            if int(item.get("uid", -1)) == int(new_livery_uid):
                item["preview_status"] = "copied"
                item["preview_source_uid"] = int(donor_livery_uid)
                item["preview_container"] = target_row.name
                item["preview_reused_native_uid"] = int(_old_uid)
        save_state(state_path, state)
        return {
            "ok": True,
            "status": "copied",
            "container": target_row.name,
            "entry": new_name,
            "source_uid": int(donor_livery_uid),
            "reused_native_uid": int(_old_uid),
            "archive_offset": new_off,
            "method": "reuse_unrelated_native_slot",
        }

    # Fresh 6-entry destination: transplant it into a compatible stock template
    # with one additional paint slot, preserving the established v0.10 route.
    current_entries = list(target_arc.entries)
    needed = len(current_entries) + 1
    candidates = []
    for row, tmpl in containers:
        if tmpl.count < needed:
            continue
        paint_slots = [e for e in tmpl.entries if _entry_category(e.name) == "paint" and len(e.name) == len(new_name)]
        if not paint_slots:
            continue
        unused = list(tmpl.entries)
        mapping = []
        okay = True
        for src in current_entries:
            exact = next((e for e in unused if e.name == src.name and _profile(e) == _profile(src)), None)
            dst = exact or next((e for e in unused if _profile(e) == _profile(src)), None)
            if dst is None:
                okay = False
                break
            mapping.append((dst, src))
            unused.remove(dst)
        if not okay:
            continue
        spare = next((e for e in unused if _profile(e) == expected_profile), None)
        if spare is None:
            continue
        score = (sum(1 for e in tmpl.entries if _entry_category(e.name) == "paint"), -tmpl.count)
        candidates.append((score, row, tmpl, mapping, spare))
    if not candidates:
        raise ValueError("No compatible native PAINTSCHEME slot remains for this driver")
    _score, template_row, template, mapping, spare = max(candidates, key=lambda x: x[0])
    out = bytearray(template.raw)
    for dst, src in mapping:
        _copy_entry_as(out, template, target_arc, dst.name, src.name, src.name)
    _copy_entry_as(out, template, donor_arc, spare.name, donor_name, new_name)
    rebuilt = bytes(out)
    parsed = v10.parse_multi_arc(rebuilt)
    names = [e.name for e in parsed.entries]
    if len(names) != len(set(names)):
        raise ValueError("Preview template rebuild produced duplicate entry names")
    for src in current_entries:
        v10.entry_by_name(parsed, src.name)
    v10.entry_by_name(parsed, new_name)
    new_off = _write_preview_container(archive, cdf, target_row, rebuilt)

    state = load_state(state_path)
    for item in state.get("schemes", []):
        if int(item.get("uid", -1)) == int(new_livery_uid):
            item["preview_status"] = "copied"
            item["preview_source_uid"] = int(donor_livery_uid)
            item["preview_container"] = target_row.name
    save_state(state_path, state)
    return {
        "ok": True,
        "status": "copied",
        "container": target_row.name,
        "template_container": template_row.name,
        "entry": new_name,
        "archive_offset": new_off,
        "method": "native_template_expand",
    }


def repair_missing_previews(game_arg: str | Path, state_path: str | Path) -> dict[str, Any]:
    """Give every active app-created livery a native PAINTSCHEME lookup entry."""
    game = base.detect_game(str(game_arg))
    before = preview_audit(game, state_path)
    state = load_state(state_path)
    by_uid = {int(x.get("uid")): x for x in state.get("schemes", []) if x.get("uid") is not None}
    repaired = []
    errors = []
    for uid in before.get("missing", []):
        item = by_uid.get(int(uid)) or {}
        try:
            result = install_preview_clone(
                game, state_path,
                driver_uid=int(item["driver_uid"]),
                donor_livery_uid=int(item["donor_uid"]),
                new_livery_uid=int(uid),
            )
            repaired.append({"uid": int(uid), **result})
        except Exception as exc:
            errors.append({"uid": int(uid), "error": str(exc)})
    after = preview_audit(game, state_path)
    return {
        "ok": not errors and not after.get("missing"),
        "before": before,
        "after": after,
        "repaired": repaired,
        "errors": errors,
    }


def assignments(state_path: str | Path) -> dict[str, dict[str, int]]:
    raw = load_state(state_path).get("assignments", {})
    out: dict[str, dict[str, int]] = {}
    if isinstance(raw, dict):
        for event_key, rows in raw.items():
            if not isinstance(rows, dict):
                continue
            out[str(event_key)] = {str(int(d)): int(l) for d, l in rows.items() if l not in (None, "", 0, "0")}
    return out


def save_assignments(state_path: str | Path, new_assignments: dict[str, Any]) -> dict[str, dict[str, int]]:
    clean: dict[str, dict[str, int]] = {}
    for event_key, rows in (new_assignments or {}).items():
        if not isinstance(rows, dict):
            continue
        erows = {}
        for d, l in rows.items():
            if l in (None, "", 0, "0"):
                continue
            erows[str(int(d))] = int(l)
        if erows:
            clean[str(event_key)] = erows
    state = load_state(state_path)
    state["assignments"] = clean
    save_state(state_path, state)
    return clean


def _event_matches(ctx: Any, event_key: str) -> list[int]:
    try:
        event_uid_s, token = str(event_key).split("|", 1)
        event_uid = int(event_uid_s)
    except Exception as exc:
        raise ValueError(f"Invalid named-race key {event_key!r}") from exc
    hits = []
    for race in base.records_of(ctx, "RACEDATA_c"):
        try:
            if base.display(ctx, race.fields.get("EventName")) == token and base.field_uid(ctx, race, "RaceEvent") == event_uid:
                hits.append(int(base.pointer_int(ctx, race.uid)))
        except Exception:
            pass
    if not hits:
        raise ValueError(f"No RACEDATA records match {event_key}")
    return sorted(set(hits))


def _resolve_assignment_rules(game_arg: str | Path, state_path: str | Path) -> list[dict[str, Any]]:
    ctx = base.load_context(str(game_arg))
    liveries = {int(base.pointer_int(ctx, r.uid)): r for r in base.records_of(ctx, "LIVERIE_c")}
    grouped: dict[tuple[int, str], set[int]] = {}
    for event_key, rows in assignments(state_path).items():
        race_uids = _event_matches(ctx, event_key)
        for d, l in rows.items():
            driver_uid, livery_uid = int(d), int(l)
            if livery_uid not in liveries:
                raise ValueError(f"Assigned livery UID {livery_uid} no longer exists")
            liv = liveries[livery_uid]
            if base.field_uid(ctx, liv, "Driver") != driver_uid:
                raise ValueError(f"Livery UID {livery_uid} does not belong to driver UID {driver_uid}")
            script = base.display(ctx, liv.fields.get("ScriptName"))
            grouped.setdefault((driver_uid, script), set()).update(race_uids)
    return [
        {"driver_uid": d, "script_name": s, "racedata_uids": sorted(uids)}
        for (d, s), uids in sorted(grouped.items())
    ]


def _assemble_multi_override(*, rules: list[dict[str, Any]], co: Any, consts: dict[Any, int], names: dict[str, int]) -> bytes:
    # Each false guard jumps to that rule's next label, where one retained
    # Python-2.5 condition value is popped before the next rule begins.
    chunks: list[bytes | tuple[str, str]] = []
    labels: dict[str, int] = {}

    def add(b: bytes) -> None:
        chunks.append(b)

    def jf(label: str) -> None:
        chunks.append(("jf", label))
        add(ev5.emit(ev5.POP_TOP))

    for i, rule in enumerate(rules):
        next_label = f"next_{i}"
        add(ev5.emit(ev5.LOAD_FAST, co.varnames.index("driverID")))
        add(ev5.emit(ev5.LOAD_CONST, consts[("driver", int(rule["driver_uid"]))]))
        add(ev5.emit(ev5.COMPARE_OP, 2))
        jf(next_label)

        for need_current in (False, True):
            add(ev5.emit(ev5.LOAD_CONST, consts[("scalar", -1)]))
            add(ev5.emit(ev5.LOAD_CONST, consts[("scalar", None)]))
            add(ev5.emit(ev5.IMPORT_NAME, names["GSRaceStoryFlowState"]))
            add(ev5.emit(ev5.LOAD_ATTR, names["GSRaceStoryFlowState_c"]))
            add(ev5.emit(ev5.LOAD_ATTR, names["Instance"]))
            add(ev5.emit(ev5.CALL_FUNCTION, 0))
            if need_current:
                add(ev5.emit(ev5.LOAD_ATTR, names["GetCurrentRaceData"]))
                add(ev5.emit(ev5.CALL_FUNCTION, 0))
            jf(next_label)

        add(ev5.emit(ev5.LOAD_CONST, consts[("scalar", -1)]))
        add(ev5.emit(ev5.LOAD_CONST, consts[("scalar", None)]))
        add(ev5.emit(ev5.IMPORT_NAME, names["GSRaceStoryFlowState"]))
        add(ev5.emit(ev5.LOAD_ATTR, names["GSRaceStoryFlowState_c"]))
        add(ev5.emit(ev5.LOAD_ATTR, names["Instance"]))
        add(ev5.emit(ev5.CALL_FUNCTION, 0))
        add(ev5.emit(ev5.LOAD_ATTR, names["GetCurrentRaceData"]))
        add(ev5.emit(ev5.CALL_FUNCTION, 0))
        add(ev5.emit(ev5.LOAD_ATTR, names["GetUID"]))
        add(ev5.emit(ev5.CALL_FUNCTION, 0))
        for uid in rule["racedata_uids"]:
            add(ev5.emit(ev5.LOAD_CONST, consts[("race", int(uid))]))
        add(ev5.emit(ev5.BUILD_TUPLE, len(rule["racedata_uids"])))
        add(ev5.emit(ev5.COMPARE_OP, 6))
        jf(next_label)
        add(ev5.emit(ev5.LOAD_CONST, consts[("script", str(rule["script_name"]))]))
        add(ev5.emit(ev5.RETURN_VALUE))
        chunks.append(("label", next_label))
        add(ev5.emit(ev5.POP_TOP))

    add(ev5.emit(ev5.LOAD_GLOBAL, co.names.index("range")))
    add(ev5.emit(ev5.JUMP_ABSOLUTE, 3))

    positions = []
    pos = 0
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
    for c, p in zip(chunks, positions):
        if isinstance(c, tuple):
            if c[0] == "label":
                continue
            delta = labels[c[1]] - (p + 3)
            if not 0 <= delta <= 0xFFFF:
                raise ValueError("Named-race AI guard jump exceeds Python 2.5 operand range")
            out += ev5.emit(ev5.JUMP_IF_FALSE, delta)
        else:
            out += c
    return bytes(out)


def patch_eventinit_multi(pyc: bytes, mapper: Any, rules: list[dict[str, Any]]) -> tuple[bytes, dict[str, Any]]:
    if pyc[:2] != struct.pack("<H", 62131):
        raise ValueError("EVENTINIT is not the expected Python 2.5c2 PYC")
    root = mapper.parse_pyc(pyc)
    old_co = ev5.find_code_mval(mapper, root, "GetLiveryName").value
    if old_co.code_bytes[:3] != ev5.emit(ev5.LOAD_GLOBAL, old_co.names.index("range")):
        raise ValueError("GetLiveryName is already patched; restore the standalone named-event probe before enabling the app schedule")
    out = pyc
    consts: dict[Any, int] = {}
    for value in (-1, None):
        out, idx, _ = ev5.ensure_const(mapper, out, "GetLiveryName", value)
        consts[("scalar", value)] = idx
    for rule in rules:
        d = int(rule["driver_uid"])
        s = str(rule["script_name"])
        out, idx, _ = ev5.ensure_const(mapper, out, "GetLiveryName", d)
        consts[("driver", d)] = idx
        out, idx, _ = ev5.ensure_const(mapper, out, "GetLiveryName", s)
        consts[("script", s)] = idx
        for uid in rule["racedata_uids"]:
            out, idx, _ = ev5.ensure_const(mapper, out, "GetLiveryName", int(uid))
            consts[("race", int(uid))] = idx
    names: dict[str, int] = {}
    for value in ("GSRaceStoryFlowState", "GSRaceStoryFlowState_c", "Instance", "GetCurrentRaceData", "GetUID"):
        out, idx, _ = ev5.ensure_name(mapper, out, "GetLiveryName", value)
        names[value] = idx
    co = ev5.code_object(mapper, out, "GetLiveryName")
    old_len = len(co.code_bytes)
    override = _assemble_multi_override(rules=rules, co=co, consts=consts, names=names)
    # Validate instruction widths and reject Python 2.7 POP_JUMP opcodes.
    p = 0
    while p < len(override):
        op = override[p]
        if op in (114, 115):
            raise ValueError("Generated invalid Python 2.7 POP_JUMP opcode")
        p += 3 if op >= ev5.HAVE_ARGUMENT else 1
    if p != len(override):
        raise ValueError("Generated EVENTINIT bytecode is truncated")
    layout = ev5.code_layout(out, mapper, "GetLiveryName")
    code = bytearray(co.code_bytes)
    code[:3] = ev5.emit(ev5.JUMP_ABSOLUTE, old_len)
    code += override
    rebuilt = bytearray(out)
    struct.pack_into("<i", rebuilt, layout["code_len_pos"], len(code))
    rebuilt[layout["code_payload"]:layout["code_end"]] = code
    rebuilt = bytes(rebuilt)
    new_root = mapper.parse_pyc(rebuilt)
    new_co = ev5.find_code_mval(mapper, new_root, "GetLiveryName").value
    if new_co.code_bytes[:3] != ev5.emit(ev5.JUMP_ABSOLUTE, old_len):
        raise ValueError("EVENTINIT entry redirect failed")
    if new_co.code_bytes[3:old_len] != old_co.code_bytes[3:]:
        raise ValueError("Original GetLiveryName body changed")
    if new_co.code_bytes[old_len:] != override:
        raise ValueError("EVENTINIT override readback failed")
    return rebuilt, {
        "rules": len(rules),
        "assignments": sum(len(r["racedata_uids"]) for r in rules),
        "old_size": len(pyc),
        "new_size": len(rebuilt),
        "growth": len(rebuilt) - len(pyc),
        "override_bytes": len(override),
        "sha256": _sha(rebuilt),
    }


def _load_eventinit(game: Path) -> tuple[Path, Path, Any, bytes, Any]:
    archive, cdf = base.archive0_paths(game)
    mapper, patcher, _ = base.load_modules()
    row = base.find_cdf_row(cdf, EVENTINIT)
    pyc = _read(archive, row.offset, row.size)
    mapper.parse_pyc(pyc)
    return archive, cdf, row, pyc, mapper


def _eventinit_clean_info(pyc: bytes, mapper: Any | None = None) -> dict[str, Any]:
    """Return whether GetLiveryName is the untouched stock-style function.

    The app schedule and the older standalone probes both redirect the function
    with a leading jump. A reusable base must instead begin with the original
    LOAD_GLOBAL range instruction.
    """
    if mapper is None:
        mapper, _patcher, _schemas = base.load_modules()
    root = mapper.parse_pyc(pyc)
    co = ev5.find_code_mval(mapper, root, "GetLiveryName").value
    try:
        expected = ev5.emit(ev5.LOAD_GLOBAL, co.names.index("range"))
    except ValueError:
        expected = b""
    clean = bool(expected and co.code_bytes[:3] == expected)
    return {
        "clean": clean,
        "sha256": _sha(pyc),
        "size": len(pyc),
        "entry_opcode": int(co.code_bytes[0]) if co.code_bytes else None,
    }


def _eventinit_from_paths(archive_path: str | Path, cdf_path: str | Path) -> tuple[bytes, Any]:
    archive = Path(archive_path)
    cdf = Path(cdf_path)
    mapper, _patcher, _schemas = base.load_modules()
    row = base.find_cdf_row(cdf, EVENTINIT)
    pyc = _read(archive, row.offset, row.size)
    mapper.parse_pyc(pyc)
    return pyc, row


def ai_base_status(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    backup_archive: str | Path | None = None,
    backup_cdf: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect all possible clean EVENTINIT base sources without writing."""
    game = base.detect_game(str(game_arg))
    archive, cdf, row, live, mapper = _load_eventinit(game)
    base_file = Path(state_path).resolve().parent / AI_BASE_NAME
    out: dict[str, Any] = {
        "ready": False,
        "source": None,
        "base_file": str(base_file),
        "base_exists": base_file.exists(),
        "live": _eventinit_clean_info(live, mapper),
        "live_offset": int(row.offset),
        "live_size": int(row.size),
        "backup_available": False,
        "backup": None,
    }
    if base_file.exists():
        try:
            saved = _eventinit_clean_info(base_file.read_bytes(), mapper)
            out["saved_base"] = saved
            if saved["clean"]:
                out.update({"ready": True, "source": "saved_base"})
                return out
        except Exception as exc:
            out["saved_base_error"] = str(exc)
    if out["live"]["clean"]:
        out.update({"ready": True, "source": "live_clean"})
        return out
    ba = Path(backup_archive) if backup_archive else None
    bc = Path(backup_cdf) if backup_cdf else None
    if ba and bc and ba.exists() and bc.exists():
        out["backup_available"] = True
        try:
            backup_pyc, backup_row = _eventinit_from_paths(ba, bc)
            binfo = _eventinit_clean_info(backup_pyc, mapper)
            binfo.update({"offset": int(backup_row.offset)})
            out["backup"] = binfo
            if binfo["clean"]:
                out.update({"ready": True, "source": "pristine_backup"})
        except Exception as exc:
            out["backup_error"] = str(exc)
    return out


def ensure_ai_base(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    backup_archive: str | Path | None = None,
    backup_cdf: str | Path | None = None,
) -> dict[str, Any]:
    """Save a validated clean EVENTINIT base without modifying the live game.

    Source priority: an existing clean app base, a clean live EVENTINIT, then a
    clean pristine archive/index backup. This lets the app safely supersede a
    still-active standalone probe instead of forcing a manual restore first.
    """
    game = base.detect_game(str(game_arg))
    archive, cdf, row, live, mapper = _load_eventinit(game)
    base_file = Path(state_path).resolve().parent / AI_BASE_NAME

    candidates: list[tuple[str, bytes]] = []
    if base_file.exists():
        try:
            candidates.append(("saved_base", base_file.read_bytes()))
        except Exception:
            pass
    candidates.append(("live_clean", live))
    ba = Path(backup_archive) if backup_archive else None
    bc = Path(backup_cdf) if backup_cdf else None
    if ba and bc and ba.exists() and bc.exists():
        try:
            backup_pyc, _backup_row = _eventinit_from_paths(ba, bc)
            candidates.append(("pristine_backup", backup_pyc))
        except Exception:
            pass

    chosen_source = None
    chosen = None
    for source, pyc in candidates:
        try:
            if _eventinit_clean_info(pyc, mapper)["clean"]:
                chosen_source, chosen = source, pyc
                break
        except Exception:
            continue
    if chosen is None:
        raise ValueError(
            "No clean EVENTINIT base is available. The live GetLiveryName is already patched "
            "and the pristine ARCHIVE0/cdfiles backup is missing or also patched. Restore the "
            "standalone named-event probe or restore original game files, then retry."
        )

    # Re-save when recovering from live/backup or replacing an invalid old base.
    try:
        current_base = base_file.read_bytes() if base_file.exists() else None
    except Exception:
        current_base = None
    if current_base != chosen:
        tmp = Path(str(base_file) + ".tmp")
        tmp.write_bytes(chosen)
        os.replace(tmp, base_file)
    state = load_state(state_path)
    state.setdefault("ai", {}).update({
        "base_sha256": _sha(chosen),
        "base_source": chosen_source,
        "base_saved": int(time.time()),
    })
    save_state(state_path, state)
    status = ai_base_status(
        game, state_path, backup_archive=backup_archive, backup_cdf=backup_cdf
    )
    status["captured_from"] = chosen_source
    return status


def ai_plan(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    backup_archive: str | Path | None = None,
    backup_cdf: str | Path | None = None,
) -> dict[str, Any]:
    game = base.detect_game(str(game_arg))
    base_status = ensure_ai_base(
        game, state_path, backup_archive=backup_archive, backup_cdf=backup_cdf
    )
    rules = _resolve_assignment_rules(game, state_path)
    archive, cdf, row, live, mapper = _load_eventinit(game)
    state = load_state(state_path)
    ai = state.setdefault("ai", {})
    base_file = Path(state_path).resolve().parent / AI_BASE_NAME
    base_pyc = base_file.read_bytes()
    mapper.parse_pyc(base_pyc)
    if rules:
        rebuilt, meta = patch_eventinit_multi(base_pyc, mapper, rules)
    else:
        rebuilt, meta = base_pyc, {"rules": 0, "assignments": 0, "old_size": len(base_pyc), "new_size": len(base_pyc), "growth": 0, "override_bytes": 0, "sha256": _sha(base_pyc)}
    return {
        "ok": True,
        "rules": rules,
        "meta": meta,
        "base_exists": base_file.exists(),
        "live_sha256": _sha(live),
        "base_sha256": _sha(base_pyc),
        "base_status": base_status,
        "changed": rebuilt != live,
        "eventinit_row": {"offset": row.offset, "size": row.size},
    }


def apply_ai(
    game_arg: str | Path,
    state_path: str | Path,
    *,
    backup_archive: str | Path | None = None,
    backup_cdf: str | Path | None = None,
) -> dict[str, Any]:
    game = base.detect_game(str(game_arg))
    ensure_ai_base(game, state_path, backup_archive=backup_archive, backup_cdf=backup_cdf)
    rules = _resolve_assignment_rules(game, state_path)
    archive, cdf, row, live, mapper = _load_eventinit(game)
    state = load_state(state_path)
    ai = state.setdefault("ai", {})
    base_file = Path(state_path).resolve().parent / AI_BASE_NAME
    base_pyc = base_file.read_bytes()
    mapper.parse_pyc(base_pyc)
    rebuilt, meta = patch_eventinit_multi(base_pyc, mapper, rules) if rules else (base_pyc, {"rules": 0, "assignments": 0, "old_size": len(base_pyc), "new_size": len(base_pyc), "growth": 0, "override_bytes": 0, "sha256": _sha(base_pyc)})
    old_size = archive.stat().st_size
    new_off = v06.align(old_size, ALIGN0)
    old_cdf = cdf.read_bytes()
    try:
        _append(archive, new_off, rebuilt, ALIGN0)
        base.write_cdf_row(cdf, row, new_off, len(rebuilt))
        check = base.find_cdf_row(cdf, EVENTINIT)
        if check.offset != new_off or check.size != len(rebuilt):
            raise ValueError("EVENTINIT repoint readback failed")
        if _read(archive, check.offset, check.size) != rebuilt:
            raise ValueError("EVENTINIT payload readback failed")
        mapper.parse_pyc(rebuilt)
    except Exception as original_error:
        rollback_errors = []
        try:
            _truncate(archive, old_size)
            _atomic(cdf, old_cdf)
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise RuntimeError(str(original_error) + '; rollback failed: ' + '; '.join(rollback_errors)) from original_error
        raise
    ai.update({
        "applied": True,
        "post_sha256": _sha(rebuilt),
        "post_offset": new_off,
        "post_size": len(rebuilt),
        "updated": int(time.time()),
        "rules": len(rules),
    })
    save_state(state_path, state)
    return {"ok": True, "meta": meta, "offset": new_off, "rules": rules}


def restore_ai(game_arg: str | Path, state_path: str | Path) -> dict[str, Any]:
    game = base.detect_game(str(game_arg))
    archive, cdf, row, live, mapper = _load_eventinit(game)
    state = load_state(state_path)
    base_file = Path(state_path).resolve().parent / AI_BASE_NAME
    if not base_file.exists():
        raise ValueError("No AI Paint Schedule base EVENTINIT has been saved")
    base_pyc = base_file.read_bytes()
    mapper.parse_pyc(base_pyc)
    old_size = archive.stat().st_size
    new_off = v06.align(old_size, ALIGN0)
    _append(archive, new_off, base_pyc, ALIGN0)
    base.write_cdf_row(cdf, row, new_off, len(base_pyc))
    check = base.find_cdf_row(cdf, EVENTINIT)
    if _read(archive, check.offset, check.size) != base_pyc:
        raise ValueError("Restored EVENTINIT readback failed")
    state.setdefault("ai", {}).update({"applied": False, "post_sha256": _sha(base_pyc), "post_offset": new_off, "post_size": len(base_pyc), "updated": int(time.time()), "rules": 0})
    save_state(state_path, state)
    return {"ok": True, "offset": new_off, "size": len(base_pyc)}
