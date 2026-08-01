#!/usr/bin/env python3
"""Compatibility copy of the proven v0.10 ARC/CDF helper API.

RC11 accidentally omitted this dependency even though team and native thumbnail
helpers import it.  This module is intentionally small: it provides only the
read/identity/transplant operations those protected helpers already use.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import json, os, re, struct, zlib
import containers as C

VERSION='0.10-compat-rc11.1'
HEADER_SIZE=0x80

@dataclass
class CdfRow:
    name:str
    offset:int
    size:int
    record_pos:int
    layout:str
    @property
    def size_pos(self): return self.record_pos+(8 if self.layout=='A' else 16)
    @property
    def offset_pos(self): return self.record_pos+(20 if self.layout=='A' else 28)

@dataclass
class MultiEntry:
    index:int
    name:str
    data_off:int
    name_ref:int
    width:int
    height:int
    fmt:str
    payload_abs:int
    payload_size:int
    chunk_start:int
    chunk_end:int
    table_start:int
    table_record:bytes
    header_abs:int=0
    mip_count:int=1
    needed:int=0

@dataclass
class MultiArc:
    raw:bytes
    count:int
    entries:list[MultiEntry]
    base:int
    name_blob:int

def _config_candidates():
    root=Path(__file__).resolve().parent
    out=[root/'config.json']
    local=os.environ.get('LOCALAPPDATA')
    if local: out.append(Path(local)/'NASCAR15ModdingApp'/'config.json')
    return out

def detect_game(explicit=None):
    candidates=[]
    if explicit: candidates.append(Path(explicit))
    for cfg in _config_candidates():
        try:
            if cfg.exists():
                value=(json.loads(cfg.read_text(encoding='utf-8')) or {}).get('game')
                if value: candidates.append(Path(value))
        except Exception: pass
    candidates += [Path(r'C:\Program Files (x86)\Steam\steamapps\common\NASCAR 15'),Path(r'D:\SteamLibrary\steamapps\common\NASCAR 15'),Path(r'E:\SteamLibrary\steamapps\common\NASCAR 15')]
    seen=set()
    for p in candidates:
        key=str(p).casefold()
        if key in seen: continue
        seen.add(key)
        if (p/'data'/'ARCHIVE1.AR').exists(): return p
    raise FileNotFoundError('NASCAR 15 was not found')

def paths_for_game(game):
    game=Path(game); data=game/'data'; archive=data/'ARCHIVE1.AR'; cdf=data/'cdfiles1.dat'
    if not archive.exists() or not cdf.exists(): raise FileNotFoundError('ARCHIVE1.AR/cdfiles1.dat was not found')
    return archive,cdf

def parse_cdf_rows(path):
    path=Path(path); raw=bytearray(path.read_bytes())
    if len(raw)<48 or struct.unpack_from('<I',raw,0)[0]!=0x436C6966: raise ValueError(f'{path.name} is not a filC index')
    hdr=struct.unpack_from('<12I',raw,0); count,strtab=int(hdr[8]),int(hdr[10]); string_base=len(raw)-strtab
    def name_at(off):
        if off<0 or off>=strtab:return ''
        p=string_base+off;e=raw.find(b'\0',p)
        return bytes(raw[p:e]).decode('ascii','replace') if e>=p else ''
    for start,layout in ((0x40,'A'),(0x50,'B')):
        rows=[];valid=0;pos=start
        for _ in range(count):
            if pos+32>string_base:break
            f=struct.unpack_from('<8I',raw,pos)
            name_off,size,offset=(f[1],f[2],f[5]) if layout=='A' else (f[3],f[4],f[7])
            name=name_at(int(name_off))
            if name and all(32<=ord(ch)<127 for ch in name):
                valid+=1;rows.append(CdfRow(name,int(offset),int(size),pos,layout))
            pos+=32
        if count and valid>count*0.8:return raw,rows
    raise ValueError(f'unrecognized cdfiles layout in {path}')

def find_row(rows,target):
    hits=[r for r in rows if r.name.casefold()==str(target).casefold()]
    if len(hits)!=1: raise ValueError(f'expected one {target} row; found {len(hits)}')
    return hits[0]

def read_entry(archive,row):
    with Path(archive).open('rb') as f:
        f.seek(int(row.offset));raw=f.read(int(row.size))
    if len(raw)!=int(row.size):raise ValueError(f'short read for {row.name}')
    return raw

def parse_multi_arc(raw):
    """Parse an ARCC bank using the v1.0 field contract.

    FIX (v1.0.2-dev8): every consumer of this function -- team_assets, the three
    thumbnail v25 modules, the extra-scheme managers -- was written against the
    v1.0 parser, whose contract is:

        base        = 0x80 + logical_count*32
        chunk_start = base + data_off          (0x20 before the texture header)
        payload_abs = chunk_start + 96

    Because physical == 2*logical + 2 on every standard bank, that base is
    exactly ``table_end - 0x20``, which lands on the 0x42 resource-order record
    and the 0xFD name-area record.  Those two records ARE the bank directory the
    v25 modules validate.

    dev5 re-pointed this shim at containers.parse_multi_arc, whose base is the
    *physical* table end.  Everything structural shifted forward by 0x20, so the
    directory check read the first texture header instead and reported
    data_section_bytes=0x01000100 (a 256x256 dims pair) and order_table_bytes=0x35
    (the '5' of 'DXT5').  Measured over the full archive map: 0/1950 containers
    passed under dev5's base, 1785/1950 pass under this one, including 21/21
    2DRIVERSELECTTD_* banks.

    containers.parse_multi_arc keeps its own (correct) 24-byte pixel offsets for
    the image display/import paths that call it directly.  This shim deliberately
    reproduces v1.0 byte-for-byte, because v1.0 is the build whose driver
    transfers are known to work.
    """
    raw=bytes(raw); rows,table_end=C.parse_multi_arc(raw)
    if not rows: raise ValueError('ARCC contained no supported textures')
    name_blob=next((int(e.get('name_blob')) for e in rows if e.get('name_blob') is not None),None)
    if name_blob is None: raise ValueError('could not resolve ARCC name area')
    base=int(table_end)-0x20
    if base<HEADER_SIZE: base=int(table_end)
    # FIX (v1.0.2-dev12): the LAST texture's chunk_end used to default to
    # name_blob, which sits AFTER the 0x42 resource-order record.  transplant_entry
    # sizes its write as min(chunk_end - payload_abs, expected), so on the final
    # resource that bound let a copy run straight through the order table and
    # zero part of it -- exactly the
    #     resource-order table is not sequential: [0, 1, 2, 3, 4, 5, 0, 0, 8]
    # damage found in 2DRIVERSELECTTD_1325.ARC.  Bound the last chunk to the
    # start of the 0x42/0xFD directory instead, so no resource write can reach it.
    directory_start=name_blob
    try:
        physical=struct.unpack_from('<I',raw,4)[0]
        te=0x80+int(physical)*16
        dir_offs=[]
        for i in range(int(physical)):
            _k,d_off,_n,packed=struct.unpack_from('<4I',raw,0x80+i*16)
            if int(packed)&0xFF in (0x42,0xFD):
                dir_offs.append(te+int(d_off))
        if dir_offs:
            candidate=min(dir_offs)
            if te<=candidate<=len(raw):directory_start=candidate
    except Exception:
        pass
    starts=sorted(base+int(e['data_off']) for e in rows)
    entries=[]
    for e in rows:
        chunk_start=base+int(e['data_off'])
        chunk_end=min((s for s in starts if s>chunk_start),default=directory_start)
        entries.append(MultiEntry(index=int(e.get('index',len(entries))),name=str(e['name']),
            data_off=int(e['data_off']),name_ref=int(e['name_ref']),
            width=int(e['w']),height=int(e['h']),fmt=str(e['fmt']),
            payload_abs=chunk_start+96,payload_size=int(e.get('payload_size',0)),
            chunk_start=chunk_start,chunk_end=chunk_end,
            table_start=int(e['table_start']),table_record=bytes(e['table_record']),
            header_abs=int(e.get('header_abs',0)),mip_count=int(e.get('mip_count',1)),
            needed=int(e.get('needed',0))))
    return MultiArc(raw=raw,count=len(entries),entries=entries,base=base,name_blob=name_blob)

def entry_by_name(parsed,name):
    hits=[e for e in parsed.entries if e.name==name]
    if len(hits)!=1: raise ValueError(f'expected one {name} resource; found {len(hits)}')
    return hits[0]

def expected_texture_bytes(entry):
    if entry.fmt=='DXT1':bpb=8
    elif entry.fmt=='DXT5':bpb=16
    elif entry.fmt=='A8R8G8B8':return int(entry.width)*int(entry.height)*4
    else:raise ValueError('unsupported texture format '+str(entry.fmt))
    return max(1,(int(entry.width)+3)//4)*max(1,(int(entry.height)+3)//4)*bpb

def identity_record(source_record,source,target):
    sf=list(struct.unpack('<8I',bytes(source_record)));tf=list(struct.unpack('<8I',bytes(target.table_record)))
    # Keep the source identity/key, but retain the destination's physical data
    # offset, visible-name reference and packed resource size.
    sf[5]=tf[5];sf[6]=tf[6];sf[7]=tf[7]
    return struct.pack('<8I',*sf)

def _rename_in_place(out,parsed,entry,new_name):
    old=entry.name.encode('latin1');new=str(new_name).encode('latin1')
    if len(old)!=len(new):raise ValueError('resource rename must preserve byte length')
    pos=parsed.name_blob+entry.name_ref
    if bytes(out[pos:pos+len(old)])!=old:raise ValueError('resource name location mismatch')
    out[pos:pos+len(new)]=new
    fields=list(struct.unpack('<8I',bytes(out[entry.table_start:entry.table_start+32])))
    crc=zlib.crc32(new.lower())&0xffffffff
    fields[0]=crc;fields[4]=crc
    out[entry.table_start:entry.table_start+32]=struct.pack('<8I',*fields)
    return {'old_name':entry.name,'new_name':str(new_name),'name_ref':entry.name_ref,'crc32':crc}

def rename_equal_length_entry(out,parsed,old_name,new_name):
    return _rename_in_place(out,parsed,entry_by_name(parsed,old_name),new_name)

def transplant_entry(out,template,source_arc,template_name,source_name):
    target=entry_by_name(template,template_name);source=entry_by_name(source_arc,source_name)
    if len(template_name.encode('latin1'))!=len(source_name.encode('latin1')):raise ValueError('source and destination resource names differ in length')
    if (source.width,source.height,source.fmt)!=(target.width,target.height,target.fmt):raise ValueError('source and destination texture profiles differ')
    expected=expected_texture_bytes(source);block=16 if source.fmt=='DXT5' else 8 if source.fmt=='DXT1' else 4
    src_n=min(source.chunk_end-source.payload_abs,expected);dst_n=min(target.chunk_end-target.payload_abs,expected)
    if src_n<=0 or dst_n<=0 or src_n%block or dst_n%block or expected-src_n>64 or expected-dst_n>64:raise ValueError('unsupported native truncation layout')
    pixels=source_arc.raw[source.payload_abs:source.payload_abs+src_n]
    pixels=(pixels+b'\0'*(expected-src_n))[:dst_n]
    out[target.table_start:target.table_start+32]=identity_record(source.table_record,source,target)
    out[target.payload_abs:target.payload_abs+dst_n]=pixels
    rename=_rename_in_place(out,template,target,source_name)
    return {'source':source_name,'destination_slot':template_name,'payload_bytes':dst_n,'rename':rename}
