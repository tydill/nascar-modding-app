#!/usr/bin/env python3
"""Protected PatchMode/ApplyPatch helper used by the extra-scheme manager.

This restores the dependency that the public package accidentally omitted.  It
builds the same guarded Python-2.5 code shape used by the manager: switch
BaseGDTObject_c.__setattr__ to PatchSetAttr, construct a cloned LIVERIE_c with a
new UID/ScriptName, retarget the recipient links, then call ApplyPatch(DATA).
The caller reparses the rebuilt PYC and proves that exactly one record was added
before any game file is written.
"""
from __future__ import annotations
import struct
import nascar15_inrange_true_extra_scheme_probe_v0_4_base as base
VERSION='0.9-compat-rc11.1'

def name_index(ctx,name):
    try:return ctx.root.value.names.index(str(name))
    except ValueError:raise ValueError(f'root bytecode has no name {name!r}')

def const_index(ctx,value):
    for i,item in enumerate(ctx.root.value.consts):
        if ctx.mapper.value_plain_for_compare(item)==value:return i
    raise ValueError(f'root bytecode has no constant {value!r}')

def _marshal_interned(text):
    raw=str(text).encode('ascii','strict');return b't'+struct.pack('<i',len(raw))+raw

def _constructor(ctx,donor,uid_idx,script_idx):
    co=ctx.root.value;ins=base.py2_instructions(ctx,co.code_bytes)
    call_i=next((i for i,x in enumerate(ins) if x['offset']==donor.call_offset and x['opname']=='CALL_FUNCTION'),None)
    if call_i is None or ins[call_i]['arg']!=17:raise ValueError('could not find donor LIVERIE_c CALL_FUNCTION 17')
    donor_uid=base.pointer_int(ctx,donor.uid);donor_script=base.display(ctx,donor.fields.get('ScriptName'))
    start_i=None
    for i in range(call_i-1,-1,-1):
        x=ins[i]
        if x['opname']=='LOAD_NAME' and x['arg'] is not None and co.names[x['arg']]=='LIVERIE_c':
            if i+1<len(ins) and ins[i+1]['opname']=='LOAD_CONST' and base.const_plain(ctx,ins[i+1]['arg'])==donor_uid:
                start_i=i;break
    if start_i is None:raise ValueError('could not locate donor constructor start')
    tail=ins[call_i:call_i+4]
    if [x['opname'] for x in tail]!=['CALL_FUNCTION','ROT_TWO','LOAD_CONST','STORE_SUBSCR']:
        raise ValueError('unexpected donor constructor tail')
    start=ins[start_i]['offset'];end=tail[-1]['offset']+1
    raw=bytearray(co.code_bytes[start:end]);clone=bytearray(base.emit(101,name_index(ctx,'__LIVERIE'))+raw)
    uid_hits=[];script_hits=[]
    for x in base.py2_instructions(ctx,bytes(clone)):
        if x['opname']!='LOAD_CONST':continue
        value=base.const_plain(ctx,x['arg'])
        if value==donor_uid:
            struct.pack_into('<H',clone,x['arg_pos'],uid_idx);uid_hits.append(x['offset'])
        elif value==donor_script:
            struct.pack_into('<H',clone,x['arg_pos'],script_idx);script_hits.append(x['offset'])
    if len(uid_hits)!=2:raise ValueError(f'expected two donor UID operands; found {len(uid_hits)}')
    if len(script_hits)!=1:raise ValueError(f'expected one donor ScriptName operand; found {len(script_hits)}')
    return bytes(clone),dict(source_start=start,source_end=end,uid_operand_count=2,script_operand_count=1)

def build_applypatch_code(ctx,donor,new_uid,script_name,driver_uid,world_uid=None):
    co=ctx.root.value
    if len(co.consts)+1>=0xffff:raise ValueError('root constant table is too large')
    uid_idx=len(co.consts);script_idx=uid_idx+1
    ctor,ctor_meta=_constructor(ctx,donor,uid_idx,script_idx)
    assignments=base.post_assignment_blocks(ctx,base.pointer_int(ctx,donor.uid))
    required={'Driver','Package','World','Season'}
    if not required.issubset({a['field'] for a in assignments}):raise ValueError('donor post-assignment map is incomplete')
    recipient_const=const_index(ctx,int(driver_uid))
    world_const=const_index(ctx,int(world_uid)) if world_uid is not None else None
    cloned=bytearray();meta=[]
    donor_uid=base.pointer_int(ctx,donor.uid)
    for a in assignments:
        raw=bytearray(a['raw']);target_hits=0;source_hits=0
        for x in base.py2_instructions(ctx,bytes(raw)):
            if x['opname']!='LOAD_CONST':continue
            value=base.const_plain(ctx,x['arg'])
            if value==donor_uid:
                struct.pack_into('<H',raw,x['arg_pos'],uid_idx);target_hits+=1
            elif a['field']=='Driver' and value==a['source_uid']:
                struct.pack_into('<H',raw,x['arg_pos'],recipient_const);source_hits+=1
            elif a['field']=='World' and world_const is not None and value==a['source_uid']:
                struct.pack_into('<H',raw,x['arg_pos'],world_const);source_hits+=1
        if target_hits!=1:raise ValueError(f"{a['field']} clone target replacement failed")
        if a['field']=='Driver' and source_hits!=1:raise ValueError('Driver source replacement failed')
        if a['field']=='World' and world_const is not None and source_hits!=1:raise ValueError('World source replacement failed')
        cloned+=raw;meta.append({k:a[k] for k in ('field','source_table','source_uid','start','end')})
    # Python 2.5 bytecode: BaseGDTObject_c.__setattr__ = PatchSetAttr
    code=bytearray()
    code+=base.emit(101,name_index(ctx,'PatchSetAttr'))
    code+=base.emit(101,name_index(ctx,'BaseGDTObject_c'))
    code+=base.emit(95,name_index(ctx,'__setattr__')) # STORE_ATTR
    code+=ctor+cloned
    code+=base.emit(101,name_index(ctx,'ApplyPatch'))
    code+=base.emit(101,name_index(ctx,'DATA'))
    code+=base.emit(131,1) # CALL_FUNCTION
    code+=base.emit(1)     # POP_TOP
    encoded=base.marshal_int(int(new_uid))+_marshal_interned(script_name)
    return bytes(code),dict(new_uid=int(new_uid),uid_const_index=uid_idx,script_const_index=script_idx,
        script_name=str(script_name),constructor=ctor_meta,assignments=meta,total_code_bytes=len(code),
        driver_uid=int(driver_uid),world_uid=(int(world_uid) if world_uid is not None else None),
        patch_mode='PatchSetAttr + ApplyPatch(DATA)'),encoded
