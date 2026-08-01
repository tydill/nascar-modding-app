#!/usr/bin/env python3
"""NASCAR 15 team/manufacturer editor backend.

Edits the stock post-constructor STORE_ATTR links directly:
* DRIVERCONFIG_c.TEAM -> an existing RACETEAM_c
* RACETEAM_c.MANUFACTURER -> an existing MANUFACTURER_c

The first app build used a late ApplyPatch call, which parsed correctly but hung
the game at startup. v1.1 detects/removes those legacy blocks and repoints the
original link operands instead.

No constructor records are added. New user-facing team slots reuse the three usable stock
CUSTOM team records (Chevrolet/Ford/Toyota), avoiding a new-team record
count or UID experiment.
"""
from __future__ import annotations
import importlib.util
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

VERSION='1.2'
SERIES_2015_UID=25040
SPARE_TEAM_UIDS=(2403,2405,2406)
ALLOWED_CHANGES={
    ('DRIVERCONFIG_c','TEAM'): 'RACETEAM_c',
    ('RACETEAM_c','MANUFACTURER'): 'MANUFACTURER_c',
}

_MAPPER=None

def _mapper():
    global _MAPPER
    if _MAPPER is not None:return _MAPPER
    path=Path(__file__).with_name('nascar15_pyc_record_mapper_v5_teams.py')
    spec=importlib.util.spec_from_file_location('n15_team_mapper',path)
    if spec is None or spec.loader is None:raise RuntimeError('team mapper helper is missing')
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    _MAPPER=mod;return mod

def _plain(v:Any)->Any:
    return _mapper().value_plain_for_compare(v)

def _display(v:Any)->str:
    return _mapper().value_to_display(v)

def ref_uid(v:Any)->int|None:
    M=_mapper();p=M.value_plain_for_compare(v)
    if isinstance(p,M.ObjCall) and p.args:
        q=M.value_plain_for_compare(p.args[0])
        try:return int(q)
        except Exception:return None
    try:
        if isinstance(p,bool):return None
        return int(p)
    except Exception:return None

def ref_arg(v:Any,index:int)->Any:
    M=_mapper();p=M.value_plain_for_compare(v)
    if isinstance(p,M.ObjCall) and len(p.args)>index:
        return M.value_plain_for_compare(p.args[index])
    return None

def _token_label(token:str,prefixes=())->str:
    s=str(token or '')
    for p in prefixes:
        if s.upper().startswith(p.upper()):s=s[len(p):];break
    words=[x for x in re.split(r'_+',s) if x]
    out=[]
    for w in words:
        u=w.upper()
        if u in ('AJ','JJ','JGR','RCR','RFR','RPM','FRM','MWR','SHR','JTG','BK','HSM','WBR','LFR'):out.append(u)
        elif len(w)<=2:out.append(u)
        else:out.append(w.title())
    return ' '.join(out) or s

def _number_from_ref(v:Any)->str:
    script=str(ref_arg(v,2) or '')
    image=str(ref_arg(v,1) or '')
    for text in (script,Path(image).name):
        m=re.match(r'^(\d+[A-Z]?)',text,re.I)
        if m:return m.group(1).upper()
    return ''

def _parse(pyc:bytes):
    M=_mapper();root=M.parse_pyc(pyc);schemas=M.build_schemas(root);records=M.map_records(root,schemas)
    return M,root,schemas,records

def catalog(pyc:bytes)->dict[str,Any]:
    M,root,schemas,records=_parse(pyc)
    by_class={}
    for r in records:by_class.setdefault(r.class_name,[]).append(r)
    team_by_uid={ref_uid(r.uid) if ref_uid(r.uid) is not None else int(_plain(r.uid)):r for r in by_class.get('RACETEAM_c',[])}
    manu_by_uid={ref_uid(r.uid) if ref_uid(r.uid) is not None else int(_plain(r.uid)):r for r in by_class.get('MANUFACTURER_c',[])}
    driver_by_uid={int(_plain(r.uid)):r for r in by_class.get('DRIVER_c',[]) if str(_plain(r.uid)).lstrip('-').isdigit()}
    manufacturers=[]
    for uid,r in sorted(manu_by_uid.items()):
        token=str(_plain(r.fields.get('Name')) or '')
        manufacturers.append(dict(uid=uid,token=token,label=_token_label(token,('S_MANU_',)),manufacturer_id=_plain(r.fields.get('ManufacturerID')),logo=str(_plain(r.fields.get('LogoFilename')) or '')))
    drivers=[]
    active_team_uids=set()
    for r in by_class.get('DRIVERCONFIG_c',[]):
        uid=int(_plain(r.uid))
        if ref_uid(r.fields.get('Series'))!=SERIES_2015_UID:continue
        if _display(r.fields.get('Selectable')) != 'True':continue
        driver_uid=ref_uid(r.fields.get('DRIVER'))
        team_uid=ref_uid(r.fields.get('TEAM'))
        if driver_uid is None or team_uid is None:continue
        dr=driver_by_uid.get(driver_uid)
        token=str(_plain((dr or r).fields.get('Name')) if dr else ref_arg(r.fields.get('DRIVER'),1) or '')
        if token in ('S_DEFAULT_PLAYER_NAME','S_DRIVER_CUSTOM_CHEVROLET','S_DRIVER_CUSTOM_DODGE','S_DRIVER_CUSTOM_FORD','S_DRIVER_CUSTOM_TOYOTA'):continue
        number=_number_from_ref(r.fields.get('NUMBER'))
        active_team_uids.add(team_uid)
        drivers.append(dict(config_uid=uid,driver_uid=driver_uid,token=token,label=_token_label(token,('S_DRIVER_',)),number=number,team_uid=team_uid,base_arc=str(_plain(r.fields.get('BaseArcName')) or '')))
    drivers.sort(key=lambda x:(int(re.match(r'\d+',x['number']).group()) if re.match(r'\d+',x['number']) else 9999,x['number'],x['label']))
    teams=[]
    members={}
    for d in drivers:members.setdefault(d['team_uid'],[]).append(d['config_uid'])
    for uid,r in sorted(team_by_uid.items()):
        token=str(_plain(r.fields.get('NAME')) or '')
        manu=ref_uid(r.fields.get('MANUFACTURER'))
        category='spare' if uid in SPARE_TEAM_UIDS else ('active' if uid in active_team_uids else 'legacy')
        teams.append(dict(uid=uid,token=token,label=_token_label(token,('S_TEAM_',)),manufacturer_uid=manu,twitter=str(_plain(r.fields.get('Twitter')) or ''),category=category,driver_config_uids=members.get(uid,[])))
    order={'active':0,'spare':1,'legacy':2}
    teams.sort(key=lambda x:(order[x['category']],x['label'],x['uid']))
    return dict(version=VERSION,manufacturers=manufacturers,teams=teams,drivers=drivers,series_uid=SERIES_2015_UID,spare_team_uids=list(SPARE_TEAM_UIDS))

# Minimal Python-2 marshal / bytecode helpers.
def _emit(op:int,arg:int|None=None)->bytes:
    if arg is None:return bytes([op])
    if not 0<=int(arg)<=0xffff:raise ValueError('bytecode operand needs EXTENDED_ARG')
    return bytes([op,int(arg)&255,(int(arg)>>8)&255])

def _m_int(v:int)->bytes:
    v=int(v)
    return (b'i'+struct.pack('<i',v)) if -(2**31)<=v<2**31 else (b'I'+struct.pack('<q',v))

def _m_str(s:str)->bytes:
    b=str(s).encode('ascii','strict');return b't'+struct.pack('<i',len(b))+b

class _Skip:
    def __init__(self,data:bytes):self.d=data;self.p=0
    def take(self,n):
        if self.p+n>len(self.d):raise ValueError('truncated marshal object')
        q=self.p;self.p+=n;return q
    def i32(self):return struct.unpack_from('<i',self.d,self.take(4))[0]
    def obj(self,depth=0):
        if depth>400:raise ValueError('marshal nesting too deep')
        t=chr(self.d[self.take(1)]&0x7f)
        if t in 'NTFS.0':return
        if t=='i':self.take(4);return
        if t=='I':self.take(8);return
        if t=='g':self.take(8);return
        if t=='y':self.take(16);return
        if t=='f':self.take(self.d[self.take(1)]);return
        if t=='x':self.take(self.d[self.take(1)]);self.take(self.d[self.take(1)]);return
        if t=='l':self.take(abs(self.i32())*2);return
        if t in 'stu':
            n=self.i32();
            if n<0:raise ValueError('negative string length')
            self.take(n);return
        if t=='R':self.take(4);return
        if t in '([':
            n=self.i32();
            for _ in range(n):self.obj(depth+1)
            return
        if t in '<>':
            n=self.i32();
            for _ in range(n):self.obj(depth+1)
            return
        if t=='{':
            while chr(self.d[self.p]&0x7f)!='0':self.obj(depth+1);self.obj(depth+1)
            self.p+=1;return
        if t=='c':
            self.take(16)
            for _ in range(8):self.obj(depth+1)
            self.take(4);self.obj(depth+1);return
        raise ValueError('unsupported marshal tag '+repr(t))

def _layout(pyc:bytes)->dict[str,int]:
    if len(pyc)<31 or (pyc[8]&0x7f)!=ord('c'):raise ValueError('unexpected PYC root layout')
    r=_Skip(pyc);r.p=9;r.take(16);tag=chr(pyc[r.take(1)]&0x7f)
    if tag not in ('s','t'):raise ValueError('root bytecode is not a marshal string')
    code_len_pos=r.p;code_len=r.i32();code_off=r.take(code_len)
    if chr(pyc[r.take(1)]&0x7f)!='(':raise ValueError('root constants are not a tuple')
    count_pos=r.p;count=r.i32();const_start=r.p
    for _ in range(count):r.obj(1)
    return dict(code_len_pos=code_len_pos,code_len=code_len,code_off=code_off,count_pos=count_pos,count=count,const_start=const_start,const_end=r.p)

def _ops(code:bytes):
    i=0;ext=0
    while i<len(code):
        off=i;op=code[i];i+=1;arg=None
        if op>=90:
            if i+2>len(code):break
            raw=code[i]|(code[i+1]<<8);i+=2;arg=raw|ext
            if op==143:ext=raw<<16;continue
            ext=0
        yield off,op,arg

def _ref_map(pyc:bytes,cls:str,field:str)->dict[int,int|None]:
    M,root,schemas,records=_parse(pyc);out={}
    for r in records:
        if r.class_name==cls:out[int(_plain(r.uid))]=ref_uid(r.fields.get(field))
    return out


def _const_plain(co, index:int)->Any:
    M=_mapper()
    if index is None or index < 0 or index >= len(co.consts):
        return None
    return M.value_plain_for_compare(co.consts[index])


def _legacy_block_at(ins:list[tuple[int,int,int|None]], pos:int, co:Any)->dict[str,Any]|None:
    """Recognize one exact v1.0 late-ApplyPatch block at instruction `pos`."""
    pattern=[101,104,4,104,4,104,4,101,100,25,2,100,60,2,100,60,2,100,60,131,1]
    if pos < 0 or pos+len(pattern)>len(ins):
        return None
    block=ins[pos:pos+len(pattern)]
    if [x[1] for x in block] != pattern:
        return None
    if block[1][2] != 0 or block[3][2] != 0 or block[5][2] != 0 or block[19][2] != 1:
        return None
    apply_idx=block[0][2]
    table_idx=block[7][2]
    if apply_idx is None or apply_idx>=len(co.names) or co.names[apply_idx] != 'ApplyPatch':
        return None
    if table_idx is None or table_idx>=len(co.names):
        return None
    table_name=co.names[table_idx]
    target_uid=_const_plain(co,block[8][2])
    field=_const_plain(co,block[11][2])
    record_uid=_const_plain(co,block[14][2])
    record_type=_const_plain(co,block[17][2])
    valid=(
        (record_type=='DRIVERCONFIG' and field=='TEAM' and table_name=='__RACETEAM') or
        (record_type=='RACETEAM' and field=='MANUFACTURER' and table_name=='__MANUFACTURER')
    )
    if not valid:
        return None
    try:
        record_uid=int(record_uid);target_uid=int(target_uid)
    except Exception:
        return None
    return dict(start=block[0][0],end=block[-1][0]+1,record_type=record_type,
                record_uid=record_uid,field=field,target_uid=target_uid,table_name=table_name)


def legacy_patch_status(pyc:bytes)->dict[str,Any]:
    """Report exact v1.0 team ApplyPatch blocks without changing the file."""
    M,root,_schemas,_records=_parse(pyc);co=root.value
    ins=list(_ops(co.code_bytes))
    if len(ins)<2 or ins[-1][1]!=83:
        return dict(found=False,count=0,blocks=[])
    blocks=[]
    # v1.0 inserted one or more contiguous blocks immediately before the final
    # LOAD_CONST None / RETURN_VALUE pair. Walk backwards in 21-op chunks.
    end_pos=len(ins)-2
    while end_pos>=21:
        cand=_legacy_block_at(ins,end_pos-21,co)
        if cand is None:
            break
        blocks.append(cand);end_pos-=21
    blocks.reverse()
    return dict(found=bool(blocks),count=len(blocks),blocks=blocks)


def strip_legacy_applypatch(pyc:bytes)->tuple[bytes,dict[str,Any]]:
    """Remove only exact v1.0 team blocks; leave their now-unused constants."""
    status=legacy_patch_status(pyc)
    if not status['found']:
        return pyc,dict(found=False,removed=0,blocks=[])
    layout=_layout(pyc)
    blocks=status['blocks']
    first=int(blocks[0]['start']);last=int(blocks[-1]['end'])
    # They are contiguous and directly before the module's final return.
    if any(int(blocks[i]['end'])!=int(blocks[i+1]['start']) for i in range(len(blocks)-1)):
        raise ValueError('legacy team blocks are not contiguous; refusing recovery')
    out=bytearray(pyc)
    del out[layout['code_off']+first:layout['code_off']+last]
    struct.pack_into('<i',out,layout['code_len_pos'],layout['code_len']-(last-first))
    rebuilt=bytes(out)
    # Parser and mapper must still accept the recovered module.
    _parse(rebuilt)
    return rebuilt,dict(found=True,removed=len(blocks),removed_code_bytes=last-first,blocks=blocks)


def _ops_full(code:bytes):
    i=0;ext=0
    while i<len(code):
        off=i;op=code[i];i+=1;arg=None;arg_off=None
        if op>=90:
            if i+2>len(code):break
            raw=code[i]|(code[i+1]<<8);arg=raw|ext;arg_off=i;i+=2
            if op==143:ext=raw<<16;continue
            ext=0
        yield off,op,arg,arg_off


def _find_const_index(co:Any,value:int)->int:
    M=_mapper()
    for i,c in enumerate(co.consts):
        p=M.value_plain_for_compare(c)
        if isinstance(p,bool):
            continue
        try:
            if int(p)==int(value):
                return i
        except Exception:
            pass
    raise ValueError(f'target UID {value} is not present in the stock constant table')


def _direct_link_plan(pyc:bytes,cls:str,uid:int,field:str,target_uid:int)->dict[str,Any]:
    M,root,schemas,records=_parse(pyc);co=root.value
    allowed={
        ('DRIVERCONFIG_c','TEAM'):('__RACETEAM','__DRIVERCONFIG'),
        ('RACETEAM_c','MANUFACTURER'):('__MANUFACTURER','__RACETEAM'),
    }
    if (cls,field) not in allowed:
        raise ValueError(f'{cls}.{field} is not a direct team link')
    target_table,source_table=allowed[(cls,field)]
    by_key={(r.class_name,int(_plain(r.uid))):r for r in records if str(_plain(r.uid)).lstrip('-').isdigit()}
    rec=by_key.get((cls,int(uid)))
    target_cls=ALLOWED_CHANGES[(cls,field)]
    target=by_key.get((target_cls,int(target_uid)))
    if rec is None:raise ValueError(f'{cls} UID {uid} was not found')
    if target is None:raise ValueError(f'{target_cls} UID {target_uid} was not found')
    old_uid=ref_uid(rec.fields.get(field))
    if old_uid==int(target_uid):
        return dict(noop=True,class_name=cls,field=field,uid=int(uid),old_uid=old_uid,target_uid=int(target_uid))
    try:field_name_idx=co.names.index(field)
    except ValueError:raise ValueError(f'{field} attribute name is missing from the database module')
    ops=list(_ops_full(co.code_bytes));candidates=[]
    # Stock generated form:
    # LOAD_NAME target_table; LOAD_CONST target_uid; BINARY_SUBSCR
    # LOAD_NAME source_table; LOAD_CONST record_uid; BINARY_SUBSCR; STORE_ATTR field
    for i in range(6,len(ops)):
        block=ops[i-6:i+1]
        if [x[1] for x in block] != [101,100,25,101,100,25,95]:
            continue
        if block[6][2]!=field_name_idx:
            continue
        n0=co.names[block[0][2]] if block[0][2] is not None and block[0][2]<len(co.names) else ''
        n1=co.names[block[3][2]] if block[3][2] is not None and block[3][2]<len(co.names) else ''
        if n0!=target_table or n1!=source_table:
            continue
        try:
            loaded_target=int(_const_plain(co,block[1][2]));loaded_source=int(_const_plain(co,block[4][2]))
        except Exception:
            continue
        if loaded_source!=int(uid) or loaded_target!=int(old_uid):
            continue
        candidates.append(block)
    if len(candidates)!=1:
        raise ValueError(f'expected one stock {cls}.{field} assignment for UID {uid}, found {len(candidates)}')
    target_const=_find_const_index(co,int(target_uid))
    if target_const>0xffff:raise ValueError('target UID constant requires EXTENDED_ARG')
    block=candidates[0]
    arg_off=block[1][3]
    if arg_off is None:raise ValueError('target link LOAD_CONST has no operand')
    layout=_layout(pyc)
    return dict(noop=False,class_name=cls,field=field,uid=int(uid),old_uid=int(old_uid),
                target_uid=int(target_uid),target_const_index=int(target_const),
                code_offset=int(block[1][0]),operand_abs=int(layout['code_off']+arg_off))


def build_changes(pyc:bytes,changes:list[dict[str,Any]])->tuple[bytes,dict[str,Any]]:
    """Strip unsafe legacy blocks, then repoint stock STORE_ATTR link operands."""
    cleaned,recovery=strip_legacy_applypatch(pyc)
    if not changes:
        return cleaned,dict(changes=[],noop=(cleaned==pyc),recovery=recovery,verified=True)
    work=cleaned;normalized=[]
    for raw in changes:
        cls=str(raw['class_name']);field=str(raw['field']);uid=int(raw['uid']);target_uid=int(raw['target_uid'])
        if (cls,field) not in ALLOWED_CHANGES:
            raise ValueError(f'{cls}.{field} is not an allowed team edit')
        plan=_direct_link_plan(work,cls,uid,field,target_uid)
        if plan.get('noop'):
            continue
        out=bytearray(work)
        struct.pack_into('<H',out,int(plan['operand_abs']),int(plan['target_const_index']))
        candidate=bytes(out)
        # Exact semantic guard: exactly one requested link must change.
        before=_ref_map(work,cls,field);after=_ref_map(candidate,cls,field)
        actual={k:(before.get(k),after.get(k)) for k in set(before)|set(after) if before.get(k)!=after.get(k)}
        expected={int(uid):(int(plan['old_uid']),int(target_uid))}
        if actual!=expected:
            raise ValueError('direct team-link diff guard failed: '+repr(dict(expected=expected,actual=actual)))
        work=candidate
        normalized.append(dict(class_name=cls,field=field,uid=uid,target_class=ALLOWED_CHANGES[(cls,field)],
                               target_uid=target_uid,old_uid=plan['old_uid'],method='stock_store_attr_operand_repoint',
                               code_offset=plan['code_offset']))
    if not normalized and work==pyc:
        return pyc,dict(changes=[],noop=True,recovery=recovery,verified=True)
    # All record counts must remain unchanged and final readbacks must match.
    _M0,_r0,_s0,recs0=_parse(pyc);_M1,_r1,_s1,recs1=_parse(work)
    counts0={};counts1={}
    for r in recs0:counts0[r.class_name]=counts0.get(r.class_name,0)+1
    for r in recs1:counts1[r.class_name]=counts1.get(r.class_name,0)+1
    if counts0!=counts1:raise ValueError('team edit unexpectedly changed database record counts')
    return work,dict(changes=normalized,noop=False,recovery=recovery,verified=True,
                     method='direct_stock_link_repoint',size_delta=len(work)-len(pyc))
