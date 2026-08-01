#!/usr/bin/env python3
"""NASCAR 15 Unique-ScriptName Extra Scheme Diagnostic Probe v0.5.

This is a diagnostic-only true-slot test. It creates a 337th LIVERIE_c record
using a free in-range UID and a UNIQUE ScriptName. It deliberately does not add
matching LIVERY_/HDLIVERY_ archive entries yet.

Purpose:
- If the game reaches the main menu, the earlier startup loop was caused by the
  duplicated ScriptName/asset identity, not by the 337th livery record itself.
- If the game still loops before the main menu, the evidence strongly points to
  a native fixed-count/secondary-registry limit outside DB_GAME_LOCAL_SCRIPT.PYC.

Do not select/start a race with the test scheme if the game reaches the menu;
its unique paint files do not exist in this diagnostic.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, struct, sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
BASE_PATH=HERE/'nascar15_inrange_true_extra_scheme_probe_v0_4_base.py'
spec=importlib.util.spec_from_file_location('n15base',str(BASE_PATH))
if spec is None or spec.loader is None: raise RuntimeError('Could not load base probe')
base=importlib.util.module_from_spec(spec); sys.modules['n15base']=base; spec.loader.exec_module(base)

VERSION='0.5'
DEFAULT_NEW_SCRIPT='15_47_AJ_EXTRA_SLOT_TEST'
NEW_SCRIPT_NAME=DEFAULT_NEW_SCRIPT

# Keep this probe's backups/manifests separate from every earlier probe.
base.VERSION=VERSION
base.BACKUP_SUFFIX='.unique_script_extra_scheme_probe_v0_5.bak'
base.MANIFEST_NAME='unique_script_extra_scheme_v0_5_manifest.json'
base.ANALYSIS_JSON='unique_script_extra_scheme_v0_5_analysis.json'
base.CONNECTION_MD='unique_script_extra_scheme_v0_5_connection_map.md'
base.CONNECTION_CSV='unique_script_extra_scheme_v0_5_asset_locations.csv'


def marshal_interned_string(text:str)->bytes:
    raw=text.encode('ascii')
    return b't'+struct.pack('<i',len(raw))+raw


def constructor_block_unique(ctx, donor, uid_const_index:int, script_const_index:int):
    co=ctx.root.value
    instructions=base.py2_instructions(ctx,co.code_bytes)
    call_index=next((i for i,x in enumerate(instructions)
                     if x['offset']==donor.call_offset and x['opname']=='CALL_FUNCTION'),None)
    if call_index is None or instructions[call_index]['arg']!=17:
        raise ValueError('Could not find donor LIVERIE_c CALL_FUNCTION 17')
    donor_uid=base.pointer_int(ctx,donor.uid)
    donor_script=base.display(ctx,donor.fields.get('ScriptName'))
    start_index=None
    for i in range(call_index-1,-1,-1):
        x=instructions[i]
        if x['opname']=='LOAD_NAME' and x['arg'] is not None and co.names[x['arg']]=='LIVERIE_c':
            if i+1<len(instructions) and instructions[i+1]['opname']=='LOAD_CONST' and base.const_plain(ctx,instructions[i+1]['arg'])==donor_uid:
                start_index=i; break
    if start_index is None: raise ValueError('Could not locate donor constructor start')
    tail=instructions[call_index:call_index+4]
    if [x['opname'] for x in tail] != ['CALL_FUNCTION','ROT_TWO','LOAD_CONST','STORE_SUBSCR']:
        raise ValueError('Unexpected donor constructor tail')
    start=instructions[start_index]['offset']; end=tail[-1]['offset']+1
    raw=bytearray(co.code_bytes[start:end])
    try: table_name=co.names.index('__LIVERIE')
    except ValueError: raise ValueError('Root bytecode has no __LIVERIE table name')
    clone=bytearray(base.emit(101,table_name)+raw)
    uid_hits=[]; script_hits=[]
    for ins in base.py2_instructions(ctx,bytes(clone)):
        if ins['opname']!='LOAD_CONST': continue
        val=base.const_plain(ctx,ins['arg'])
        if val==donor_uid:
            struct.pack_into('<H',clone,ins['arg_pos'],uid_const_index); uid_hits.append(ins['offset'])
        elif val==donor_script:
            struct.pack_into('<H',clone,ins['arg_pos'],script_const_index); script_hits.append(ins['offset'])
    if len(uid_hits)!=2: raise ValueError(f'Expected two donor UID operands; found {len(uid_hits)}')
    if len(script_hits)!=1: raise ValueError(f'Expected one donor ScriptName operand; found {len(script_hits)}')
    return bytes(clone),dict(source_start=start,source_end=end,source_call=donor.call_offset,
                             uid_operand_count=len(uid_hits),script_operand_count=len(script_hits),
                             donor_script=donor_script,new_script=NEW_SCRIPT_NAME,clone_bytes=len(clone))


def build_clone_code_unique(ctx, donor, new_uid:int, recipient_uid:int):
    co=ctx.root.value
    if len(co.consts)+1>=0xFFFF: raise ValueError('Root constant table too large')
    uid_idx=len(co.consts); script_idx=len(co.consts)+1
    ctor,ctor_meta=constructor_block_unique(ctx,donor,uid_idx,script_idx)
    assignments=base.post_assignment_blocks(ctx,base.pointer_int(ctx,donor.uid))
    required={'Driver','Package','World','Season'}
    if not required.issubset({a['field'] for a in assignments}):
        raise ValueError('Donor post-assignment map is incomplete')
    recipient_const=base.find_const_index(ctx,recipient_uid)
    clone_assignments=bytearray(); meta=[]
    for a in assignments:
        raw=bytearray(a['raw']); target=source=0
        for item in base.py2_instructions(ctx,bytes(raw)):
            if item['opname']!='LOAD_CONST': continue
            value=base.const_plain(ctx,item['arg'])
            if value==base.pointer_int(ctx,donor.uid):
                struct.pack_into('<H',raw,item['arg_pos'],uid_idx); target+=1
            elif a['field']=='Driver' and value==a['source_uid']:
                struct.pack_into('<H',raw,item['arg_pos'],recipient_const); source+=1
        if target!=1: raise ValueError(f"{a['field']} clone target replacement failed")
        if a['field']=='Driver' and source!=1: raise ValueError('Driver source replacement failed')
        clone_assignments+=raw
        meta.append({k:a[k] for k in ('field','source_table','source_uid','start','end')})
    return bytes(ctor+clone_assignments),dict(new_uid=new_uid,uid_const_index=uid_idx,
        script_const_index=script_idx,unique_script_name=NEW_SCRIPT_NAME,constructor=ctor_meta,
        assignments=meta,total_code_bytes=len(ctor)+len(clone_assignments))


def build_patched_pyc_unique(ctx, donor_uid:int, original_driver_uid:int, recipient_uid:int, preferred_new_uid:int):
    donor=base.find_record(ctx,'LIVERIE_c',donor_uid)
    current_driver=base.field_uid(ctx,donor,'Driver')
    if current_driver not in (original_driver_uid,recipient_uid):
        raise ValueError(f'Donor currently belongs to unexpected driver {current_driver}')
    # Enforce unique name against all stock/current liveries.
    existing={base.display(ctx,r.fields.get('ScriptName')) for r in base.records_of(ctx,'LIVERIE_c')}
    if NEW_SCRIPT_NAME in existing: raise ValueError(f'ScriptName {NEW_SCRIPT_NAME!r} already exists')
    new_uid=base.choose_new_uid(ctx,preferred_new_uid)
    if new_uid>=25600: raise ValueError('Probe refuses UID 25600 or higher')
    code,clone_meta=build_clone_code_unique(ctx,donor,new_uid,recipient_uid)
    layout=base.root_layout(ctx.pyc); co=ctx.root.value
    ins=base.py2_instructions(ctx,co.code_bytes)
    data_store=next((i for i,x in enumerate(ins) if x['opname']=='STORE_NAME' and x['arg'] is not None and co.names[x['arg']]=='DATA'),None)
    if data_store is None: raise ValueError('Could not locate DATA registry')
    build_map=next((i for i in range(data_store-1,-1,-1) if ins[i]['opname']=='BUILD_MAP'),None)
    if build_map is None: raise ValueError('Could not locate DATA BUILD_MAP')
    insert_off=ins[build_map]['offset']
    if any(('JUMP' in x['opname'] or x['opname'] in ('FOR_ITER','SETUP_LOOP','SETUP_EXCEPT','SETUP_FINALLY','CONTINUE_LOOP')) for x in ins):
        raise ValueError('Root module has control-flow jumps; insertion refused')
    out=bytearray(ctx.pyc)
    donor_restore=None
    if current_driver!=original_driver_uid:
        operand=base.locate_driver_assignment_operand(ctx,donor_uid)
        orig_const=base.find_const_index(ctx,original_driver_uid)
        struct.pack_into('<H',out,operand['pyc_argument_offset'],orig_const)
        donor_restore=dict(from_driver_uid=current_driver,to_driver_uid=original_driver_uid,operand=operand,new_const_index=orig_const)
    absolute=layout['code_off']+insert_off
    out[absolute:absolute]=code
    delta=len(code)
    struct.pack_into('<i',out,layout['code_len_pos'],layout['code_len']+delta)
    count_pos=layout['count_pos']+delta; const_end=layout['const_end']+delta
    struct.pack_into('<i',out,count_pos,layout['count']+2)
    encoded_uid=base.marshal_int(new_uid); encoded_script=marshal_interned_string(NEW_SCRIPT_NAME)
    out[const_end:const_end]=encoded_uid+encoded_script
    rebuilt=bytes(out)
    return rebuilt,dict(new_uid=new_uid,old_pyc_size=len(ctx.pyc),new_pyc_size=len(rebuilt),growth=len(rebuilt)-len(ctx.pyc),
        insert_code_offset=insert_off,insert_before_master_DATA=True,insert_code_bytes=delta,
        marshal_uid_bytes=len(encoded_uid),marshal_script_bytes=len(encoded_script),clone=clone_meta,donor_restore=donor_restore,
        diagnostic='unique ScriptName; matching paint assets intentionally absent')


def validate_unique(ctx, rebuilt:bytes, donor_uid:int, original_driver_uid:int, recipient_uid:int, new_uid:int):
    m=ctx.mapper; root2=m.parse_pyc(rebuilt); schemas2=m.build_schemas(root2); recs2=m.map_records(root2,schemas2)
    after=base.Context(ctx.game,ctx.archive,ctx.cdfiles,ctx.row,rebuilt,m,ctx.containers,root2,schemas2,recs2)
    before={base.pointer_int(ctx,r.uid):r for r in base.records_of(ctx,'LIVERIE_c')}
    after_l={base.pointer_int(after,r.uid):r for r in base.records_of(after,'LIVERIE_c')}
    if set(after_l)!=set(before)|{new_uid}: raise ValueError('Livery UID set did not gain exactly one UID')
    donor=after_l[donor_uid]; clone=after_l[new_uid]
    if base.field_uid(after,donor,'Driver')!=original_driver_uid: raise ValueError('Donor not retained by original driver')
    if base.field_uid(after,clone,'Driver')!=recipient_uid: raise ValueError('Clone not assigned to recipient')
    if base.display(after,clone.fields.get('ScriptName'))!=NEW_SCRIPT_NAME: raise ValueError('Unique ScriptName not applied')
    donor_sig=base.record_signature(after,donor); clone_sig=base.record_signature(after,clone)
    for field in after.schemas['LIVERIE_c'].fields:
        if field in ('UID','Driver','ScriptName'): continue
        if donor_sig.get(field)!=clone_sig.get(field): raise ValueError(f'Clone field {field} differs from donor')
    scripts=[base.display(after,r.fields.get('ScriptName')) for r in base.records_of(after,'LIVERIE_c')]
    if len(scripts)!=len(set(scripts)): raise ValueError('ScriptName values are not unique after rebuild')
    # Existing records may only include restoring donor from an earlier reassignment.
    changes=[]
    for uid,old in before.items():
        ns=base.record_signature(after,after_l[uid]); osig=base.record_signature(ctx,old)
        for f in sorted(set(osig)|set(ns)):
            if osig.get(f)!=ns.get(f): changes.append(dict(uid=uid,field=f,before=osig.get(f),after=ns.get(f)))
    allowed=[]
    if base.field_uid(ctx,before[donor_uid],'Driver')!=original_driver_uid: allowed=[{'uid':donor_uid,'field':'Driver'}]
    if [{'uid':x['uid'],'field':x['field']} for x in changes]!=allowed: raise ValueError(f'Unexpected existing record changes: {changes[:10]}')
    # Counts: only LIVERIE +1.
    bc={}; ac={}
    for r in ctx.records: bc[r.class_name]=bc.get(r.class_name,0)+1
    for r in recs2: ac[r.class_name]=ac.get(r.class_name,0)+1
    for cls in set(bc)|set(ac):
        d=ac.get(cls,0)-bc.get(cls,0); exp=1 if cls=='LIVERIE_c' else 0
        if d!=exp: raise ValueError(f'Unexpected {cls} count delta {d}')
    return dict(verified=True,before_livery_count=len(before),after_livery_count=len(after_l),
                unique_script_names=True,existing_record_changes=changes,
                donor_after=base.livery_summary(after,donor),clone=base.livery_summary(after,clone),
                pyc_sha256=base.sha256_bytes(rebuilt))

base.build_patched_pyc=build_patched_pyc_unique
base.validate_rebuild=validate_unique

# Make the printed/report conclusion honest for this diagnostic.
_old_conn=base.connection_map
def connection_map_unique(ctx,donor,new_uid,recipient_uid):
    out=_old_conn(ctx,donor,new_uid,recipient_uid)
    out['expected_unique_script_name']=NEW_SCRIPT_NAME
    out['connection_conclusions']=[
        'This diagnostic isolates duplicate ScriptName versus a 337-record native limit.',
        'The clone uses a unique ScriptName and a free in-range UID.',
        'Matching SD/HD paint files are intentionally absent, so do not load the test scheme into a race.',
        'If the game reaches the main menu, duplicate ScriptName caused the earlier startup loop.',
        'If the game still loops before the main menu, a fixed count or secondary native registry is strongly indicated.'
    ]
    return out
base.connection_map=connection_map_unique


def parser():
    p=argparse.ArgumentParser(description='Diagnose NASCAR 15 extra-livery startup limit with a unique ScriptName')
    p.add_argument('command',choices=['analyze','apply','restore'])
    p.add_argument('--game',default=None)
    p.add_argument('--donor-uid',type=int,default=25580)
    p.add_argument('--original-driver-uid',type=int,default=1115)
    p.add_argument('--recipient-driver-uid',type=int,default=1083)
    p.add_argument('--new-uid',type=int,default=25582)
    p.add_argument('--new-script-name',default=DEFAULT_NEW_SCRIPT)
    return p

def main(argv=None):
    global NEW_SCRIPT_NAME
    args=parser().parse_args(argv); NEW_SCRIPT_NAME=args.new_script_name
    # Base commands only consume the common args and our monkey-patched funcs use global name.
    return {'analyze':base.cmd_analyze,'apply':base.cmd_apply,'restore':base.cmd_restore}[args.command](args)

if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as ex:
        print(f'ERROR: {ex}',file=sys.stderr)
        import traceback; traceback.print_exc(); raise SystemExit(1)
