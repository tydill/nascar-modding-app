#!/usr/bin/env python3
"""Game file verification for the NASCAR 15 Modding App.

Every corruption bug found during the moved-driver investigation was invisible
to the app: it wrote bad bytes, verified its own arithmetic, and reported
success. _validate_directory_header only inspected the header it had just
written. The readback in add_exact_resource compared the merged chunk against
the source chunk, which already contained the corruption.

This module checks properties of the RESULT instead. If a bank comes out of a
write malformed, these checks notice, whether or not anyone predicted that
particular failure.

Invariants, and what breaking each one does in-game:

  count_matches_header     entry count disagrees with the table size field
  name_table_readable      resource names cannot be resolved -> nothing loads
  name_table_size          name area size disagrees with the records in it
  names_in_order           name offsets not ascending (stock is always ascending)
  one_directory_header     a second, foreign bank header is present  -> fatal
  directory_header_correct the bank header does not describe this bank -> fatal
  resource_order_table     the 0..N-1 order table is missing/corrupt  -> fatal
  offsets_in_range         a resource points outside the data area
  offsets_do_not_overlap   two resources claim the same bytes
  name_table_aligned       name area does not start on a 16-byte boundary

Public API:
    verify_container(raw)            -> list[Finding]
    verify_archive(game, index)      -> Report
    verify_game(game, indexes)       -> Report
    describe(container_name)         -> friendly label
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

FILC = 0x436C6966
REC = 32
HEADER_SIZE = 0x80
FOOTER_PREFIX = 0x20
VERSION = "1.0"

# Checks whose failure means the game will crash rather than merely look wrong.
FATAL = {
    "name_table_readable", "one_directory_header", "directory_header_correct",
    "resource_order_table", "offsets_in_range", "offsets_do_not_overlap",
}

REPAIRABLE = {"resource_order_table", "one_directory_header"}


def align(v: int, b: int) -> int:
    return (v + b - 1) // b * b


@dataclass
class Finding:
    container: str
    check: str
    detail: str
    fatal: bool = False
    repairable: bool = False

    def friendly(self) -> str:
        return f"{describe(self.container)}: {self.detail}"


@dataclass
class Report:
    checked: int = 0
    skipped: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def fatal_count(self) -> int:
        return sum(1 for f in self.findings if f.fatal)

    def summary(self) -> str:
        if self.ok:
            return f"All {self.checked} game file groups look correct."
        n = len(self.findings)
        bad = len({f.container for f in self.findings})
        if self.fatal_count:
            return (f"{self.fatal_count} problem(s) that will crash the game, "
                    f"across {bad} file group(s). {n} finding(s) total.")
        return f"{n} problem(s) across {bad} file group(s). None should crash the game."

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "checked": self.checked, "skipped": self.skipped,
            "summary": self.summary(), "fatal": self.fatal_count,
            "findings": [
                {"container": f.container, "label": describe(f.container),
                 "check": f.check, "detail": f.detail,
                 "fatal": f.fatal, "repairable": f.repairable,
                 "message": f.friendly()}
                for f in self.findings
            ],
        }


def describe(name: str) -> str:
    """Human label for a container, so findings read like sentences."""
    n = (name or "").upper()
    if n.startswith("2DRIVERSELECTTD_"):
        uid = n[len("2DRIVERSELECTTD_"):].replace(".ARC", "")
        return f"Team {uid} driver artwork"
    if n.startswith("CUSTOMSCHEMETHUMBNAILS"):
        return "Custom paint thumbnails"
    if n.startswith("HDLIVERY_"):
        return "HD paint " + n[len("HDLIVERY_"):].replace(".ARC", "").replace("_", " ").title()
    if n.startswith("LIVERY_"):
        return "Paint " + n[len("LIVERY_"):].replace(".ARC", "").replace("_", " ").title()
    if n.startswith("2DRIVERSELECTMENUIMAGE"):
        return "Team logos"
    return name


# ---------------------------------------------------------------- container

def _find_name_table(raw: bytes, entries: list[dict]) -> int | None:
    """Locate the name area exactly.

    The last name record always finishes at end-of-file, so for a candidate
    name length L the table must start at end - (highest_name_ref + L + 1).
    Several candidates can produce printable text, so the accepted one must
    also satisfy the exact size identity: the records it implies add up to the
    size of the area. Scanning for printable runs instead of deriving the
    offset finds coincidental matches - that mistake made an earlier version of
    this check report a healthy file as corrupt.
    """
    if not entries:
        return None
    end = len(raw)
    top = max(int(e["name_ref"]) for e in entries)
    best = None
    for length in range(2, 80):
        start = end - (top + length + 1)
        if start < HEADER_SIZE:
            break
        names = []
        ok = True
        for e in entries:
            p = start + int(e["name_ref"])
            if p < 9 or p >= end:
                ok = False
                break
            z = raw.find(b"\0", p)
            if z < 0 or not (1 < z - p < 80):
                ok = False
                break
            if not all(32 <= c < 127 for c in raw[p:z]):
                ok = False
                break
            names.append((p, raw[p:z]))
        if not ok:
            continue
        # exact size identity: 4B crc + 4B crc + 00 + ascii + 00 per record
        if sum(9 + len(n) + 1 for _p, n in names) != end - start:
            continue
        # With a single resource the size identity is satisfied by any length,
        # so score candidates by how many stored name hashes actually match the
        # name they sit in front of. The hashes are plain zlib crc32 of the
        # lowercase and exact spellings.
        score = 0
        for p, n in names:
            try:
                lo, ex = struct.unpack_from("<II", raw, p - 9)
            except Exception:
                continue
            if lo == zlib.crc32(n.lower()) & 0xFFFFFFFF:
                score += 2
            if ex == zlib.crc32(n) & 0xFFFFFFFF:
                score += 2
            score += 1                      # prefer longer, better-formed names
        if best is None or score > best[0]:
            best = (score, start)
    return best[1] if best else None


def verify_container(raw: bytes, name: str = "") -> list[Finding]:
    """Check one ARCC image bank. Returns [] when everything is correct."""
    out: list[Finding] = []

    def bad(check: str, detail: str) -> None:
        out.append(Finding(name, check, detail,
                           fatal=check in FATAL, repairable=check in REPAIRABLE))

    if len(raw) < HEADER_SIZE or raw[:4] != b"ARCC":
        return out                      # not an image bank; nothing to say
    f04, count = struct.unpack_from("<II", raw, 4)
    if not (1 <= count <= 512) or f04 != count * 2 + 2:
        return out                      # a different ARCC variant

    base = HEADER_SIZE + count * REC
    if base + FOOTER_PREFIX > len(raw):
        bad("count_matches_header",
            f"claims {count} resources but the file is too small to hold them")
        return out

    entries = []
    for i in range(count):
        w = list(struct.unpack_from("<8I", raw, HEADER_SIZE + i * REC))
        entries.append({"index": i, "words": w, "data_off": w[5], "name_ref": w[6]})

    blob = _find_name_table(raw, entries)
    if blob is None:
        bad("name_table_readable",
            "the resource name table cannot be read, so the game cannot find "
            "anything inside this file")
        return out
    for e in entries:
        p = blob + int(e["name_ref"])
        e["name"] = raw[p:raw.find(b"\0", p)].decode("ascii", "replace")

    expect = sum(9 + len(e["name"]) + 1 for e in entries)
    if expect != len(raw) - blob:
        bad("name_table_size",
            f"the name area is {len(raw) - blob} bytes but its records add up "
            f"to {expect}")
    if blob % 16:
        bad("name_table_aligned",
            f"the name area starts at {blob}, which is not a 16-byte boundary")
    if any(entries[i]["name_ref"] >= entries[i + 1]["name_ref"] for i in range(count - 1)):
        bad("names_in_order", "resource names are not stored in table order")

    footer_len = align(FOOTER_PREFIX + count * 4, 0x10)
    fstart = blob - footer_len
    if fstart < base:
        bad("resource_order_table", "there is no room for the resource order table")
        return out

    order = list(struct.unpack_from(f"<{count}I", raw, fstart + FOOTER_PREFIX))
    if order != list(range(count)):
        bad("resource_order_table",
            "the resource order table is missing or damaged. The game will "
            "fail to load this file")

    got = (struct.unpack_from("<I", raw, base + 0x04)[0], raw[base + 0x0F],
           struct.unpack_from("<I", raw, base + 0x14)[0],
           int.from_bytes(raw[base + 0x1E:base + 0x20], "big"))
    want = (fstart - base, count * 4, blob - base - 0x20, len(raw) - blob)
    if got != want:
        bad("directory_header_correct",
            "the internal directory does not describe this file correctly")

    for e in entries:
        if int(e["data_off"]) == 0:
            continue
        blk = raw[base + e["data_off"]:base + e["data_off"] + FOOTER_PREFIX]
        if len(blk) == FOOTER_PREFIX and blk[8:12] == b"\xff\xff\xff\xff" and blk[12] == 0x42:
            claims, = struct.unpack_from("<I", blk, 4)
            bad("one_directory_header",
                f"'{e['name']}' carries a directory belonging to a different "
                f"file (claims a data area of {claims} bytes). The game will "
                f"fail to load this file")

    limit = fstart - base
    if any(not (0 <= int(e["data_off"]) < limit) for e in entries):
        bad("offsets_in_range", "a resource points outside this file's data area")
    offs = sorted({int(e["data_off"]) for e in entries})
    if any(a >= b for a, b in zip(offs, offs[1:])):
        bad("offsets_do_not_overlap", "two resources claim the same bytes")
    return out


# ------------------------------------------------------------------ archive

def _cdf_rows(path: Path) -> list[dict]:
    d = path.read_bytes()
    hdr = struct.unpack_from("<12I", d, 0)
    if hdr[0] != FILC:
        raise ValueError(f"{path.name} is not a game index file")
    n, strtab = hdr[8], hdr[10]
    sbase = len(d) - strtab

    def nm(off: int) -> str:
        p = sbase + off
        if p < 0 or p >= len(d):
            return ""
        e = d.find(b"\0", p)
        return d[p:e].decode("ascii", "replace") if e > p else ""

    best = None
    for start, lay in ((0x40, "A"), (0x50, "B")):
        pos, ok, rows, seen = start, 0, [], set()
        for _ in range(n):
            if pos + REC > sbase:
                break
            f = struct.unpack_from("<8I", d, pos)
            no = f[1] if lay == "A" else f[3]
            s = nm(no) if no < strtab else ""
            if s and all(32 <= ord(c) < 127 for c in s):
                ok += 1
                seen.add(s)
            rows.append({"fields": list(f), "name": s})
            pos += REC
        if ok > n * 0.8 and (best is None or len(seen) > best[1]):
            best = (lay, len(seen), rows)
    if best is None:
        raise ValueError(f"{path.name} has an unrecognized layout")
    lay, _, rows = best
    si, oi = (2, 5) if lay == "A" else (4, 7)
    for r in rows:
        r["size"], r["offset"] = r["fields"][si], r["fields"][oi]
    return rows


def verify_archive(game: str | Path, index: str = "1",
                   only: Iterable[str] | None = None,
                   max_size: int = 16 * 1024 * 1024) -> Report:
    game = Path(game)
    data = game / "data"
    suf = "" if str(index) == "0" else str(index)
    cdf, arc = data / f"cdfiles{suf}.dat", data / f"ARCHIVE{index}.AR"
    rep = Report()
    if not (cdf.exists() and arc.exists()):
        return rep
    wanted = {str(x).upper() for x in only} if only else None
    rows = _cdf_rows(cdf)
    with arc.open("rb") as fh:
        for r in rows:
            nm = r["name"] or ""
            if wanted and nm.upper() not in wanted:
                continue
            if r["size"] < HEADER_SIZE or r["size"] > max_size:
                rep.skipped += 1
                continue
            fh.seek(r["offset"])
            raw = fh.read(r["size"])
            if raw[:4] != b"ARCC":
                rep.skipped += 1
                continue
            rep.checked += 1
            rep.findings.extend(verify_container(raw, nm))
    return rep


def verify_game(game: str | Path, indexes: Iterable[str] = ("1",)) -> Report:
    total = Report()
    for i in indexes:
        r = verify_archive(game, str(i))
        total.checked += r.checked
        total.skipped += r.skipped
        total.findings.extend(r.findings)
    return total


def assert_container_ok(raw: bytes, name: str = "") -> None:
    """Raise if a freshly built container is malformed. Use after every write.

    This is the check that would have caught the foreign directory header and
    the missing order table on the day they were introduced, because it tests
    the artifact rather than re-deriving the numbers that produced it.
    """
    problems = [f for f in verify_container(raw, name) if f.fatal]
    if problems:
        raise ValueError(
            f"{name or 'container'} failed verification after writing: "
            + "; ".join(p.detail for p in problems))
