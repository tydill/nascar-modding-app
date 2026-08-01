#!/usr/bin/env python3
"""Fixed-count stock-team Paint Select writer for NASCAR 15.

This is the generalized form of the in-game-proven v0.10 AJ Allmendinger
preview route.  It never grows a 2DRIVERSELECTTD container.  Instead it starts
from a compatible pristine, game-authored fixed-count template, transplants
only the resources that the target team currently needs, repurposes one unused
native PAINTSCHEME slot for the new livery UID, imports the requested thumbnail
into that fixed slot, appends the rebuilt container to ARCHIVE1, and repoints
only that container's cdfiles1 row.  The template allocator supports stock teams
with more than two drivers without growing the selected container. Driver tiles
and 3D-number cards are always transplanted as a matched donor identity pair;
they are never assigned independently.

Moved/custom-team creation is intentionally outside this module's scope.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path
from typing import Any, Iterable

import nascar15_thumbnail_import_probe_rc10 as thumb
import nascar15_true_extra_scheme_preview_persistence_rc10 as v10

VERSION = "1.2"
DEFAULT_TEMPLATE = "2DRIVERSELECTTD_1336.ARC"
ALIGNMENT = 0x10


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _align(value: int, alignment: int = ALIGNMENT) -> int:
    return (int(value) + alignment - 1) // alignment * alignment


def _rows(path: Path):
    _raw, rows = v10.parse_cdf_rows(path)
    return rows


def _row(rows, name: str):
    hits = [r for r in rows if str(r.name).casefold() == str(name).casefold()]
    if len(hits) != 1:
        raise ValueError(f"Expected one cdfiles1 row for {name}; found {len(hits)}")
    return hits[0]


def _read(archive: Path, row) -> bytes:
    with archive.open("rb") as fh:
        fh.seek(int(row.offset))
        raw = fh.read(int(row.size))
    if len(raw) != int(row.size):
        raise ValueError(f"Short archive read for {row.name}")
    return raw


def _backup_pair(data: Path) -> tuple[Path, Path] | None:
    candidates: list[tuple[Path, Path]] = []
    for suffix in (".n15mod.bak", ".gridapp.bak"):
        archive = data / ("ARCHIVE1.AR" + suffix)
        cdf = data / ("cdfiles1.dat" + suffix)
        if archive.exists() and cdf.exists():
            candidates.append((archive, cdf))
    if not candidates:
        return None
    # Earliest complete pair is the closest thing to a pristine game-authored source.
    return min(candidates, key=lambda p: (max(p[0].stat().st_mtime, p[1].stat().st_mtime), str(p[0])))


def _template(game: Path, container: str = DEFAULT_TEMPLATE):
    data = game / "data"
    choices: list[tuple[Path, Path, str]] = []
    backup = _backup_pair(data)
    if backup:
        choices.append((backup[0], backup[1], "pristine app backup"))
    choices.append((data / "ARCHIVE1.AR", data / "cdfiles1.dat", "live archive"))
    errors = []
    for archive, cdf, label in choices:
        try:
            row = _row(_rows(cdf), container)
            raw = _read(archive, row)
            parsed = v10.parse_multi_arc(raw)
            if parsed.count != 10:
                raise ValueError(f"expected the proven 10-entry template; found {parsed.count}")
            return raw, parsed, label
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    raise ValueError("Could not load the fixed stock template: " + " | ".join(errors))


def _kind(entry: v10.MultiEntry) -> str:
    name = str(entry.name).upper()
    if name.startswith("DRIVERPAINT_"):
        return "driverpaint"
    if name.startswith("DRIVER_") and "_3DNUM_" in name:
        return "3dnum"
    if name.startswith("PAINTSCHEME_"):
        return "paintscheme"
    return "other"


def _profile(entry: v10.MultiEntry) -> tuple[str, int, int, str, int]:
    return (
        _kind(entry), int(entry.width), int(entry.height), str(entry.fmt),
        len(str(entry.name).encode("latin1")),
    )


def _actual_recipient_entries(
    recipient: v10.MultiArc,
    team_driver_uids: Iterable[int],
    livery_uids: Iterable[int],
    new_uid: int,
) -> list[v10.MultiEntry]:
    drivers = {int(x) for x in team_driver_uids}
    liveries = {int(x) for x in livery_uids if int(x) != int(new_uid)}
    selected: list[v10.MultiEntry] = []
    unexplained: list[str] = []
    for entry in recipient.entries:
        name = str(entry.name)
        m = re.fullmatch(r"DRIVERPAINT_([0-9]+)_.+", name, re.I)
        if m:
            if int(m.group(1)) in drivers:
                selected.append(entry)
            continue
        m = re.fullmatch(r"DRIVER_([0-9]+)_3DNUM_.+", name, re.I)
        if m:
            if int(m.group(1)) in drivers:
                selected.append(entry)
            continue
        m = re.fullmatch(r"PAINTSCHEME_([0-9]+)", name, re.I)
        if m:
            if int(m.group(1)) in liveries:
                selected.append(entry)
            continue
        # Unknown stock resources must be preserved.  A template without a
        # compatible fixed slot is rejected rather than silently dropping one.
        selected.append(entry)
        unexplained.append(name)

    # Only transplant previews that are actually present in the current team
    # bank and belong to a live livery for this team.  This deliberately ignores
    # unused placeholder names left by an earlier fixed-template rebuild.
    for driver_uid in sorted(drivers):
        has_tile = any(re.match(fr"DRIVERPAINT_{driver_uid}_", e.name, re.I) for e in selected)
        has_num = any(re.match(fr"DRIVER_{driver_uid}_3DNUM_", e.name, re.I) for e in selected)
        if not has_tile or not has_num:
            missing = []
            if not has_tile: missing.append("driver tile")
            if not has_num: missing.append("3D number")
            raise ValueError(
                f"The current team bank is missing {' and '.join(missing)} for driver UID {driver_uid}"
            )
    return sorted(selected, key=lambda e: e.index)


def _driver_resource(entry: v10.MultiEntry) -> tuple[int, str] | None:
    """Return (driver_uid, kind) for a native driver-art resource."""
    name = str(entry.name)
    m = re.fullmatch(r"DRIVERPAINT_([0-9]+)_.+", name, re.I)
    if m:
        return int(m.group(1)), "driverpaint"
    m = re.fullmatch(r"DRIVER_([0-9]+)_3DNUM_.+", name, re.I)
    if m:
        return int(m.group(1)), "3dnum"
    return None


def _driver_pairs(entries: Iterable[v10.MultiEntry]) -> dict[int, dict[str, v10.MultiEntry]]:
    pairs: dict[int, dict[str, v10.MultiEntry]] = {}
    for entry in entries:
        info = _driver_resource(entry)
        if info is None:
            continue
        uid, kind = info
        if kind in pairs.setdefault(uid, {}):
            raise ValueError(f"duplicate {kind} resource for driver UID {uid}")
        pairs[uid][kind] = entry
    return pairs


def _map_to_template(sources: list[v10.MultiEntry], template: v10.MultiArc, new_name: str):
    """Map recipient resources into one fixed-count native template.

    The in-game-proven v0.10 AJ route used DRIVERPAINT and 3DNUM slots belonging
    to the *same donor driver*.  rc7 generalized those two resource families
    independently.  On templates whose table order differs between DRIVERPAINT
    and 3DNUM sections, that crossed hidden donor identities and caused the
    entire Paint Select bank to be rejected by the game.

    This allocator treats every driver's tile + number card as one atomic donor
    pair.  Non-driver resources are mapped only after all pairs are reserved.
    """
    unused = list(template.entries)
    mapping: list[tuple[v10.MultiEntry, v10.MultiEntry]] = []
    exact_matches = 0
    driver_pair_map: list[dict[str, Any]] = []

    source_pairs = _driver_pairs(sources)
    template_pairs = _driver_pairs(template.entries)

    # Preserve source-driver order by the first physical resource in its pair.
    source_order = sorted(
        source_pairs,
        key=lambda uid: min(e.index for e in source_pairs[uid].values()),
    )
    used_donor_uids: set[int] = set()
    driver_source_entries: set[int] = set()

    for source_uid in source_order:
        pair = source_pairs[source_uid]
        if set(pair) != {"driverpaint", "3dnum"}:
            missing = sorted({"driverpaint", "3dnum"} - set(pair))
            raise ValueError(
                f"source driver UID {source_uid} lacks a matched native "
                + " and ".join(missing)
            )
        source_dp = pair["driverpaint"]
        source_num = pair["3dnum"]
        driver_source_entries.update((id(source_dp), id(source_num)))

        candidates = []
        for donor_uid, donor_pair in template_pairs.items():
            if donor_uid in used_donor_uids:
                continue
            if set(donor_pair) != {"driverpaint", "3dnum"}:
                continue
            donor_dp = donor_pair["driverpaint"]
            donor_num = donor_pair["3dnum"]
            if donor_dp not in unused or donor_num not in unused:
                continue
            if _profile(donor_dp) != _profile(source_dp):
                continue
            if _profile(donor_num) != _profile(source_num):
                continue
            exact = int(donor_dp.name == source_dp.name) + int(donor_num.name == source_num.name)
            first_index = min(donor_dp.index, donor_num.index)
            candidates.append((exact, -first_index, donor_uid, donor_dp, donor_num))

        if not candidates:
            raise ValueError(
                f"no matched native DRIVERPAINT + 3DNUM donor pair for driver UID {source_uid}"
            )
        exact, _neg_index, donor_uid, donor_dp, donor_num = max(candidates)
        used_donor_uids.add(int(donor_uid))
        unused.remove(donor_dp)
        unused.remove(donor_num)
        mapping.extend(((donor_dp, source_dp), (donor_num, source_num)))
        exact_matches += int(exact)
        driver_pair_map.append({
            "source_driver_uid": int(source_uid),
            "donor_driver_uid": int(donor_uid),
            "driverpaint_slot": str(donor_dp.name),
            "driverpaint_resource": str(source_dp.name),
            "number_slot": str(donor_num.name),
            "number_resource": str(source_num.name),
        })

    # Reserve the new livery's native paint identity before mapping existing
    # thumbnails.  The historical success used an established 255xx alternate
    # slot (PAINTSCHEME_25580), not a primary 253xx slot that merely happened to
    # be left over after greedy mapping.  Prefer 25580, then another stock 255xx
    # paint identity, and avoid consuming an exact-name live source when a spare
    # unrelated identity exists.
    needed_len = len(new_name.encode("latin1"))
    source_names = {str(e.name) for e in sources}
    paint_slots = [e for e in unused
                   if _kind(e) == "paintscheme" and len(e.name.encode("latin1")) == needed_len]
    if not paint_slots:
        raise ValueError("no spare equal-length native PAINTSCHEME slot")

    def paint_donor_score(entry: v10.MultiEntry):
        m = re.fullmatch(r"PAINTSCHEME_([0-9]+)", str(entry.name), re.I)
        uid = int(m.group(1)) if m else -1
        return (
            1 if str(entry.name) not in source_names else 0,
            2 if str(entry.name).upper() == "PAINTSCHEME_25580" else
            (1 if 25500 <= uid < 25600 else 0),
            -int(entry.index),
        )

    donor = max(paint_slots, key=paint_donor_score)
    unused.remove(donor)

    # Map existing paint thumbnails and any unknown stock resources only after
    # the donor identity and all matched driver-art pairs are reserved.
    for source in sources:
        if id(source) in driver_source_entries:
            continue
        candidates = [e for e in unused if _profile(e) == _profile(source)]
        if not candidates:
            raise ValueError(
                f"no compatible slot for {source.name} "
                f"({source.width}x{source.height} {source.fmt})"
            )
        candidates.sort(key=lambda e: (0 if e.name == source.name else 1, e.index))
        dest = candidates[0]
        if dest.name == source.name:
            exact_matches += 1
        unused.remove(dest)
        mapping.append((dest, source))

    # Final structural guard: each transplanted source driver must resolve to a
    # single donor UID for both of its resources.
    for pair in driver_pair_map:
        donor_uid = pair["donor_driver_uid"]
        if f"_{donor_uid}_" not in pair["driverpaint_slot"] or f"_{donor_uid}_" not in pair["number_slot"]:
            raise ValueError("driver-art donor-pair validation failed")

    return mapping, donor, unused, exact_matches, driver_pair_map


def _template_candidates(
    game: Path,
    *,
    recipient_raw: bytes,
    recipient: v10.MultiArc,
    target_container: str,
):
    """Yield fixed-count candidate banks without trusting modified live banks.

    The current target bank is always considered first so a prior fixed-template
    rebuild can reuse its native spare slots.  Other candidates come from the
    earliest complete pristine ARCHIVE1/cdfiles1 backup when available; only on
    installs without a backup do we inspect the live stock banks.
    """
    yield recipient_raw, recipient, "current target bank", target_container, True

    data = game / "data"
    backup = _backup_pair(data)
    if backup:
        sources = [(backup[0], backup[1], "pristine app backup")]
    else:
        sources = [(data / "ARCHIVE1.AR", data / "cdfiles1.dat", "live stock archive")]

    seen = {_sha(recipient_raw)}
    for archive, cdf, label in sources:
        try:
            rows = _rows(cdf)
        except Exception:
            continue
        for row in rows:
            if not str(row.name).startswith("2DRIVERSELECTTD_"):
                continue
            try:
                raw = _read(archive, row)
                digest = _sha(raw)
                if digest in seen:
                    continue
                parsed = v10.parse_multi_arc(raw)
                seen.add(digest)
                yield raw, parsed, label, str(row.name), False
            except Exception:
                continue


def _choose_template(
    game: Path,
    *,
    recipient_raw: bytes,
    recipient: v10.MultiArc,
    target_container: str,
    sources: list[v10.MultiEntry],
    new_name: str,
):
    """Choose the smallest compatible game-authored fixed-count bank.

    The old rc6 helper hardcoded Brad/Joey's two-driver container.  That worked
    for AJ but rejected stock teams containing three or more drivers.  This
    allocator searches every pristine stock team bank and picks one that can
    preserve all current driver tiles, 3D numbers, and live paint thumbnails,
    plus one additional native PAINTSCHEME slot.
    """
    plans = []
    rejected = []
    for raw, template, source_label, container_name, is_current in _template_candidates(
        game,
        recipient_raw=recipient_raw,
        recipient=recipient,
        target_container=target_container,
    ):
        if int(template.count) < len(sources) + 1:
            rejected.append(f"{container_name}: {template.count} slots for {len(sources)+1} required")
            continue
        try:
            mapping, donor, unused, exact_matches, driver_pair_map = _map_to_template(sources, template, new_name)
        except Exception as exc:
            rejected.append(f"{container_name}: {exc}")
            continue
        remaining_paint = sum(
            1 for e in unused
            if _kind(e) == "paintscheme" and e.name != donor.name
        )
        # Prefer reusing the current fixed bank, then exact-name matches, then
        # the smallest native container that fits.  Extra paint capacity is the
        # final tiebreaker.
        score = (
            1 if is_current else 0,
            int(exact_matches),
            -int(template.count),
            int(remaining_paint),
        )
        plans.append((score, raw, template, source_label, container_name,
                      mapping, donor, unused, remaining_paint, driver_pair_map))

    if not plans:
        detail = " | ".join(rejected[:8])
        raise ValueError(
            "No pristine fixed-count Paint Select template can hold this team's "
            f"{len(sources)} existing resources plus one new paint slot."
            + (" Checked: " + detail if detail else "")
        )
    return max(plans, key=lambda x: x[0])


def _import_entry(raw: bytes, name: str) -> dict[str, Any]:
    parsed = v10.parse_multi_arc(raw)
    entry = v10.entry_by_name(parsed, name)
    logical = v10.expected_texture_bytes(entry)
    room = max(0, entry.chunk_end - entry.payload_abs)
    size = min(logical, room)
    block = 16 if entry.fmt == "DXT5" else 8
    size -= size % block
    if size <= 0:
        raise ValueError(f"Native slot {name} has no writable texture payload")
    return {
        "name": name, "w": int(entry.width), "h": int(entry.height),
        "fmt": str(entry.fmt), "payload_abs": int(entry.payload_abs),
        "payload_size": int(size), "needed": int(logical),
    }


def _patch_cdf(cdf_bytes: bytes, cdf_path: Path, row_name: str, offset: int, size: int) -> bytes:
    out = bytearray(cdf_bytes)
    row = _row(_rows(cdf_path), row_name)
    struct.pack_into("<I", out, int(row.size_pos), int(size))
    struct.pack_into("<I", out, int(row.offset_pos), int(offset))
    return bytes(out)


def _atomic(path: Path, data: bytes) -> None:
    tmp = Path(str(path) + ".fixed_stock.tmp")
    with tmp.open("wb") as fh:
        fh.write(data); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def install_fixed_template_thumbnail(
    game_arg: str | Path,
    *,
    target_container: str,
    team_driver_uids: Iterable[int],
    livery_uids: Iterable[int],
    new_uid: int,
    image_path: str | Path,
    template_container: str = DEFAULT_TEMPLATE,
) -> dict[str, Any]:
    """Install one new stock-team preview without growing the ARCC bank."""
    game = v10.detect_game(str(game_arg))
    data = game / "data"
    archive = data / "ARCHIVE1.AR"
    cdf = data / "cdfiles1.dat"
    if not archive.exists() or not cdf.exists():
        raise FileNotFoundError("ARCHIVE1.AR/cdfiles1.dat is missing")

    live_rows = _rows(cdf)
    target_row = _row(live_rows, target_container)
    recipient_raw = _read(archive, target_row)
    recipient = v10.parse_multi_arc(recipient_raw)
    new_name = f"PAINTSCHEME_{int(new_uid)}"

    sources = _actual_recipient_entries(
        recipient, team_driver_uids, livery_uids, int(new_uid)
    )
    (_score, template_raw, template, template_source, selected_template,
     mapping, donor, unused, planned_remaining_paint, driver_pair_map) = _choose_template(
        game,
        recipient_raw=recipient_raw,
        recipient=recipient,
        target_container=target_container,
        sources=sources,
        new_name=new_name,
    )

    out = bytearray(template_raw)
    transplants = []
    for dest, source in mapping:
        transplants.append(v10.transplant_entry(out, template, recipient, dest.name, source.name))
    rename = v10.rename_equal_length_entry(out, template, donor.name, new_name)
    rebuilt = bytes(out)

    from PIL import Image
    image = Image.open(str(image_path)); image.load()
    final, encoder = thumb.replace_payload(rebuilt, _import_entry(rebuilt, new_name), image)
    parsed = v10.parse_multi_arc(final)
    if parsed.count != template.count or len(final) != len(template_raw):
        raise ValueError("Fixed-template thumbnail import changed the stock count or size")
    v10.entry_by_name(parsed, new_name)
    for _dest, source in mapping:
        v10.entry_by_name(parsed, source.name)

    required_names = {e.name for e in sources} | {new_name}
    result_names = {e.name for e in parsed.entries}
    missing = sorted(required_names - result_names)
    if missing:
        raise ValueError("Fixed template lost required resources: " + ", ".join(missing))

    cdf_before = cdf.read_bytes()
    archive_size_before = archive.stat().st_size
    new_offset = _align(archive_size_before)
    cdf_after = _patch_cdf(cdf_before, cdf, target_container, new_offset, len(final))
    try:
        with archive.open("ab") as fh:
            if fh.tell() != archive_size_before:
                raise RuntimeError("ARCHIVE1 changed during fixed-template planning")
            if new_offset > archive_size_before:
                fh.write(b"\0" * (new_offset - archive_size_before))
            fh.write(final); fh.flush(); os.fsync(fh.fileno())
        _atomic(cdf, cdf_after)

        check_row = _row(_rows(cdf), target_container)
        if int(check_row.offset) != new_offset or int(check_row.size) != len(final):
            raise RuntimeError("cdfiles1 did not repoint to the fixed native bank")
        readback = _read(archive, check_row)
        if readback != final:
            raise RuntimeError("Fixed native Paint Select bank readback mismatch")
        v10.entry_by_name(v10.parse_multi_arc(readback), new_name)
    except Exception:
        try:
            with archive.open("r+b") as fh:
                fh.truncate(archive_size_before); fh.flush(); os.fsync(fh.fileno())
            _atomic(cdf, cdf_before)
        except Exception:
            pass
        raise

    actual_count = len(required_names)
    remaining_paint_slots = sum(1 for e in parsed.entries
                                if _kind(e) == "paintscheme" and e.name not in required_names)
    return {
        "ok": True,
        "version": VERSION,
        "architecture": "v0.10_fixed_count_paired_native_template_allocator",
        "target_container": target_container,
        "container": target_container,
        "method": "v0.10_fixed_count_paired_native_template_allocator",
        "game_safe_stock_legacy": True,
        "game_safe_same_bank_custom": False,
        "game_safe_raw_clone": False,
        "identity_name": donor.name,
        "template_container": selected_template,
        "requested_template_container": template_container,
        "template_source": template_source,
        "recipient_count_before": recipient.count,
        "result_count": parsed.count,
        "result_size": len(final),
        "new_entry": new_name,
        "native_slot_donor": donor.name,
        "encoder": encoder,
        "archive1_offset": new_offset,
        "required_resource_count": actual_count,
        "remaining_native_paint_slots": remaining_paint_slots,
        "planned_remaining_native_paint_slots": planned_remaining_paint,
        "transplants": transplants,
        "driver_pair_map": driver_pair_map,
        "paired_driver_art": True,
        "rename": rename,
        "result_sha256": _sha(final),
        "readback_verified": True,
    }
