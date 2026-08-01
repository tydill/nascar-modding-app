#!/usr/bin/env python3
"""
NASCAR 15 V1.1 probe patcher.

Current targets:
  - Patch race lap constants inside DB_GAME_LOCAL_SCRIPT.PYC.
  - Patch same-size numeric fields inside NASCAR track *_SCR.ARC files.

This is intentionally conservative. By default it copies ARCHIVE0.AR to a new
patched file and only overwrites entries without changing their indexed size.
"""

from __future__ import annotations

import argparse
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path


TRACK_ALIASES = {
    "AUTOCLUB": "AUTOCLUB",
    "BRISTOL": "BRISTOL",
    "CHARLOTTE": "CHARLOTTE",
    "CHICAGO": "CHICAGO",
    "CHICAGOLAND": "CHICAGO",
    "DARLINGTON": "DARLINGTON",
    "DAYTONA": "DAYTONA",
    "DOVER": "DOVER",
    "HOMESTEAD": "HOMESTEAD",
    "INDY": "INDY",
    "INDIANAPOLIS": "INDY",
    "INFINEON": "INFINEON",
    "SONOMA": "INFINEON",
    "KANSAS": "KANSAS",
    "KENTUCKY": "KENTUCKY",
    "LASVEGAS": "LASVEGAS",
    "VEGAS": "LASVEGAS",
    "MARTINSVILLE": "MARTINSVILLE",
    "MICHIGAN": "MICHIGAN",
    "NEWHAMP": "NEWHAMP",
    "NEWHAMPSHIRE": "NEWHAMP",
    "PHOENIX": "PHOENIX",
    "POCONO": "POCONO",
    "RICHMOND": "RICHMOND",
    "TALLADEGA": "TALLADEGA",
    "TEXAS": "TEXAS",
    "WATKINS": "WATKINS",
    "WATKINSGLEN": "WATKINS",
    "ATLANTA": "ATLANTA",
}


SCR_KEYS = {
    "FRONT-DRAFT-DRAG",
    "REAR-DRAFT-DRAG",
    "SIDE-DRAFT-DRAG",
    "FRONT-DRAFT-DOWNFORCE",
    "REAR-DRAFT-DOWNFORCE",
    "OVERALL-DOWNFORCE-SCALE",
    "AI-LAT-GRIP-BOOST",
    "TAPE",
    "SPLITTER",
}


@dataclass
class CdfEntry:
    index: int
    name: str
    size: int
    archive_offset: int
    entry_offset: int


def find_string_base(cdf: bytes) -> int:
    marker = b"NAS4\\LANG\\"
    pos = cdf.find(marker)
    if pos < 0:
        raise ValueError("Could not find cdfiles string table marker NAS4\\LANG\\")
    # Name offsets in this cdfiles format are relative to one byte before the
    # first visible string. Earlier NASCAR 15 tools call this the string base.
    return pos - 1


def read_cdfiles(cdfiles_path: Path) -> dict[str, CdfEntry]:
    cdf = cdfiles_path.read_bytes()
    if cdf[:4] != b"filC":
        raise ValueError("cdfiles.dat does not start with filC")

    count = struct.unpack_from("<I", cdf, 0x20)[0]
    string_base = find_string_base(cdf)
    entries: dict[str, CdfEntry] = {}

    for i in range(count):
        entry_offset = 0x40 + i * 32
        if entry_offset + 32 > len(cdf):
            break

        _typ, name_off, size, _u1, _u2, archive_offset, _flags, _u3 = struct.unpack_from(
            "<IIIIIIII", cdf, entry_offset
        )
        name_pos = string_base + name_off
        if not (0 <= name_pos < len(cdf)):
            continue
        end = cdf.find(b"\x00", name_pos)
        if end < 0:
            continue

        name = cdf[name_pos:end].decode("latin1", "replace")
        entries[name.upper()] = CdfEntry(i, name, size, archive_offset, entry_offset)

    return entries


class Py25MarshalReader:
    """Small Python 2.5 marshal reader for finding top-level const offsets."""

    def __init__(self, data: bytes):
        self.data = data
        self.i = 0
        self.refs: list[object] = []

    def read(self, n: int) -> bytes:
        out = self.data[self.i : self.i + n]
        self.i += n
        return out

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def obj(self):
        start = self.i
        type_code = chr(self.read(1)[0])

        if type_code in "0N":
            return None
        if type_code == "F":
            return False
        if type_code == "T":
            return True
        if type_code == "i":
            return self.i32()
        if type_code == "I":
            return struct.unpack("<q", self.read(8))[0]
        if type_code == "g":
            return struct.unpack("<d", self.read(8))[0]
        if type_code == "f":
            n = self.read(1)[0]
            return float(self.read(n))
        if type_code in "st":
            n = self.i32()
            value = self.read(n).decode("latin1", "replace")
            if type_code == "t":
                self.refs.append(value)
            return value
        if type_code == "R":
            return self.refs[self.i32()]
        if type_code == "(":
            n = self.i32()
            return tuple(self.obj() for _ in range(n))
        if type_code == "[":
            n = self.i32()
            return [self.obj() for _ in range(n)]
        if type_code == "{":
            out = {}
            while True:
                key = self.obj()
                if key is None:
                    break
                out[key] = self.obj()
            return out
        if type_code == "u":
            n = self.i32()
            return self.read(n).decode("utf-8", "replace")
        if type_code == "c":
            return self.code_object()

        raise ValueError(f"Unsupported marshal type {type_code!r} at offset 0x{start:X}")

    def code_object(self):
        argcount = self.i32()
        nlocals = self.i32()
        stacksize = self.i32()
        flags = self.i32()
        code = self.obj()

        consts_type_offset = self.i
        consts_type = chr(self.read(1)[0])
        if consts_type != "(":
            raise ValueError(f"Expected const tuple at 0x{consts_type_offset:X}")
        const_count = self.i32()
        const_offsets = []
        consts = []
        for _ in range(const_count):
            const_offsets.append(self.i)
            consts.append(self.obj())

        return {
            "argcount": argcount,
            "nlocals": nlocals,
            "stacksize": stacksize,
            "flags": flags,
            "code": code,
            "consts": tuple(consts),
            "const_offsets": const_offsets,
            "names": self.obj(),
            "varnames": self.obj(),
            "freevars": self.obj(),
            "cellvars": self.obj(),
            "filename": self.obj(),
            "name": self.obj(),
            "firstlineno": self.i32(),
            "lnotab": self.obj(),
        }


def extract_entry(archive: Path, entry: CdfEntry) -> bytes:
    with archive.open("rb") as fh:
        fh.seek(entry.archive_offset)
        return fh.read(entry.size)


def write_entry(archive: Path, entry: CdfEntry, data: bytes) -> None:
    if len(data) != entry.size:
        raise ValueError(
            f"{entry.name} size changed from {entry.size} to {len(data)}. "
            "This probe patcher only does same-size installs."
        )
    with archive.open("r+b") as fh:
        fh.seek(entry.archive_offset)
        fh.write(data)


def patch_race_laps(pyc: bytes, event: str, laps: int) -> bytes:
    if not (1 <= laps <= 999):
        raise ValueError("laps must be 1-999")

    reader = Py25MarshalReader(pyc[8:])
    root = reader.obj()
    consts = root["consts"]
    offsets = root["const_offsets"]
    event_upper = event.upper()

    matches = []
    for i in range(len(consts) - 5):
        if not (
            isinstance(consts[i], int)
            and isinstance(consts[i + 1], int)
            and 1 <= consts[i + 1] <= 999
            and isinstance(consts[i + 2], int)
            and isinstance(consts[i + 3], str)
            and consts[i + 3].upper().startswith(("S_EVT", "S_EVENT"))
            and isinstance(consts[i + 4], int)
            and isinstance(consts[i + 5], str)
        ):
            continue

        uid = consts[i]
        old_laps = consts[i + 1]
        event_name = consts[i + 3]
        intro = consts[i + 5]

        if event_upper in {str(uid).upper(), event_name.upper(), intro.upper()}:
            matches.append((i + 1, uid, old_laps, event_name, intro))

    if not matches:
        raise ValueError(f"No race-lap record matched {event!r}")
    if len(matches) > 1:
        pretty = ", ".join(f"{m[3]} uid={m[1]} intro={m[4]}" for m in matches)
        raise ValueError(f"Ambiguous race match {event!r}: {pretty}")

    const_index, uid, old_laps, event_name, intro = matches[0]
    pyc_offset = 8 + offsets[const_index]
    if pyc[pyc_offset : pyc_offset + 1] != b"i":
        raise ValueError(f"Expected int marshal tag at 0x{pyc_offset:X}")

    out = bytearray(pyc)
    out[pyc_offset + 1 : pyc_offset + 5] = struct.pack("<i", laps)
    print(f"[laps] {event_name} uid={uid} intro={intro}: {old_laps} -> {laps}")
    print(f"[laps] patched DB_GAME_LOCAL_SCRIPT.PYC offset 0x{pyc_offset:X}")
    return bytes(out)


def patch_scr_value(scr: bytes, key: str, value: str, occurrence: int = 1) -> bytes:
    key = key.upper()
    if key not in SCR_KEYS:
        raise ValueError(f"Unsupported SCR key {key!r}. Known: {', '.join(sorted(SCR_KEYS))}")
    if occurrence < 1:
        raise ValueError("occurrence must be 1 or higher")

    data = bytearray(scr)
    key_bytes = key.encode("ascii")
    pos = -1
    for _ in range(occurrence):
        pos = scr.find(key_bytes, pos + 1)
        if pos < 0:
            raise ValueError(f"Could not find occurrence {occurrence} of {key}")

    cursor = pos + len(key_bytes)
    while cursor < len(scr) and scr[cursor] in b"\x00 \t\r\n":
        cursor += 1
    end = cursor
    while end < len(scr) and scr[end] not in b"\x00 \t\r\n{}":
        end += 1

    old = scr[cursor:end].decode("ascii", "replace")
    new = value.encode("ascii")
    old_len = end - cursor
    if len(new) != old_len:
        raise ValueError(
            f"{key} old value {old!r} has byte length {old_len}, "
            f"but new value {value!r} has byte length {len(new)}. "
            "Use a same-length value for this probe, e.g. 0.75 -> 0.80."
        )

    data[cursor:end] = new
    print(f"[scr] {key} occurrence {occurrence}: {old} -> {value} at file offset 0x{cursor:X}")
    return bytes(data)


def resolve_scr_name(track: str, mode: str) -> str:
    track_key = track.replace(" ", "").replace("_", "").replace("-", "").upper()
    if track_key not in TRACK_ALIASES:
        raise ValueError(f"Unknown track {track!r}")
    mode = mode.upper()
    if mode not in {"AI", "PLAYER"}:
        raise ValueError("mode must be AI or PLAYER")
    return f"NASCAR{TRACK_ALIASES[track_key]}{mode}_SCR.ARC"


def prepare_output_archive(input_archive: Path, output_archive: Path, in_place: bool) -> Path:
    if in_place:
        return input_archive
    if output_archive.resolve() != input_archive.resolve():
        print(f"[copy] {input_archive} -> {output_archive}")
        shutil.copy2(input_archive, output_archive)
    return output_archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch NASCAR 15 ARCHIVE0 V1.1 probe values.")
    parser.add_argument("--archive", required=True, type=Path, help="Path to ARCHIVE0.AR")
    parser.add_argument("--cdfiles", required=True, type=Path, help="Path to matching cdfiles.dat")
    parser.add_argument("--out-archive", type=Path, help="Patched output archive path")
    parser.add_argument("--in-place", action="store_true", help="Patch ARCHIVE0.AR directly")

    sub = parser.add_subparsers(dest="command", required=True)

    laps = sub.add_parser("set-race-laps", help="Patch a race lap count in DB_GAME_LOCAL_SCRIPT.PYC")
    laps.add_argument("--event", required=True, help="Event string, intro string, or UID, e.g. S_EVT_DAYTONA_50")
    laps.add_argument("--laps", required=True, type=int, help="New lap count")

    scr = sub.add_parser("set-scr-value", help="Patch a same-size numeric value in a track *_SCR.ARC")
    scr.add_argument("--track", required=True, help="Track name, e.g. DAYTONA")
    scr.add_argument("--mode", required=True, choices=["AI", "PLAYER", "ai", "player"])
    scr.add_argument("--key", required=True, help="SCR key, e.g. AI-LAT-GRIP-BOOST")
    scr.add_argument("--value", required=True, help="Same-byte-length replacement value, e.g. 0.20")
    scr.add_argument("--occurrence", type=int, default=1, help="Which occurrence of the key to patch")

    args = parser.parse_args()

    entries = read_cdfiles(args.cdfiles)
    if not args.archive.exists():
        raise FileNotFoundError(args.archive)

    out_archive = args.out_archive
    if out_archive is None:
        out_archive = args.archive.with_name(args.archive.stem + "_patched" + args.archive.suffix)
    target_archive = prepare_output_archive(args.archive, out_archive, args.in_place)

    if args.command == "set-race-laps":
        entry = entries.get("DB_GAME_LOCAL_SCRIPT.PYC")
        if entry is None:
            raise ValueError("DB_GAME_LOCAL_SCRIPT.PYC not found in cdfiles.dat")
        data = extract_entry(target_archive, entry)
        patched = patch_race_laps(data, args.event, args.laps)
        write_entry(target_archive, entry, patched)

    elif args.command == "set-scr-value":
        scr_name = resolve_scr_name(args.track, args.mode)
        entry = entries.get(scr_name.upper())
        if entry is None:
            raise ValueError(f"{scr_name} not found in cdfiles.dat")
        data = extract_entry(target_archive, entry)
        patched = patch_scr_value(data, args.key, args.value, args.occurrence)
        write_entry(target_archive, entry, patched)
        print(f"[scr] patched {scr_name}")

    print(f"[done] patched archive: {target_archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
