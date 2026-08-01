#!/usr/bin/env python3
"""NASCAR 15 schedule RaceEvent-link mapper/patcher v0.1.

The normal season builder reads the post-constructor assignment:

    __RACEDATA[racedata_uid].RaceEvent = __EVENT[event_uid]

Changing only the RACEDATA constructor's display event/laps is not enough.  This
module maps those exact Python-2 bytecode operands and repoints only the selected
RACEDATA records to an existing EVENT constant.  It never changes marshal-table
sizes, PYC sizes, archive sizes, or cdfiles records.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional

VERSION = "0.2"


class MarshalReader:
    """Small Python-2.7 marshal reader sufficient for NASCAR 15 code objects."""

    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.pos = offset
        self.interned: List[bytes] = []

    def take(self, count: int) -> bytes:
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError("truncated marshal object")
        out = self.data[self.pos:self.pos + count]
        self.pos += count
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def obj(self, depth: int = 0) -> Any:
        if depth > 500:
            raise ValueError("marshal nesting is too deep")
        tag = chr(self.byte() & 0x7F)
        if tag == "N": return None
        if tag == "T": return True
        if tag == "F": return False
        if tag in ("S", ".", "0"): return None
        if tag == "i": return self.i32()
        if tag == "I": return struct.unpack("<q", self.take(8))[0]
        if tag == "g": return struct.unpack("<d", self.take(8))[0]
        if tag == "f": return float(self.take(self.byte()).decode("ascii"))
        if tag == "l":
            n = self.i32()
            digits = [struct.unpack("<H", self.take(2))[0] for _ in range(abs(n))]
            value = sum(digit << (15 * i) for i, digit in enumerate(digits))
            return -value if n < 0 else value
        if tag in ("s", "t"):
            n = self.i32()
            if n < 0: raise ValueError("negative marshal string length")
            raw = self.take(n)
            if tag == "t": self.interned.append(raw)
            return raw
        if tag == "u":
            n = self.i32()
            if n < 0: raise ValueError("negative marshal unicode length")
            return self.take(n).decode("utf-8", "replace")
        if tag == "R":
            index = self.i32()
            if index < 0 or index >= len(self.interned):
                raise ValueError("invalid marshal interned-string reference")
            return self.interned[index]
        if tag in ("(", "["):
            n = self.i32()
            if n < 0 or n > 10_000_000:
                raise ValueError("invalid marshal sequence length")
            values = [self.obj(depth + 1) for _ in range(n)]
            return tuple(values) if tag == "(" else values
        if tag == "{":
            out: Dict[Any, Any] = {}
            while True:
                if self.pos >= len(self.data): raise ValueError("unterminated marshal dict")
                if chr(self.data[self.pos] & 0x7F) == "0":
                    self.pos += 1
                    break
                key = self.obj(depth + 1)
                out[key] = self.obj(depth + 1)
            return out
        if tag == "c":
            argcount = self.i32(); nlocals = self.i32(); stacksize = self.i32(); flags = self.i32()
            code_tag_pos = self.pos
            code = self.obj(depth + 1)
            if not isinstance(code, (bytes, bytearray)):
                raise ValueError("code object bytecode is not a byte string")
            # NASCAR 15's Python-2 PYC stores bytecode as a normal marshal string:
            # one tag byte + four-byte length, then the bytecode itself.
            code_offset = code_tag_pos + 5
            consts = self.obj(depth + 1); names = self.obj(depth + 1)
            varnames = self.obj(depth + 1); freevars = self.obj(depth + 1); cellvars = self.obj(depth + 1)
            filename = self.obj(depth + 1); name = self.obj(depth + 1)
            firstlineno = self.i32(); lnotab = self.obj(depth + 1)
            return dict(argcount=argcount,nlocals=nlocals,stacksize=stacksize,flags=flags,
                        code=bytes(code),code_offset=code_offset,consts=tuple(consts),names=tuple(names),
                        varnames=tuple(varnames),freevars=tuple(freevars),cellvars=tuple(cellvars),
                        filename=filename,name=name,firstlineno=firstlineno,lnotab=lnotab)
        raise ValueError(f"unsupported Python-2 marshal type {tag!r} at 0x{self.pos-1:X}")


def _text(value: Any) -> str:
    return value.decode("latin1", "replace") if isinstance(value, bytes) else str(value)


def _instructions(code: bytes) -> List[Dict[str, Optional[int]]]:
    out=[]; i=0; extended=0
    while i < len(code):
        start=i; opcode=code[i]; i+=1; arg=None; arg_offset=None
        if opcode >= 90:
            if i + 2 > len(code): raise ValueError(f"truncated bytecode operand at 0x{start:X}")
            raw=code[i] | (code[i+1] << 8); arg_offset=i; i+=2; arg=raw | extended
            if opcode == 145: extended=raw << 16  # Python-2.7 EXTENDED_ARG
            else: extended=0
        out.append(dict(offset=start,opcode=opcode,arg=arg,arg_offset=arg_offset))
    return out


def parse_root(pyc: bytes) -> Dict[str, Any]:
    if len(pyc) < 16: raise ValueError("PYC is too small")
    root=MarshalReader(pyc,8).obj()
    if not isinstance(root,dict) or "code" not in root:
        raise ValueError("PYC root is not a Python code object")
    return root


def collect_links(root: Dict[str, Any]) -> Dict[int, Dict[str, int]]:
    """Return RACEDATA UID -> exact runtime RaceEvent assignment metadata."""
    names=[_text(x) for x in root["names"]]; consts=root["consts"]; ins=_instructions(root["code"])
    pattern=[101,100,25,101,100,25,95]  # LOAD_NAME/CONST/BINARY_SUBSCR twice, STORE_ATTR
    links={}
    for i in range(len(ins)-len(pattern)+1):
        seq=ins[i:i+len(pattern)]
        if [x["opcode"] for x in seq] != pattern: continue
        if any(x["arg"] is None for x in (seq[0],seq[1],seq[3],seq[4],seq[6])): continue
        if seq[0]["arg"]>=len(names) or seq[3]["arg"]>=len(names) or seq[6]["arg"]>=len(names): continue
        if names[seq[0]["arg"]]!="__EVENT" or names[seq[3]["arg"]]!="__RACEDATA" or names[seq[6]["arg"]]!="RaceEvent": continue
        if seq[1]["arg"]>=len(consts) or seq[4]["arg"]>=len(consts): continue
        event_uid=consts[seq[1]["arg"]]; racedata_uid=consts[seq[4]["arg"]]
        if isinstance(event_uid,bool) or isinstance(racedata_uid,bool): continue
        if not isinstance(event_uid,int) or not isinstance(racedata_uid,int): continue
        links[int(racedata_uid)]=dict(racedata_uid=int(racedata_uid),event_uid=int(event_uid),
            event_const_index=int(seq[1]["arg"]),racedata_const_index=int(seq[4]["arg"]),
            event_operand_code_offset=int(seq[1]["arg_offset"]),
            racedata_operand_code_offset=int(seq[4]["arg_offset"]),
            event_operand_pyc_offset=int(root["code_offset"]+seq[1]["arg_offset"]))
    return links


def inspect_links(pyc: bytes) -> Dict[int, Dict[str, int]]:
    return collect_links(parse_root(pyc))


def _reference_event_indices(reference_pyc: bytes) -> Dict[int, List[int]]:
    root=parse_root(reference_pyc); links=collect_links(root); out={}
    for link in links.values():
        out.setdefault(int(link["event_uid"]),[]).append(int(link["event_const_index"]))
    return {uid:sorted(set(indices)) for uid,indices in out.items()}


def patch_links(pyc: bytes, assignments: List[Dict[str, Any]], reference_pyc: Optional[bytes]=None):
    """Patch selected runtime RaceEvent assignments.

    assignments require target_uid and event_uid; source_uid is strongly checked
    against the pristine/reference link when supplied.  The reference PYC is used
    to recover each event's semantic constant index after a prior custom schedule
    has repointed every live slot to the same event.
    """
    if not isinstance(assignments,list): raise ValueError("assignments must be a list")
    reference_pyc = pyc if reference_pyc is None else reference_pyc
    live_root=parse_root(pyc); live_links=collect_links(live_root)
    ref_root=parse_root(reference_pyc); ref_links=collect_links(ref_root)
    ref_indices=_reference_event_indices(reference_pyc)
    patched=bytearray(pyc); allowed=set(); changes=[]; seen=set()

    for item in assignments:
        target_uid=int(item["target_uid"]); event_uid=int(item["event_uid"])
        source_uid=None if item.get("source_uid") is None else int(item["source_uid"])
        if target_uid in seen: raise ValueError(f"duplicate RaceEvent target UID {target_uid}")
        seen.add(target_uid)
        if target_uid not in live_links: raise ValueError(f"RACEDATA UID {target_uid} has no runtime RaceEvent assignment")
        ref_source=ref_links.get(source_uid) if source_uid is not None else None
        source_reference_matches=bool(ref_source and int(ref_source["event_uid"])==event_uid)
        if source_reference_matches:
            reference_index=int(ref_source["event_const_index"])
        else:
            # Older builds may have altered the visible constructor separately
            # from the runtime WorldPointer. The clean reference still proves
            # that the requested event UID is a legitimate schedule event.
            candidates=ref_indices.get(event_uid) or []
            if not candidates: raise ValueError(f"event UID {event_uid} has no semantic RaceEvent constant in the reference PYC")
            reference_index=int(candidates[0])

        # Never assume the live constant table still matches pristine indices.
        # Prior name/rating/lap/schedule rebuilds can append or replace constants.
        # Resolve the requested event semantically in the current live PYC.
        live_candidates=[]
        if 0 <= reference_index < len(live_root["consts"]) and live_root["consts"][reference_index] == event_uid:
            live_candidates.append(reference_index)
        for link in live_links.values():
            if int(link["event_uid"])==event_uid:
                live_candidates.append(int(link["event_const_index"]))
        live_candidates.extend(i for i,value in enumerate(live_root["consts"])
                               if isinstance(value,int) and not isinstance(value,bool) and int(value)==event_uid)
        live_candidates=sorted(set(live_candidates))
        if not live_candidates:
            raise ValueError(f"event UID {event_uid} is valid in the clean reference but missing from the live constant table")
        desired_index=int(live_candidates[0])
        if desired_index > 0xFFFF: raise ValueError(f"event UID {event_uid} requires EXTENDED_ARG; edit refused")

        target=live_links[target_uid]; pos=int(target["event_operand_pyc_offset"])
        if pos < 0 or pos + 2 > len(pyc): raise ValueError(f"RaceEvent operand for UID {target_uid} lies outside the PYC")
        old_index=struct.unpack_from("<H",pyc,pos)[0]
        if old_index != int(target["event_const_index"]): raise ValueError(f"RaceEvent operand readback mismatch for UID {target_uid}")
        new_operand=struct.pack("<H",desired_index)
        patched[pos:pos+2]=new_operand; allowed.update((pos,pos+1))
        changes.append(dict(target_uid=target_uid,source_uid=source_uid,
            old_event_uid=int(target["event_uid"]),new_event_uid=event_uid,
            old_const_index=old_index,new_const_index=desired_index,reference_const_index=reference_index,
            operand_pyc_offset=pos,source_reference_matches=source_reference_matches,
            changed=(old_index!=desired_index)))

    patched=bytes(patched)
    diff=[i for i,(a,b) in enumerate(zip(pyc,patched)) if a!=b]
    if len(patched)!=len(pyc): raise ValueError("RaceEvent patch changed PYC size")
    if not set(diff).issubset(allowed): raise ValueError(f"unexpected RaceEvent patch diff: {diff[:20]}")

    verify_links=inspect_links(patched)
    for change in changes:
        got=verify_links.get(change["target_uid"],{}).get("event_uid")
        if got != change["new_event_uid"]:
            raise ValueError(f"RaceEvent verification failed for UID {change['target_uid']}: got {got}, expected {change['new_event_uid']}")
    return patched,changes,dict(version=VERSION,assignment_count=len(assignments),
        changed_assignments=sum(1 for x in changes if x["changed"]),changed_bytes=len(diff),
        mapped_runtime_links=len(live_links),reference_runtime_links=len(ref_links))
