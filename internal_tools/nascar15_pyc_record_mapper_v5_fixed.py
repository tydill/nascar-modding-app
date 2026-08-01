#!/usr/bin/env python3
r"""
nascar15_pyc_record_mapper.py

NASCAR 15 ARCHIVE0/PYC record mapper + safe same-size numeric patcher.
V5: adds STORE_ATTR/PatchSetAttr emulation so post-constructor DB roster links resolve.

What this is for:
  - Map generated Python database records inside .PYC files.
  - Trace Daytona/etc from DB_GAME_LOCAL_SCRIPT.PYC -> DB_AICONFIG_SCRIPT.PYC AI track config.
  - Patch same-size numeric/string constants inside .PYC entries and write a new ARCHIVE0.AR.

Important:
  - Float/int edits are same-size and archive-safe.
  - String edits require the same byte length.
  - Bool edits are not supported yet because marshal bools have no payload byte.
  - Put this script beside nascar15_v11_probe_patcher.py so it can reuse read_cdfiles().

Command style:
  py .\nascar15_pyc_record_mapper.py <command> --archive ... --cdfiles ... [options]
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


# ----------------------------
# cdfiles / archive helpers
# ----------------------------

@dataclass
class CDFEntryLite:
    name: str
    offset: int
    size: int


def import_patcher(patcher_path: Path):
    spec = importlib.util.spec_from_file_location("nascar15_v11_probe_patcher_imported", str(patcher_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import patcher from {patcher_path}")
    mod = importlib.util.module_from_spec(spec)
    # Python 3.14 dataclasses expects this during decorators.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, "read_cdfiles"):
        raise RuntimeError("Imported patcher does not expose read_cdfiles(). Use the v1.1 probe patcher.")
    return mod


def _get_field(entry: Any, candidates: list[str]) -> Any:
    if isinstance(entry, dict):
        low = {str(k).lower(): k for k in entry.keys()}
        for c in candidates:
            if c in entry:
                return entry[c]
            if c.lower() in low:
                return entry[low[c.lower()]]

    for c in candidates:
        if hasattr(entry, c):
            return getattr(entry, c)

    for c in candidates:
        for real in dir(entry):
            if real.lower() == c.lower():
                return getattr(entry, real)

    raise AttributeError(f"Could not find any of fields {candidates!r} on entry {entry!r}")


def normalize_entries(raw_entries: Iterable[Any]) -> list[CDFEntryLite]:
    out: list[CDFEntryLite] = []
    if isinstance(raw_entries, dict):
        iterator = raw_entries.items()
    else:
        iterator = [(None, e) for e in raw_entries]

    for name_hint, e in iterator:
        try:
            if isinstance(e, dict):
                name = e.get("name") or e.get("filename") or e.get("file_name") or e.get("path") or name_hint
                offset = _get_field(e, ["archive_offset", "arch_offset", "offset", "data_offset", "file_offset"])
                size = _get_field(e, ["size", "indexed_size", "file_size", "length", "data_size"])
            elif isinstance(e, (tuple, list)):
                if len(e) >= 3 and isinstance(e[0], (str, bytes)):
                    name, offset, size = e[0], e[1], e[2]
                elif len(e) >= 2 and name_hint is not None:
                    name, offset, size = name_hint, e[0], e[1]
                else:
                    raise AttributeError("tuple/list shape not understood")
            else:
                name = name_hint if name_hint is not None else _get_field(e, ["name", "filename", "file_name", "path"])
                offset = _get_field(e, ["archive_offset", "arch_offset", "offset", "data_offset", "file_offset"])
                size = _get_field(e, ["size", "indexed_size", "file_size", "length", "data_size"])

            if isinstance(name, bytes):
                name = name.decode("ascii", errors="replace")
            out.append(CDFEntryLite(str(name), int(offset), int(size)))
        except Exception:
            continue
    return out


def load_entries(cdfiles_path: Path, patcher_path: Path) -> list[CDFEntryLite]:
    mod = import_patcher(patcher_path)
    raw = mod.read_cdfiles(cdfiles_path)
    entries = normalize_entries(raw)
    if not entries:
        raise RuntimeError("Could not normalize cdfiles entries.")
    return entries


def find_entry(entries: list[CDFEntryLite], target: str) -> CDFEntryLite:
    target_u = target.upper()
    exact = [e for e in entries if e.name.upper() == target_u]
    if exact:
        return exact[0]
    ends = [e for e in entries if e.name.upper().endswith(target_u)]
    if ends:
        return ends[0]
    contains = [e for e in entries if target_u in e.name.upper()]
    if contains:
        if len(contains) > 1:
            print(f"[!] Multiple cdfiles matches for {target!r}; using first:", file=sys.stderr)
            for e in contains[:10]:
                print(f"    {e.name}", file=sys.stderr)
        return contains[0]
    raise FileNotFoundError(f"No cdfiles entry matched {target!r}")


def extract_entry(archive_path: Path, entry: CDFEntryLite) -> bytes:
    with archive_path.open("rb") as f:
        f.seek(entry.offset)
        return f.read(entry.size)


def install_entry_same_size(src_archive: Path, out_archive: Path, entry: CDFEntryLite, new_data: bytes):
    if len(new_data) != entry.size:
        raise ValueError(f"Entry size changed: old={entry.size} new={len(new_data)}. Refusing to patch archive.")
    if src_archive.resolve() != out_archive.resolve():
        shutil.copyfile(src_archive, out_archive)
    with out_archive.open("r+b") as f:
        f.seek(entry.offset)
        f.write(new_data)


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


# ----------------------------
# Python 2 marshal parser with offsets
# ----------------------------

@dataclass
class CodeObj:
    name: str
    filename: str
    firstlineno: int
    argcount: int
    nlocals: int
    stacksize: int
    flags: int
    code_bytes: bytes
    consts: list["MVal"]
    names: list[str]
    varnames: list[str]
    freevars: list[str]
    cellvars: list[str]


@dataclass(eq=False)
class MVal:
    tag: str
    value: Any
    tag_offset: int
    payload_offset: int
    payload_size: int
    raw: bytes = b""

    def __hash__(self) -> int:
        return hash((self.tag, self.tag_offset, self.payload_offset, self.payload_size))

    def plain(self) -> Any:
        if isinstance(self.value, CodeObj):
            return f"<code {self.value.name}>"
        if isinstance(self.value, list):
            return [plain(x) for x in self.value]
        if isinstance(self.value, dict):
            return {plain(k): plain(v) for k, v in self.value.items()}
        return self.value


def plain(x: Any) -> Any:
    if isinstance(x, MVal):
        return x.plain()
    if isinstance(x, ObjCall):
        return x
    if isinstance(x, list):
        return [plain(v) for v in x]
    if isinstance(x, tuple):
        return tuple(plain(v) for v in x)
    return x


def decode_bytes(raw: bytes) -> str:
    try:
        if all((32 <= b <= 126) or b in (9, 10, 13) for b in raw):
            return raw.decode("ascii", errors="replace")
        return raw.decode("latin1", errors="replace")
    except Exception:
        return raw.decode("latin1", errors="replace")


class MarshalReader:
    """Python 2.x marshal parser. It preserves payload offsets for patching."""

    def __init__(self, data: bytes, start_pos: int = 8):
        self.data = data
        self.pos = start_pos
        self.string_refs: list[MVal] = []

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise EOFError(f"marshal read past EOF at 0x{self.pos:X}, need {n}")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.read(1)[0]

    def i32_raw(self) -> tuple[int, int]:
        off = self.pos
        return struct.unpack("<i", self.read(4))[0], off

    def i32(self) -> int:
        return self.i32_raw()[0]

    def obj(self) -> MVal:
        tag_offset = self.pos
        tag_b = self.read(1)
        if not tag_b:
            raise EOFError("marshal EOF reading tag")
        tag_ord = tag_b[0]
        tag = chr(tag_ord & 0x7F)  # tolerate ref flag if ever seen

        def mv(value: Any, payload_offset: int = tag_offset + 1, payload_size: int = 0, raw: bytes = b""):
            return MVal(tag, value, tag_offset, payload_offset, payload_size, raw)

        if tag == "0":
            return mv({"$type": "NULL"})
        if tag == "N":
            return mv(None)
        if tag == "F":
            return mv(False)
        if tag == "T":
            return mv(True)
        if tag == "S":
            return mv({"$type": "StopIteration"})
        if tag == ".":
            return mv(Ellipsis)

        if tag == "i":
            value, off = self.i32_raw()
            return mv(value, off, 4, self.data[off:off + 4])

        if tag == "I":
            off = self.pos
            lo = self.i32() & 0xFFFFFFFF
            hi = self.i32()
            return mv((hi << 32) | lo, off, 8, self.data[off:off + 8])

        if tag == "f":
            n_off = self.pos
            n = self.u8()
            payload_off = self.pos
            raw = self.read(n)
            try:
                value = float(raw.decode("ascii"))
            except Exception:
                value = raw.decode("latin1", errors="replace")
            return mv(value, payload_off, n, raw)

        if tag == "g":
            off = self.pos
            raw = self.read(8)
            return mv(struct.unpack("<d", raw)[0], off, 8, raw)

        if tag == "x":
            n1 = self.u8()
            raw1 = self.read(n1)
            n2 = self.u8()
            raw2 = self.read(n2)
            return mv(complex(float(raw1.decode("ascii")), float(raw2.decode("ascii"))))

        if tag == "y":
            off = self.pos
            real = struct.unpack("<d", self.read(8))[0]
            imag = struct.unpack("<d", self.read(8))[0]
            return mv(complex(real, imag), off, 16, self.data[off:off + 16])

        if tag == "l":
            off = self.pos
            n = self.i32()
            sign = -1 if n < 0 else 1
            n_abs = abs(n)
            value = 0
            for i in range(n_abs):
                digit = struct.unpack("<H", self.read(2))[0]
                value += digit << (15 * i)
            return mv(sign * value, off, self.pos - off, self.data[off:self.pos])

        if tag in ("s", "t"):
            n, len_off = self.i32_raw()
            payload_off = self.pos
            raw = self.read(n)
            s = decode_bytes(raw)
            out = mv(s, payload_off, n, raw)
            if tag == "t":
                self.string_refs.append(out)
            return out

        if tag == "u":
            n, len_off = self.i32_raw()
            payload_off = self.pos
            raw = self.read(n)
            s = raw.decode("utf-8", errors="replace")
            return mv(s, payload_off, n, raw)

        if tag == "R":
            idx, off = self.i32_raw()
            try:
                ref = self.string_refs[idx]
                return MVal("R", ref.value, tag_offset, off, 4, self.data[off:off + 4])
            except Exception:
                return mv({"$type": "BAD_STRINGREF", "idx": idx}, off, 4, self.data[off:off + 4])

        if tag in ("(", ")"):
            n = self.i32() if tag == "(" else self.u8()
            items = [self.obj() for _ in range(n)]
            return mv(items, tag_offset + 1, self.pos - tag_offset - 1)

        if tag == "[":
            n = self.i32()
            items = [self.obj() for _ in range(n)]
            return mv(items, tag_offset + 1, self.pos - tag_offset - 1)

        if tag == "{":
            d = {}
            while True:
                key = self.obj()
                if isinstance(key.value, dict) and key.value.get("$type") == "NULL":
                    break
                val = self.obj()
                try:
                    d[key] = val
                except TypeError:
                    d[repr(key)] = val
            return mv(d, tag_offset + 1, self.pos - tag_offset - 1)

        if tag == "<":
            n = self.i32()
            items = [self.obj() for _ in range(n)]
            return mv({"$type": "set", "items": items}, tag_offset + 1, self.pos - tag_offset - 1)

        if tag == ">":
            n = self.i32()
            items = [self.obj() for _ in range(n)]
            return mv({"$type": "frozenset", "items": items}, tag_offset + 1, self.pos - tag_offset - 1)

        if tag == "c":
            code = self.code_obj()
            return mv(code, tag_offset + 1, self.pos - tag_offset - 1)

        raise ValueError(f"Unsupported marshal tag {tag!r} at byte 0x{tag_offset:X}")

    def code_obj(self) -> CodeObj:
        argcount = self.i32()
        nlocals = self.i32()
        stacksize = self.i32()
        flags = self.i32()

        code_val = self.obj()
        consts_val = self.obj()
        names_val = self.obj()
        varnames_val = self.obj()
        freevars_val = self.obj()
        cellvars_val = self.obj()
        filename_val = self.obj()
        name_val = self.obj()
        firstlineno = self.i32()
        lnotab_val = self.obj()

        code_bytes = code_val.raw if isinstance(code_val.raw, bytes) and code_val.raw else bytes(str(code_val.value), "latin1", errors="replace")

        def tuple_strings(v: MVal) -> list[str]:
            if not isinstance(v.value, list):
                return []
            return [str(plain(x)) for x in v.value]

        return CodeObj(
            name=str(plain(name_val)),
            filename=str(plain(filename_val)),
            firstlineno=firstlineno,
            argcount=argcount,
            nlocals=nlocals,
            stacksize=stacksize,
            flags=flags,
            code_bytes=code_bytes,
            consts=consts_val.value if isinstance(consts_val.value, list) else [],
            names=tuple_strings(names_val),
            varnames=tuple_strings(varnames_val),
            freevars=tuple_strings(freevars_val),
            cellvars=tuple_strings(cellvars_val),
        )


def parse_pyc(data: bytes) -> MVal:
    if len(data) < 9:
        raise ValueError("Too small to be a .pyc")
    return MarshalReader(data, start_pos=8).obj()


def walk_code_objects(obj: Any):
    if isinstance(obj, MVal):
        if isinstance(obj.value, CodeObj):
            yield obj.value
            for c in obj.value.consts:
                yield from walk_code_objects(c)
        elif isinstance(obj.value, list):
            for x in obj.value:
                yield from walk_code_objects(x)
        elif isinstance(obj.value, dict):
            for k, v in obj.value.items():
                yield from walk_code_objects(k)
                yield from walk_code_objects(v)


# ----------------------------
# Schema and record mapping
# ----------------------------

@dataclass
class Schema:
    class_name: str
    fields: list[str]
    firstlineno: int
    filename: str


@dataclass
class Sym:
    name: str


@dataclass
class Attr:
    obj: Any
    name: str


@dataclass
class Subscr:
    obj: Any
    key: Any


@dataclass
class Func:
    name: str
    code: Optional[CodeObj] = None


@dataclass
class ObjCall:
    func: str
    args: list[Any]
    call_offset: int


@dataclass
class Record:
    class_name: str
    uid: Any
    fields: dict[str, Any]
    call_offset: int
    assigned_type: str = ""
    assigned_key: Any = None


EXCLUDE_RECORD_CLASSES = {"BaseGDTObject_c", "UnexportedPointer_c"}


def build_schemas(root: MVal) -> dict[str, Schema]:
    schemas: dict[str, Schema] = {}
    for co in walk_code_objects(root):
        if not co.name.endswith("_c"):
            continue
        init = None
        for c in co.consts:
            if isinstance(c.value, CodeObj) and c.value.name == "__init__":
                init = c.value
                break
        if init:
            fields = init.varnames[1:]  # skip self
        else:
            fields = []
        # Generated GDT classes have useful __init__ varnames.
        if fields:
            schemas[co.name] = Schema(co.name, fields, co.firstlineno, co.filename)
    return schemas


def value_to_display(v: Any) -> str:
    if isinstance(v, MVal):
        if isinstance(v.value, CodeObj):
            return f"<code {v.value.name}>"
        return str(v.value)
    if isinstance(v, Sym):
        return v.name
    if isinstance(v, Attr):
        return f"{value_to_display(v.obj)}.{v.name}"
    if isinstance(v, Subscr):
        return f"{value_to_display(v.obj)}[{value_to_display(v.key)}]"
    if isinstance(v, Func):
        return f"<func {v.name}>"
    if isinstance(v, ObjCall):
        return f"{v.func}(" + ", ".join(value_to_display(a) for a in v.args) + ")"
    if isinstance(v, list):
        return "[" + ", ".join(value_to_display(x) for x in v) + "]"
    if isinstance(v, tuple):
        return "(" + ", ".join(value_to_display(x) for x in v) + ")"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{value_to_display(k)}: {value_to_display(val)}" for k, val in list(v.items())[:8]) + ("..." if len(v) > 8 else "") + "}"
    return str(v)


def value_plain_for_compare(v: Any) -> Any:
    if isinstance(v, MVal):
        return v.value
    if isinstance(v, ObjCall) and v.func == "UnexportedPointer_c" and v.args:
        return value_plain_for_compare(v.args[0])
    return v


def pointer_to_int(v: Any) -> Optional[int]:
    p = value_plain_for_compare(v)
    if isinstance(p, bool):
        return None
    if isinstance(p, int):
        return p
    if isinstance(p, float) and p.is_integer():
        return int(p)
    try:
        s = str(p).strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
    except Exception:
        pass
    return None


def func_name(v: Any) -> Optional[str]:
    if isinstance(v, Sym):
        return v.name
    if isinstance(v, Func):
        return v.name
    if isinstance(v, Attr):
        return v.name
    return None


# Python 2.x opcode names we need. Unknown opcodes are tolerated.
HAVE_ARGUMENT = 90
OP = {
    0: "STOP_CODE",
    1: "POP_TOP",
    2: "ROT_TWO",
    3: "ROT_THREE",
    4: "DUP_TOP",
    5: "ROT_FOUR",
    9: "NOP",
    23: "BINARY_ADD",
    24: "BINARY_SUBTRACT",
    25: "BINARY_SUBSCR",
    54: "STORE_MAP",
    60: "STORE_SUBSCR",
    83: "RETURN_VALUE",
    84: "IMPORT_STAR",
    87: "POP_BLOCK",
    88: "END_FINALLY",
    89: "BUILD_CLASS",
    90: "STORE_NAME",
    91: "DELETE_NAME",
    92: "UNPACK_SEQUENCE",
    93: "FOR_ITER",
    95: "STORE_ATTR",
    97: "STORE_GLOBAL",
    99: "DUP_TOPX",
    100: "LOAD_CONST",
    101: "LOAD_NAME",
    102: "BUILD_TUPLE",
    103: "BUILD_LIST",
    104: "BUILD_MAP",
    105: "LOAD_ATTR",
    106: "COMPARE_OP",
    107: "IMPORT_NAME",
    108: "IMPORT_FROM",
    110: "JUMP_FORWARD",
    111: "JUMP_IF_FALSE_OR_POP",
    112: "JUMP_IF_TRUE_OR_POP",
    113: "JUMP_ABSOLUTE",
    114: "POP_JUMP_IF_FALSE",
    115: "POP_JUMP_IF_TRUE",
    116: "LOAD_GLOBAL",
    119: "CONTINUE_LOOP",
    120: "SETUP_LOOP",
    121: "SETUP_EXCEPT",
    122: "SETUP_FINALLY",
    124: "LOAD_FAST",
    125: "STORE_FAST",
    126: "DELETE_FAST",
    130: "RAISE_VARARGS",
    131: "CALL_FUNCTION",
    132: "MAKE_FUNCTION",
    133: "BUILD_SLICE",
    134: "MAKE_CLOSURE",
    135: "LOAD_CLOSURE",
    136: "LOAD_DEREF",
    137: "STORE_DEREF",
    140: "CALL_FUNCTION_VAR",
    141: "CALL_FUNCTION_KW",
    142: "CALL_FUNCTION_VAR_KW",
    143: "EXTENDED_ARG",
}


def popn(stack: list[Any], n: int) -> list[Any]:
    if n <= 0:
        return []
    if len(stack) < n:
        vals = stack[:]
        stack.clear()
        return vals
    vals = stack[-n:]
    del stack[-n:]
    return vals



def hashable_key(v: Any) -> Any:
    """Convert parsed marshal/VM values into usable dictionary keys.

    V4 note:
      Some generated Python 2 bytecode leaves MVal wrappers or VM expression
      objects as dictionary keys during DATA / ApplyPatch construction.  Dict
      insertion must never crash the mapper, so normalize aggressively.
    """
    p = value_plain_for_compare(v)

    # Unwrap nested MVal wrappers.
    guard = 0
    while isinstance(p, MVal) and guard < 8:
        p = value_plain_for_compare(p)
        guard += 1

    if isinstance(p, ObjCall):
        # UnexportedPointer_c(1234) is a DB pointer; use the pointed UID.
        if p.func == "UnexportedPointer_c" and p.args:
            return hashable_key(p.args[0])
        return value_to_display(p)

    if isinstance(p, Sym):
        return p.name

    if isinstance(p, (Attr, Subscr, Func)):
        return value_to_display(p)

    if isinstance(p, (list, tuple, dict, set)):
        return value_to_display(p)

    try:
        hash(p)
        return p
    except Exception:
        return value_to_display(p)


def class_name_from_data_key(type_name: Any) -> str:
    s = str(hashable_key(type_name))
    if s.startswith("__"):
        s = s[2:]
    if s.endswith("_c"):
        return s
    return s + "_c"


def find_record_for_patch(records: list[Record], class_name: str, uid: Any) -> Optional[Record]:
    uid_p = hashable_key(uid)
    class_u = class_name.upper()
    for r in records:
        if r.class_name.upper() == class_u and str(r.uid).upper() == str(uid_p).upper():
            return r
    return None


def preserve_patch_mvals(value: Any) -> Any:
    """Normalize patch containers while retaining direct marshal-value leaves.

    Older mapper builds flattened ApplyPatch dictionaries with
    value_plain_for_compare(), which discarded the MVal origin of bool/numeric
    leaves.  The editor then knew the displayed value but could not identify
    the exact bytecode constant feeding that field.
    """
    if isinstance(value, MVal):
        if isinstance(value.value, dict):
            return {hashable_key(k): preserve_patch_mvals(v) for k, v in value.value.items()}
        if isinstance(value.value, (list, tuple)):
            return type(value.value)(preserve_patch_mvals(v) for v in value.value)
        return value
    if isinstance(value, dict):
        return {hashable_key(k): preserve_patch_mvals(v) for k, v in value.items()}
    if isinstance(value, list):
        return [preserve_patch_mvals(v) for v in value]
    if isinstance(value, tuple):
        return tuple(preserve_patch_mvals(v) for v in value)
    return value


def apply_patch_dict_to_records(records: list[Record], patch_data: Any, debug: bool = False) -> int:
    """Emulate generated ApplyPatch() enough to resolve post-constructor DB links.

    Generated DB files often build objects with pointer fields set to None, then call
    ApplyPatch({'TYPE': {UID: {'Field': value}}}, ...).  For mapping, we need those
    patched values so TrackPointer / AITrackProfile chains resolve.
    """
    patch_data = preserve_patch_mvals(patch_data)
    if not isinstance(patch_data, dict):
        return 0

    changed = 0
    for type_name, type_data in list(patch_data.items()):
        class_name = class_name_from_data_key(type_name)
        type_data = preserve_patch_mvals(type_data)
        if not isinstance(type_data, dict):
            continue
        for item_key, item in list(type_data.items()):
            rec = find_record_for_patch(records, class_name, item_key)
            item = preserve_patch_mvals(item)

            # Existing object patch: {'FieldName': new_value, ...}
            if rec is not None and isinstance(item, dict):
                for member, value in list(item.items()):
                    member_name = str(hashable_key(member))
                    rec.fields[member_name] = value
                    changed += 1
                continue

            # New object insertion patch. Usually not needed for trace resolving, but
            # handle simple constructor calls so record browsing stays complete.
            if rec is None and isinstance(item, ObjCall):
                # The object call was already recorded when constructed, so only attach
                # assigned context if we can find it by call offset.
                for r in records:
                    if r.call_offset == item.call_offset:
                        r.assigned_type = str(hashable_key(type_name))
                        r.assigned_key = hashable_key(item_key)
                        changed += 1
                        break

    if debug and changed:
        print(f"[debug] ApplyPatch emulation changed {changed} record fields", file=sys.stderr)
    return changed



def data_ref_to_class_uid(expr: Any) -> tuple[Optional[str], Any]:
    """Resolve symbolic DATA[__TYPE][UID] expressions when dict lookup didn't materialize."""
    if isinstance(expr, Subscr):
        uid = hashable_key(expr.key)
        inner = expr.obj
        if isinstance(inner, Subscr):
            base = inner.obj
            type_key = inner.key
            if isinstance(base, Sym) and base.name == "DATA":
                return class_name_from_data_key(type_key), uid
    return None, None


def record_from_obj(records: list[Record], obj: Any) -> Optional[Record]:
    """Resolve an expression/object back to the generated DB record it represents."""
    p = value_plain_for_compare(obj)

    # Direct constructor call object, e.g. DRIVERCONFIG_c(...)
    if isinstance(p, ObjCall):
        for r in reversed(records):
            if r.call_offset == p.call_offset:
                return r
        if p.args:
            uid = hashable_key(p.args[0])
            for r in reversed(records):
                if r.class_name == p.func and str(r.uid).upper() == str(uid).upper():
                    return r

    # Symbolic DATA[__TYPE][UID] reference.
    cls, uid = data_ref_to_class_uid(p)
    if cls is not None:
        return find_record_for_patch(records, cls, uid)

    return None


def patchsetattr_to_record(records: list[Record], args: list[Any], debug: bool = False) -> bool:
    """Emulate common PatchSetAttr(record, field, value) call shapes."""
    if not args:
        return False

    rec = None
    rec_arg_i = -1
    for i, a in enumerate(args):
        rec = record_from_obj(records, a)
        if rec is not None:
            rec_arg_i = i
            break
    if rec is None:
        return False

    field_i = -1
    field_name = None
    for i, a in enumerate(args):
        if i == rec_arg_i:
            continue
        p = value_plain_for_compare(a)
        if isinstance(p, str):
            field_i = i
            field_name = p
            break

    if not field_name:
        return False

    # Prefer the argument right after the field name as value; otherwise last arg.
    if field_i + 1 < len(args):
        val = args[field_i + 1]
    else:
        val = args[-1]
    rec.fields[str(field_name)] = val
    if debug:
        print(f"[debug] PatchSetAttr {rec.class_name} UID={rec.uid} {field_name}={value_to_display(val)}", file=sys.stderr)
    return True

def map_records(root: MVal, schemas: dict[str, Schema], debug: bool = False) -> list[Record]:
    if not isinstance(root.value, CodeObj):
        raise ValueError("Root pyc object is not a code object")

    co = root.value
    code = co.code_bytes
    stack: list[Any] = []
    env: dict[str, Any] = {}
    records: list[Record] = []

    i = 0
    ext = 0
    while i < len(code):
        off = i
        op = code[i]
        i += 1
        arg = None
        if op >= HAVE_ARGUMENT:
            if i + 2 > len(code):
                break
            arg = code[i] | (code[i + 1] << 8) | ext
            i += 2
            if op == 143:  # EXTENDED_ARG
                ext = arg << 16
                continue
            ext = 0

        opname = OP.get(op, f"OP_{op}")

        try:
            if opname == "LOAD_CONST":
                stack.append(co.consts[arg] if arg is not None and arg < len(co.consts) else Sym(f"<const {arg}>"))

            elif opname in ("LOAD_NAME", "LOAD_GLOBAL"):
                name = co.names[arg] if arg is not None and arg < len(co.names) else f"<name {arg}>"
                stack.append(env.get(name, Sym(name)))

            elif opname == "STORE_NAME":
                name = co.names[arg] if arg is not None and arg < len(co.names) else f"<name {arg}>"
                env[name] = stack.pop() if stack else Sym("<empty>")

            elif opname == "STORE_GLOBAL":
                if stack:
                    stack.pop()

            elif opname == "LOAD_ATTR":
                name = co.names[arg] if arg is not None and arg < len(co.names) else f"<attr {arg}>"
                obj = stack.pop() if stack else Sym("<empty>")
                stack.append(Attr(obj, name))

            elif opname == "STORE_ATTR":
                # Python 2 STORE_ATTR: TOS.name = TOS1, then pop object and value.
                # Generated GDT files use this heavily after construction, e.g.
                # DATA[__DRIVERCONFIG][25127].Series = DATA[__RACESERIES][25040]
                name = co.names[arg] if arg is not None and arg < len(co.names) else f"<attr {arg}>"
                obj = stack.pop() if stack else Sym("<obj>")
                val = stack.pop() if stack else Sym("<value>")
                rec = record_from_obj(records, obj)
                if rec is not None:
                    rec.fields[str(name)] = val
                    if debug:
                        print(f"[debug] STORE_ATTR {rec.class_name} UID={rec.uid} {name}={value_to_display(val)}", file=sys.stderr)

            elif opname == "BUILD_TUPLE":
                stack.append(tuple(popn(stack, int(arg or 0))))

            elif opname == "BUILD_LIST":
                stack.append(popn(stack, int(arg or 0)))

            elif opname == "BUILD_MAP":
                stack.append({})

            elif opname == "STORE_MAP":
                # Python 2 STORE_MAP is used for dict literals.
                # The dict stays on stack. Normalize keys immediately.
                if len(stack) >= 3:
                    key = stack.pop()
                    val = stack.pop()
                    d = stack[-1]
                    if isinstance(d, dict):
                        k = hashable_key(key)
                        try:
                            d[k] = val
                        except Exception:
                            d[value_to_display(k)] = val

            elif opname == "BINARY_SUBSCR":
                key = stack.pop() if stack else Sym("<key>")
                obj = stack.pop() if stack else Sym("<obj>")
                if isinstance(obj, dict):
                    k = hashable_key(key)
                    stack.append(obj.get(k, Subscr(obj, key)))
                elif isinstance(obj, (list, tuple)) and isinstance(value_plain_for_compare(key), int):
                    idx = value_plain_for_compare(key)
                    try:
                        stack.append(obj[idx])
                    except Exception:
                        stack.append(Subscr(obj, key))
                else:
                    stack.append(Subscr(obj, key))

            elif opname == "STORE_SUBSCR":
                key = stack.pop() if stack else Sym("<key>")
                obj = stack.pop() if stack else Sym("<obj>")
                val = stack.pop() if stack else Sym("<value>")
                key_norm = hashable_key(key)
                if isinstance(obj, dict):
                    try:
                        obj[key_norm] = val
                    except Exception:
                        obj[value_to_display(key_norm)] = val
                # Attach DATA type/key context if we can.
                if isinstance(val, ObjCall):
                    for r in reversed(records):
                        if r.call_offset == val.call_offset:
                            r.assigned_key = key_norm
                            if isinstance(obj, Subscr):
                                base = obj.obj
                                subkey = obj.key
                                if isinstance(base, Sym) and base.name == "DATA":
                                    r.assigned_type = str(hashable_key(subkey))
                            break

            elif opname == "DUP_TOP":
                stack.append(stack[-1] if stack else Sym("<empty>"))

            elif opname == "DUP_TOPX":
                n = int(arg or 0)
                if 1 <= n <= len(stack):
                    stack.extend(stack[-n:])

            elif opname == "ROT_TWO":
                if len(stack) >= 2:
                    stack[-1], stack[-2] = stack[-2], stack[-1]

            elif opname == "ROT_THREE":
                if len(stack) >= 3:
                    stack[-3], stack[-2], stack[-1] = stack[-1], stack[-3], stack[-2]

            elif opname == "ROT_FOUR":
                if len(stack) >= 4:
                    stack[-4], stack[-3], stack[-2], stack[-1] = stack[-1], stack[-4], stack[-3], stack[-2]

            elif opname == "POP_TOP":
                if stack:
                    stack.pop()

            elif opname in ("IMPORT_NAME",):
                # Pops import level/fromlist in Py2, pushes module.
                if stack:
                    stack.pop()
                if stack:
                    stack.pop()
                name = co.names[arg] if arg is not None and arg < len(co.names) else f"<import {arg}>"
                stack.append(Sym(name))

            elif opname == "IMPORT_FROM":
                name = co.names[arg] if arg is not None and arg < len(co.names) else f"<import_from {arg}>"
                stack.append(Sym(name))

            elif opname == "MAKE_FUNCTION":
                defaults = popn(stack, int(arg or 0))
                code_val = stack.pop() if stack else None
                name = "<function>"
                if isinstance(code_val, MVal) and isinstance(code_val.value, CodeObj):
                    name = code_val.value.name
                    stack.append(Func(name, code_val.value))
                else:
                    stack.append(Func(name, None))

            elif opname == "MAKE_CLOSURE":
                defaults = popn(stack, int(arg or 0))
                if stack:
                    stack.pop()  # closure tuple
                code_val = stack.pop() if stack else None
                name = "<closure>"
                if isinstance(code_val, MVal) and isinstance(code_val.value, CodeObj):
                    name = code_val.value.name
                    stack.append(Func(name, code_val.value))
                else:
                    stack.append(Func(name, None))

            elif opname == "BUILD_CLASS":
                # Py2: name, bases, dict/function-ish -> class
                methods = stack.pop() if stack else None
                bases = stack.pop() if stack else None
                name = stack.pop() if stack else Sym("<class>")
                class_name = str(value_plain_for_compare(name))
                stack.append(Sym(class_name))

            elif opname.startswith("CALL_FUNCTION"):
                pos_count = int(arg or 0) & 0xFF
                kw_count = ((int(arg or 0) >> 8) & 0xFF)
                # keyword args are name/value pairs on stack; not expected in generated GDT records.
                kw_items = popn(stack, kw_count * 2)
                args = popn(stack, pos_count)

                if opname in ("CALL_FUNCTION_VAR", "CALL_FUNCTION_VAR_KW"):
                    if stack:
                        args.append(stack.pop())
                if opname in ("CALL_FUNCTION_KW", "CALL_FUNCTION_VAR_KW"):
                    if stack:
                        args.append(stack.pop())

                func = stack.pop() if stack else Sym("<call>")
                fn = func_name(func) or value_to_display(func)
                call = ObjCall(fn, args, off)
                stack.append(call)

                if fn in schemas and fn not in EXCLUDE_RECORD_CLASSES:
                    schema = schemas[fn]
                    if schema.fields and schema.fields[0] == "UID" and len(args) >= len(schema.fields):
                        uid = value_plain_for_compare(args[0])
                        field_map = {field: args[idx] for idx, field in enumerate(schema.fields) if idx < len(args)}
                        records.append(Record(fn, uid, field_map, off))

                elif fn == "ApplyPatch" and args:
                    apply_patch_dict_to_records(records, args[0], debug=debug)

                elif fn == "PatchSetAttr" and args:
                    patchsetattr_to_record(records, args, debug=debug)

            elif opname in ("RETURN_VALUE", "STOP_CODE"):
                # Do not break; some generated pyc can have bytes after unreachable jumps,
                # but usually this is the end.
                pass

            elif opname in ("UNPACK_SEQUENCE",):
                seq = stack.pop() if stack else []
                n = int(arg or 0)
                if isinstance(seq, (list, tuple)) and len(seq) >= n:
                    stack.extend(list(seq[:n]))
                else:
                    stack.extend(Sym(f"<unpack {j}>") for j in range(n))

            elif opname in ("BUILD_SLICE",):
                items = popn(stack, int(arg or 2))
                stack.append(tuple(items))

            elif opname in ("JUMP_FORWARD", "JUMP_ABSOLUTE", "POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE",
                            "JUMP_IF_FALSE_OR_POP", "JUMP_IF_TRUE_OR_POP",
                            "SETUP_LOOP", "SETUP_EXCEPT", "SETUP_FINALLY", "FOR_ITER",
                            "POP_BLOCK", "END_FINALLY", "CONTINUE_LOOP", "NOP"):
                # Linear scan is okay for generated database modules.
                if opname in ("POP_JUMP_IF_FALSE", "POP_JUMP_IF_TRUE") and stack:
                    stack.pop()

            elif opname.startswith("BINARY_"):
                b = stack.pop() if stack else None
                a = stack.pop() if stack else None
                stack.append(Sym(f"({value_to_display(a)} {opname} {value_to_display(b)})"))

            else:
                # Tolerate unknowns. Most unknowns are not used in the generated DB construction path.
                if debug:
                    print(f"[debug] unknown {opname} arg={arg} off={off} stack={len(stack)}", file=sys.stderr)

        except Exception as ex:
            if debug:
                print(f"[debug] VM error at off={off} {opname} arg={arg}: {ex}", file=sys.stderr)
            continue

    return records


# ----------------------------
# Loading and querying
# ----------------------------

@dataclass
class LoadedPYC:
    file_name: str
    entry: Optional[CDFEntryLite]
    data: bytes
    root: MVal
    schemas: dict[str, Schema]
    records: list[Record]


def load_pyc_from_args(args, file_name: str) -> LoadedPYC:
    if getattr(args, "pyc", None):
        data = Path(args.pyc).read_bytes()
        entry = None
    else:
        entries = load_entries(args.cdfiles, args.patcher)
        entry = find_entry(entries, file_name)
        data = extract_entry(args.archive, entry)
    root = parse_pyc(data)
    schemas = build_schemas(root)
    records = map_records(root, schemas, debug=getattr(args, "debug", False))
    return LoadedPYC(file_name, entry, data, root, schemas, records)


def get_records(loaded: LoadedPYC, class_name: Optional[str] = None) -> list[Record]:
    if not class_name:
        return loaded.records
    if not class_name.endswith("_c"):
        class_name = class_name + "_c"
    return [r for r in loaded.records if r.class_name.upper() == class_name.upper()]


def record_contains(r: Record, needle: str) -> bool:
    n = needle.upper()
    if n in str(r.uid).upper():
        return True
    for k, v in r.fields.items():
        if n in k.upper() or n in value_to_display(v).upper():
            return True
    return False


def compare_field_value(v: Any, expected: str) -> bool:
    p = value_plain_for_compare(v)
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        try:
            return float(p) == float(expected)
        except Exception:
            pass
    return str(p).upper() == str(expected).upper()


def filter_records(records: list[Record], args) -> list[Record]:
    out = records
    if getattr(args, "uid", None) is not None:
        out = [r for r in out if compare_field_value(MVal("x", r.uid, 0, 0, 0), str(args.uid))]
    if getattr(args, "contains", None):
        out = [r for r in out if record_contains(r, args.contains)]
    if getattr(args, "where_field", None) and getattr(args, "where_value", None) is not None:
        wf = args.where_field
        out = [r for r in out if wf in r.fields and compare_field_value(r.fields[wf], str(args.where_value))]
    return out


def record_to_row(r: Record, schema: Optional[Schema] = None, include_all: bool = True) -> dict[str, Any]:
    row = {
        "class": r.class_name,
        "uid": r.uid,
        "assigned_type": r.assigned_type,
        "assigned_key": r.assigned_key,
        "call_offset": f"0x{r.call_offset:X}",
    }
    fields = schema.fields if schema else list(r.fields.keys())
    for f in fields:
        if f in r.fields:
            row[f] = value_to_display(r.fields[f])
    return row


def print_record(r: Record, schema: Optional[Schema] = None, fields: Optional[list[str]] = None):
    schema_fields = schema.fields if schema else list(r.fields.keys())
    if fields:
        schema_fields = ["UID"] + [f for f in fields if f != "UID"]
    print(f"{r.class_name} UID={r.uid} assigned={r.assigned_type}[{r.assigned_key}] call=0x{r.call_offset:X}")
    for f in schema_fields:
        if f in r.fields:
            print(f"  {f}: {value_to_display(r.fields[f])}")


def parse_value_for_existing(existing: Any, new_text: str) -> Any:
    if isinstance(existing, MVal):
        tag = existing.tag
        old = existing.value
    else:
        tag = ""
        old = existing

    if tag in ("i", "I", "l") or isinstance(old, int) and not isinstance(old, bool):
        return int(new_text, 0)
    if tag in ("f", "g") or isinstance(old, float):
        return float(new_text)
    if isinstance(old, str):
        return str(new_text)
    # Let user force numeric when possible.
    try:
        if "." in new_text or "e" in new_text.lower():
            return float(new_text)
        return int(new_text, 0)
    except Exception:
        return new_text


def patch_mval(data: bytes, m: MVal, new_value: Any) -> bytes:
    b = bytearray(data)

    if m.tag == "i":
        b[m.payload_offset:m.payload_offset + 4] = struct.pack("<i", int(new_value))
    elif m.tag == "I":
        value = int(new_value)
        lo = value & 0xFFFFFFFF
        hi = (value >> 32) & 0xFFFFFFFF
        if hi >= 0x80000000:
            hi -= 0x100000000
        b[m.payload_offset:m.payload_offset + 8] = struct.pack("<Ii", lo, hi)
    elif m.tag == "g":
        b[m.payload_offset:m.payload_offset + 8] = struct.pack("<d", float(new_value))
    elif m.tag == "f":
        raw = ("%s" % float(new_value)).encode("ascii")
        if len(raw) != m.payload_size:
            raise ValueError(f"Text-float marshal size would change ({m.payload_size} -> {len(raw)}). Try a same-length value.")
        b[m.payload_offset:m.payload_offset + m.payload_size] = raw
    elif m.tag in ("s", "t", "u"):
        if isinstance(new_value, bytes):
            raw = new_value
        else:
            raw = str(new_value).encode("utf-8" if m.tag == "u" else "latin1")
        if len(raw) != m.payload_size:
            raise ValueError(f"String marshal size would change ({m.payload_size} -> {len(raw)}). Use a same-length string.")
        b[m.payload_offset:m.payload_offset + m.payload_size] = raw
    else:
        raise ValueError(f"Cannot patch marshal tag {m.tag!r}. Supported: int, float, same-length string.")
    return bytes(b)


def find_one_record(loaded: LoadedPYC, class_name: str, args) -> Record:
    records = filter_records(get_records(loaded, class_name), args)
    if not records:
        raise ValueError("No matching record found.")
    if len(records) > 1:
        print(f"[!] {len(records)} records matched; using first. Narrow with --uid or --where-field/--where-value.", file=sys.stderr)
        for r in records[:10]:
            print(f"    {r.class_name} UID={r.uid}", file=sys.stderr)
    return records[0]


# ----------------------------
# AI trace helpers
# ----------------------------

def get_field(record: Record, field: str) -> Any:
    if field not in record.fields:
        raise KeyError(f"{record.class_name} UID={record.uid} has no field {field}")
    return record.fields[field]


def first_record_by_uid(records: list[Record], class_name: str, uid: int | str) -> Optional[Record]:
    class_u = class_name.upper()
    for r in records:
        if r.class_name.upper() == class_u and str(r.uid).upper() == str(uid).upper():
            return r
    return None


def record_uid_int(r: Record) -> Optional[int]:
    try:
        if isinstance(r.uid, bool):
            return None
        return int(r.uid)
    except Exception:
        return None


def unique_ints(vals: list[Any]) -> list[int]:
    out: list[int] = []
    for v in vals:
        if v is None:
            continue
        try:
            if isinstance(v, bool):
                continue
            iv = int(v)
        except Exception:
            continue
        if iv not in out:
            out.append(iv)
    return out


def resolve_ai_track_profile(ai: LoadedPYC, local_world: Record, ai_world: Record, world_id: int, verbose: bool = True):
    """
    v2 fallback resolver.

    Some generated GDT files construct TrackPointer/AITrackProfile as None and
    patch the pointer later with attribute assignment. v1 did not emulate those
    post-constructor pointer assignments, so this resolver tries the stable UID
    relationship used by these DB files:
      local/AI WORLDSCRIPT UID, WorldID-1, WorldID, and nearby IDs.
    """

    local_uid = record_uid_int(local_world)
    ai_world_uid = record_uid_int(ai_world)
    ai_track_ptr = pointer_to_int(get_field(ai_world, "TrackPointer")) if "TrackPointer" in ai_world.fields else None
    local_track_ptr = pointer_to_int(get_field(local_world, "TrackPointer")) if "TrackPointer" in local_world.fields else None

    track_try_uids = unique_ints([
        ai_track_ptr,
        local_track_ptr,
        ai_world_uid,
        local_uid,
        world_id - 1,
        world_id,
        ai_world_uid + 1 if ai_world_uid is not None else None,
        local_uid + 1 if local_uid is not None else None,
    ])

    candidate_tracks: list[tuple[int, Record, Optional[int], Optional[Record], str]] = []

    for track_uid in track_try_uids:
        track = first_record_by_uid(ai.records, "TRACK_c", track_uid)
        if track is None:
            continue

        profile_ptr = pointer_to_int(get_field(track, "AITrackProfile")) if "AITrackProfile" in track.fields else None

        profile_try_uids = unique_ints([
            profile_ptr,
            track_uid,
            track_uid + 1,
            world_id - 1,
            world_id,
        ])

        for profile_uid in profile_try_uids:
            profile = first_record_by_uid(ai.records, "AIRACINGTRACKCONFIG_c", profile_uid)
            if profile is not None:
                reason = "direct pointer" if profile_ptr == profile_uid else "fallback nearby UID"
                return track, profile, reason

        candidate_tracks.append((track_uid, track, profile_ptr, None, "no profile resolved"))

    # Extra fallback: sometimes TRACK.AITrackProfile is post-assigned too.
    # If exactly one AIRACINGTRACKCONFIG exists with UID matching world/local nearby IDs, use it.
    for profile_uid in unique_ints([ai_world_uid, local_uid, world_id - 1, world_id, (ai_world_uid + 1 if ai_world_uid is not None else None)]):
        profile = first_record_by_uid(ai.records, "AIRACINGTRACKCONFIG_c", profile_uid)
        if profile is not None:
            track = candidate_tracks[0][1] if candidate_tracks else Record("TRACK_c", profile_uid, {}, 0)
            return track, profile, "profile UID fallback"

    msg = ["Could not resolve DB_AICONFIG TRACK_c -> AIRACINGTRACKCONFIG_c."]
    msg.append(f"WorldID={world_id}, local UID={local_uid}, ai world UID={ai_world_uid}")
    msg.append(f"Tried TRACK_c UIDs: {track_try_uids}")
    if candidate_tracks:
        msg.append("TRACK_c candidates found:")
        for track_uid, track, profile_ptr, _profile, reason in candidate_tracks[:20]:
            msg.append(f"  TRACK UID={track_uid} AITrackProfile={value_to_display(track.fields.get('AITrackProfile', '<missing>'))} parsed={profile_ptr}")
    msg.append("Run this to inspect nearby AI records:")
    msg.append("  records --file DB_AICONFIG_SCRIPT.PYC --class TRACK_c --uid <uid> --fields UID AITrackProfile")
    msg.append("  records --file DB_AICONFIG_SCRIPT.PYC --class AIRACINGTRACKCONFIG_c --uid <uid>")
    raise ValueError("\n".join(msg))


def trace_ai_track(args):
    local = load_pyc_from_args(args, "DB_GAME_LOCAL_SCRIPT.PYC")
    ai = load_pyc_from_args(args, "DB_AICONFIG_SCRIPT.PYC")

    # Find world(s) in DB_GAME_LOCAL by track string or exact world id.
    local_worlds = get_records(local, "WORLDSCRIPT_c")
    if args.world_id is not None:
        candidates = [r for r in local_worlds if compare_field_value(get_field(r, "WorldID"), str(args.world_id))]
    else:
        candidates = [r for r in local_worlds if record_contains(r, args.track)]

    if not candidates:
        raise ValueError(f"No DB_GAME_LOCAL WORLDSCRIPT matched track={args.track!r} world_id={args.world_id!r}")

    if len(candidates) > 1:
        print(f"[i] {len(candidates)} local world matches. Using index {args.select}.")
        for idx, r in enumerate(candidates[:20]):
            print(f"  [{idx}] UID={r.uid} WorldID={value_to_display(get_field(r, 'WorldID'))} "
                  f"WorldName={value_to_display(get_field(r, 'WorldName'))} TrackPointer={value_to_display(get_field(r, 'TrackPointer'))}")
    local_world = candidates[args.select]
    world_id = pointer_to_int(get_field(local_world, "WorldID"))
    if world_id is None:
        raise ValueError("Could not parse WorldID from local WORLDSCRIPT.")

    ai_worlds = [r for r in get_records(ai, "WORLDSCRIPT_c") if compare_field_value(get_field(r, "WorldID"), str(world_id))]
    if not ai_worlds:
        # Fallback: same UID as the local world.
        local_uid = record_uid_int(local_world)
        ai_world = first_record_by_uid(ai.records, "WORLDSCRIPT_c", local_uid) if local_uid is not None else None
        if ai_world is None:
            raise ValueError(f"No DB_AICONFIG WORLDSCRIPT matched WorldID={world_id} or local UID={local_uid}")
    else:
        ai_world = ai_worlds[0]

    ai_track, profile, reason = resolve_ai_track_profile(ai, local_world, ai_world, world_id, verbose=True)
    if reason != "direct pointer":
        print(f"[i] pointer fallback used: {reason}")

    return local, ai, local_world, ai_world, ai_track, profile

def interesting_ai_fields(profile: Record) -> list[str]:
    return [
        "UID",
        "BumpDraftingEnabled",
        "BumpDraftingConsiderGearing",
        "BumpDraftingMaxPackSize",
        "BumpDraftingRoadStraightness",
        "BumpdraftPlayerRoadStraightness",
        "CatchupWantSpeedModifierEasy",
        "CatchupWantSpeedModifierHard",
        "StateMachineWeightingOvertake",
        "StateMachineWeightingBumpDraft",
        "StayAlongsideGap",
        "StayBehindRegion",
        "ThrottleLiftToleranceDeg",
        "CatchupPowModifier",
    ]


# ----------------------------
# Commands
# ----------------------------

def cmd_schema(args):
    loaded = load_pyc_from_args(args, args.file)
    if args.class_name:
        cname = args.class_name if args.class_name.endswith("_c") else args.class_name + "_c"
        s = loaded.schemas.get(cname)
        if not s:
            raise ValueError(f"No schema named {cname}.")
        print(f"{s.class_name} line={s.firstlineno} file={s.filename}")
        for idx, f in enumerate(s.fields):
            print(f"{idx:02d}: {f}")
    else:
        for name in sorted(loaded.schemas):
            s = loaded.schemas[name]
            print(f"{name:32s} fields={len(s.fields):2d} line={s.firstlineno}")


def cmd_records(args):
    loaded = load_pyc_from_args(args, args.file)
    records = filter_records(get_records(loaded, args.class_name), args)
    schema = None
    if args.class_name:
        cname = args.class_name if args.class_name.endswith("_c") else args.class_name + "_c"
        schema = loaded.schemas.get(cname)

    if args.csv:
        rows = [record_to_row(r, loaded.schemas.get(r.class_name)) for r in records]
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            if rows:
                # union fieldnames preserving common first columns
                keys = ["class", "uid", "assigned_type", "assigned_key", "call_offset"]
                for row in rows:
                    for k in row.keys():
                        if k not in keys:
                            keys.append(k)
                w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            else:
                f.write("class,uid,assigned_type,assigned_key,call_offset\n")
        print(f"[+] wrote {len(rows)} records: {args.csv}")
        return

    print(f"[i] records matched: {len(records)}")
    show = records[:args.limit]
    for idx, r in enumerate(show):
        if idx:
            print("")
        print_record(r, loaded.schemas.get(r.class_name), fields=args.fields)
    if len(records) > len(show):
        print(f"\n... {len(records) - len(show)} more hidden; use --limit or --csv")


def cmd_trace_ai_track(args):
    local, ai, local_world, ai_world, ai_track, profile = trace_ai_track(args)

    print("")
    print("LOCAL WORLD")
    print_record(local_world, local.schemas.get("WORLDSCRIPT_c"),
                 fields=["UID", "WorldID", "WorldName", "Length", "TrackPointer", "TrackType", "TrackHandling", "TrackShape"])

    print("")
    print("AI WORLD")
    print_record(ai_world, ai.schemas.get("WORLDSCRIPT_c"),
                 fields=["UID", "WorldID", "TrackPointer"])

    print("")
    print("AI TRACK")
    print_record(ai_track, ai.schemas.get("TRACK_c"),
                 fields=["UID", "AITrackProfile"])

    print("")
    print("AI TRACK CONFIG")
    print_record(profile, ai.schemas.get("AIRACINGTRACKCONFIG_c"),
                 fields=interesting_ai_fields(profile))

    print("")
    print("[i] To patch this AI track config directly:")
    print(f"    --class AIRACINGTRACKCONFIG_c --uid {profile.uid}")


def cmd_set_record_value(args):
    loaded = load_pyc_from_args(args, args.file)
    record = find_one_record(loaded, args.class_name, args)
    if args.field not in record.fields:
        raise ValueError(f"Field {args.field!r} not found on {record.class_name}.")
    old = record.fields[args.field]
    if not isinstance(old, MVal):
        raise ValueError(f"Field {args.field!r} is not a direct marshal constant: {value_to_display(old)}")

    new_value = parse_value_for_existing(old, args.value)
    new_data = patch_mval(loaded.data, old, new_value)

    print(f"[i] {loaded.file_name} {record.class_name} UID={record.uid} {args.field}: {value_to_display(old)} -> {new_value}")
    if args.dry_run:
        print("[dry-run] No file written.")
        return

    if args.pyc_out:
        args.pyc_out.write_bytes(new_data)
        print(f"[+] wrote patched pyc: {args.pyc_out}")

    if args.out_archive:
        if loaded.entry is None:
            raise ValueError("--out-archive requires archive/cdfiles source, not --pyc.")
        install_entry_same_size(args.archive, args.out_archive, loaded.entry, new_data)
        print(f"[+] wrote patched archive: {args.out_archive}")


def cmd_set_ai_track_value(args):
    local, ai, local_world, ai_world, ai_track, profile = trace_ai_track(args)

    if args.field not in profile.fields:
        raise ValueError(f"Field {args.field!r} not found on AIRACINGTRACKCONFIG_c.")
    old = profile.fields[args.field]
    if not isinstance(old, MVal):
        raise ValueError(f"Field {args.field!r} is not a direct marshal constant: {value_to_display(old)}")

    new_value = parse_value_for_existing(old, args.value)
    new_ai_data = patch_mval(ai.data, old, new_value)

    print(f"[i] Track match: WorldID={value_to_display(get_field(local_world, 'WorldID'))} "
          f"WorldName={value_to_display(get_field(local_world, 'WorldName'))}")
    print(f"[i] AIRACINGTRACKCONFIG UID={profile.uid} {args.field}: {value_to_display(old)} -> {new_value}")

    if args.dry_run:
        print("[dry-run] No archive written.")
        return

    if not args.out_archive:
        raise ValueError("set-ai-track-value needs --out-archive.")
    if ai.entry is None:
        raise ValueError("--out-archive requires archive/cdfiles source, not --pyc.")
    install_entry_same_size(args.archive, args.out_archive, ai.entry, new_ai_data)
    print(f"[+] wrote patched archive: {args.out_archive}")


# ----------------------------
# argparse
# ----------------------------

def add_common(p):
    p.add_argument("--archive", type=Path, default=Path(r"D:\SteamLibrary\steamapps\common\NASCAR 15\data\ARCHIVE0.AR"))
    p.add_argument("--cdfiles", type=Path, required=False, default=Path(r"D:\SteamLibrary\steamapps\common\NASCAR 15\data\cdfiles.dat"))
    p.add_argument("--patcher", type=Path, default=Path(".") / "nascar15_v11_probe_patcher.py")
    p.add_argument("--pyc", type=Path, default=None, help="Optional standalone .PYC input instead of archive/cdfiles.")
    p.add_argument("--debug", action="store_true")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NASCAR 15 PYC generated-record mapper and same-size patcher")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("version", help="Print script version")
    p.set_defaults(func=lambda args: print("nascar15_pyc_record_mapper_v5_fixed"))

    p = sub.add_parser("schema", help="List generated GDT class schemas / fields")
    add_common(p)
    p.add_argument("--file", required=True, help="PYC filename, e.g. DB_GAME_LOCAL_SCRIPT.PYC")
    p.add_argument("--class", dest="class_name", default=None, help="Class name, e.g. RACEDATA_c")
    p.set_defaults(func=cmd_schema)

    p = sub.add_parser("records", help="List records from a generated GDT PYC")
    add_common(p)
    p.add_argument("--file", required=True)
    p.add_argument("--class", dest="class_name", default=None)
    p.add_argument("--uid", default=None)
    p.add_argument("--contains", default=None)
    p.add_argument("--where-field", default=None)
    p.add_argument("--where-value", default=None)
    p.add_argument("--fields", nargs="*", default=None, help="Only display selected fields")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--csv", type=Path, default=None)
    p.set_defaults(func=cmd_records)

    p = sub.add_parser("trace-ai-track", help="Trace local track/world to DB_AICONFIG AIRACINGTRACKCONFIG")
    add_common(p)
    p.add_argument("--track", default="DAYTONA")
    p.add_argument("--world-id", type=int, default=None)
    p.add_argument("--select", type=int, default=0, help="Which local world match to use when multiple match")
    p.set_defaults(func=cmd_trace_ai_track)

    p = sub.add_parser("set-record-value", help="Patch a direct int/float/same-length string field on one record")
    add_common(p)
    p.add_argument("--file", required=True)
    p.add_argument("--class", dest="class_name", required=True)
    p.add_argument("--uid", default=None)
    p.add_argument("--where-field", default=None)
    p.add_argument("--where-value", default=None)
    p.add_argument("--field", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--pyc-out", type=Path, default=None)
    p.add_argument("--out-archive", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_set_record_value)

    p = sub.add_parser("set-ai-track-value", help="Patch a direct AIRACINGTRACKCONFIG field by track/world trace")
    add_common(p)
    p.add_argument("--track", default="DAYTONA")
    p.add_argument("--world-id", type=int, default=None)
    p.add_argument("--select", type=int, default=0)
    p.add_argument("--field", required=True)
    p.add_argument("--value", required=True)
    p.add_argument("--out-archive", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_set_ai_track_value)

    args = ap.parse_args(argv)

    # Some commands, like `version`, do not use the common archive/pyc args.
    # Only validate paths for commands that actually have those arguments.
    if hasattr(args, "pyc") and args.pyc:
        if not args.pyc.exists():
            raise FileNotFoundError(args.pyc)
    elif hasattr(args, "archive"):
        if not args.archive.exists():
            raise FileNotFoundError(args.archive)
        if not args.cdfiles.exists():
            raise FileNotFoundError(args.cdfiles)
        if not args.patcher.exists():
            raise FileNotFoundError(args.patcher)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
