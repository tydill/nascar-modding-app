#!/usr/bin/env python3
"""Safe NASCAR 15 2015 Cup schedule mapper/patcher used by Grid Builder v0.9.26.

The 36 normal Cup races are selected from RACEDATA_c by series, positive laps,
and a unique NumberInSeries sequence of 1..36. v0.9.26 keeps those 36 calendar
slots fixed but can repoint each slot's EventName, RaceEvent pointer, and
RaceLaps LOAD_CONST operands to any existing Cup event, including duplicates.
The PYC size never changes and every record is reparsed.
"""
from __future__ import annotations
import importlib.util, os, re, struct, sys
from dataclasses import dataclass, asdict
from typing import Any

class ScheduleError(RuntimeError): pass
_HELPER_CACHE={}
ACTIVE_SEASON_YEAR=2015

def configure(season_year=2015):
    global ACTIVE_SEASON_YEAR
    ACTIVE_SEASON_YEAR=int(season_year)
    return ACTIVE_SEASON_YEAR


@dataclass
class ScheduleRow:
    uid:int; event:str; order:int; date:int; laps:int; drivers:int; race_event:str; series:str; event_uid:int|None=None
    @property
    def track(self):
        text=self.race_event or ''
        m=re.search(r"EVENT_c\([^,]+,[^,]+,\s*([^,]+)",text)
        if m: return m.group(1).strip()
        s=self.event or str(self.uid)
        s=re.sub(r'^S_EVT_','',s,flags=re.I).replace('_',' ').title()
        return s
    def public(self):
        d=asdict(self); d['track']=self.track; return d

def load_module(path:str,name:str):
    spec=importlib.util.spec_from_file_location(name,path)
    if not spec or not spec.loader: raise ScheduleError('cannot load '+path)
    mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod; spec.loader.exec_module(mod); return mod

def plain(mapper,v):
    try:return mapper.value_plain_for_compare(v)
    except Exception:return getattr(v,'value',v)

def display(mapper,v):
    try:return mapper.value_to_display(v)
    except Exception:return str(plain(mapper,v))


def event_pointer_uid(mapper,v):
    # RaceEvent may be EVENT_c directly, wrapped in MVal, or nested inside an
    # UnexportedPointer_c/other generated constructor. Walk the actual value
    # first, then recover from the rendered text without requiring EVENT_c to
    # begin at character zero.
    seen=set()
    def walk(x):
        if id(x) in seen:return None
        seen.add(id(x))
        inner=getattr(x,'value',None)
        if inner is not None and inner is not x:
            found=walk(inner)
            if found is not None:return found
        func=getattr(x,'func',None);args=getattr(x,'args',None)
        if func=='EVENT_c' and args:
            try:return int(plain(mapper,args[0]))
            except Exception:
                found=walk(args[0])
                if found is not None:return found
        if args:
            for arg in args:
                found=walk(arg)
                if found is not None:return found
        return None
    found=walk(v)
    if found is not None:return found
    text=display(mapper,v)
    m=re.search(r'\bEVENT_c\s*\(\s*(-?\d+)',text)
    return int(m.group(1)) if m else None

def map_schedule(raw:bytes,mapper):
    root=mapper.parse_pyc(raw); schemas=mapper.build_schemas(root); records=mapper.map_records(root,schemas)
    candidates=[]; raw_by_uid={}
    for rec in records:
        if rec.class_name.upper()!='RACEDATA_C': continue
        try:
            uid=int(rec.uid); series=display(mapper,rec.fields.get('RaceSeries'))
            order=int(plain(mapper,rec.fields.get('NumberInSeries')))
            date=int(plain(mapper,rec.fields.get('RaceDate')))
            laps=int(plain(mapper,rec.fields.get('RaceLaps')))
            drivers=int(plain(mapper,rec.fields.get('NumDrivers')))
        except Exception: continue
        if 'S_SERIES_SPRINT_CUP' not in series or str(ACTIVE_SEASON_YEAR) not in series: continue
        if not (1<=order<=36) or laps<=0: continue
        event=str(plain(mapper,rec.fields.get('EventName')) or '')
        race_value=rec.fields.get('RaceEvent'); race_event=display(mapper,race_value)
        event_uid=event_pointer_uid(mapper,race_value)
        fake_raw=plain(mapper,rec.fields.get('FakeRaceData'))
        fake=bool(fake_raw) if fake_raw is not None else False
        row=ScheduleRow(uid,event,order,date,laps,drivers,race_event,series,event_uid)
        candidates.append((row,fake)); raw_by_uid[uid]=rec
    if ACTIVE_SEASON_YEAR==2014:
        # NASCAR '14 ships duplicate/fake variants at several calendar positions.
        # Keep one deterministic public slot per order, preferring a real record,
        # then the normal 43-driver entry, then the lowest stable UID.
        by_order={}
        for row,fake in candidates:
            rank=(1 if fake else 0,0 if row.drivers==43 else 1,row.uid)
            if row.order not in by_order or rank<by_order[row.order][0]: by_order[row.order]=(rank,row)
        selected=[by_order[o][1] for o in sorted(by_order)]
    else:
        selected=[row for row,_fake in candidates]
        selected.sort(key=lambda x:x.order)
    if len(selected)!=36:
        raise ScheduleError(f'expected 36 normal {ACTIVE_SEASON_YEAR} Cup races, found {len(selected)}')
    orders=[x.order for x in selected]
    if orders!=list(range(1,37)):
        raise ScheduleError(f'{ACTIVE_SEASON_YEAR} schedule is not a unique 1-36 sequence: {orders}')
    selected_uids={r.uid for r in selected}
    return selected,{uid:rec for uid,rec in raw_by_uid.items() if uid in selected_uids}

def locate_record_arg(repoint,codes,uid,arg_index):
    matches=[]
    for code in codes:
        args=repoint.find_record_loadconsts(code,int(uid))
        if args and arg_index<len(args): matches.append((code,args[arg_index]))
    if len(matches)!=1: raise ScheduleError(f'UID {uid} arg[{arg_index}] matched {len(matches)} locations; refused')
    return matches[0]


def _record_args(repoint,codes,uid):
    hits=[]
    for code in codes:
        args=repoint.find_record_loadconsts(code,int(uid))
        if args:hits.append((code,args))
    if len(hits)!=1:raise ScheduleError(f'UID {uid} constructor matched {len(hits)} code locations; refused')
    return hits[0]


def _same_value(a,b):
    if isinstance(a,bool) or isinstance(b,bool):return type(a) is type(b) and a==b
    if isinstance(a,(int,float)) and isinstance(b,(int,float)):return a==b
    return type(a) is type(b) and a==b


def _const_index(co,value):
    for idx,v in enumerate(co.consts):
        if _same_value(v,value):return idx
    return None


def _infer_arg_index(rows,records,codes,repoint,mapper,getter,label):
    votes={};considered=0;ambiguous=0
    for row in rows:
        rec=records.get(row.uid)
        if rec is None:continue
        expected=getter(row,rec)
        if expected is None:continue
        co,args=_record_args(repoint,codes,row.uid)
        hits=[i for i,a in enumerate(args) if _same_value(a.get('value'),expected)]
        considered+=1
        if len(hits)==1:votes[hits[0]]=votes.get(hits[0],0)+1
        else:ambiguous+=1
    if not votes:raise ScheduleError(f'could not infer the {label} constructor operand')
    ranked=sorted(votes.items(),key=lambda kv:(-kv[1],kv[0]));idx,count=ranked[0]
    second=ranked[1][1] if len(ranked)>1 else 0
    needed=max(8,int(max(1,considered)*0.70))
    if count<needed or count<=second:
        raise ScheduleError(f'{label} operand inference was not stable ({count}/{considered}, second={second}, ambiguous={ambiguous})')
    return idx,dict(index=idx,matches=count,considered=considered,ambiguous=ambiguous)


def _find_event_call(v):
    """Return the nested EVENT_c ObjCall carried by RaceEvent.

    The RaceEvent field is commonly wrapped in UnexportedPointer_c, so the
    outer RACEDATA constructor does not expose its UID as a direct argument.
    """
    seen=set()
    def walk(x):
        if id(x) in seen:return None
        seen.add(id(x))
        if getattr(x,'func',None)=='EVENT_c':return x
        inner=getattr(x,'value',None)
        if inner is not None and inner is not x:
            found=walk(inner)
            if found is not None:return found
        for a in (getattr(x,'args',None) or []):
            found=walk(a)
            if found is not None:return found
        return None
    return walk(v)

def _mapper_instructions(mapper,co):
    code=co.code_bytes;i=0;ext=0
    while i<len(code):
        off=i;op=code[i];i+=1;arg=None;arg_off=None
        if op>=mapper.HAVE_ARGUMENT:
            if i+2>len(code):break
            arg_off=i;arg=code[i] | (code[i+1]<<8) | ext;i+=2
            if op==143:
                ext=arg<<16;continue
            ext=0
        yield off,mapper.OP.get(op,f'OP_{op}'),arg,arg_off

def _root_repoint_code(codes,mapper_root):
    candidates=[c for c in codes if c.name==mapper_root.value.name and len(c.code)==len(mapper_root.value.code_bytes)]
    if len(candidates)!=1:
        # Generated DB files normally use the module code object for records.
        candidates=[c for c in codes if c.name=='<module>' and len(c.code)==len(mapper_root.value.code_bytes)]
    if len(candidates)!=1:raise ScheduleError('could not locate the schedule module code object')
    return candidates[0]

def _event_uid_operand(raw,mapper,repoint,codes,rec,mapper_root=None,mapper_ins=None,repoint_code=None):
    """Locate the direct LOAD_CONST used by the nested EVENT_c UID.

    Previous builds tried to infer RaceEvent as an outer RACEDATA_c argument.
    That fails because RaceEvent is an inline EVENT_c(...) constructor.  The
    mapper already records the exact EVENT_c CALL_FUNCTION offset, so locate
    the EVENT_c LOAD_NAME and its UID constant directly.
    """
    value=rec.fields.get('RaceEvent');call=_find_event_call(value)
    if call is None:raise ScheduleError(f'UID {rec.uid}: RaceEvent has no EVENT_c constructor')
    expected=event_pointer_uid(mapper,value)
    if expected is None:raise ScheduleError(f'UID {rec.uid}: RaceEvent UID is unavailable')
    root=mapper_root or mapper.parse_pyc(raw);mco=root.value;rco=repoint_code or _root_repoint_code(codes,root)
    ins=mapper_ins or list(_mapper_instructions(mapper,mco))
    # Find the last explicit EVENT_c load before this exact call.
    starts=[]
    for n,(off,op,arg,arg_off) in enumerate(ins):
        if off>=call.call_offset:break
        if op in ('LOAD_NAME','LOAD_GLOBAL') and arg is not None and arg<len(mco.names) and mco.names[arg]=='EVENT_c':
            starts.append((n,off))
    if not starts:raise ScheduleError(f'UID {rec.uid}: EVENT_c loader not found before call')
    start_n,start_off=starts[-1]
    candidates=[]
    for off,op,arg,arg_off in ins[start_n+1:]:
        if off>=call.call_offset:break
        if op=='CALL_FUNCTION':
            # EVENT_c arguments contain no nested calls in the stock database.
            break
        if op=='LOAD_CONST' and arg is not None and arg<len(mco.consts):
            try:v=plain(mapper,mco.consts[arg])
            except Exception:v=getattr(mco.consts[arg],'value',None)
            if isinstance(v,int) and not isinstance(v,bool) and v==expected:
                candidates.append(dict(instr_off=off,arg_off=arg_off,const_index=arg,value=v))
    if len(candidates)!=1:
        raise ScheduleError(f'UID {rec.uid}: EVENT_c UID operand matched {len(candidates)} locations')
    return rco,candidates[0],dict(call_offset=call.call_offset,loader_offset=start_off,current_uid=expected)

def _patch_arg(buf,co,arg,value,label):
    idx=_const_index(co,value)
    if idx is None:
        available=[]
        if isinstance(value,int):available=sorted({v for v in co.consts if isinstance(v,int) and not isinstance(v,bool) and 1<=v<=999})
        extra=f'; existing 1-999 constants include {available[:80]}' if available else ''
        raise ScheduleError(f'{label}: value {value!r} is not in this PYC constant table{extra}')
    if idx>0xffff:raise ScheduleError(f'{label}: target constant needs EXTENDED_ARG')
    off=co.code_off+arg['arg_off'];cur=struct.unpack_from('<H',buf,off)[0]
    # Some RACEDATA records alias the same inline EVENT_c constructor operand.
    # Repeated fills may revisit that location. Accept an earlier identical
    # write, but reject a conflicting request for a third value.
    if cur==idx:
        return off,cur,idx
    if cur!=arg['const_index']:
        raise ScheduleError(f'{label}: shared bytecode operand already targets a different value (current const {cur}, expected {arg["const_index"]}, wanted {idx})')
    struct.pack_into('<H',buf,off,idx)
    return off,arg['const_index'],idx


def apply_custom(raw:bytes,slots:list[dict],mapper,repoint):
    """Fill the fixed 36 calendar slots from existing Cup event definitions.

    Slot target UIDs remain unique and keep NumberInSeries/RaceDate. Source
    events may repeat. Only EventName, the EVENT_c pointer UID, and RaceLaps are
    repointed; every other RACEDATA field must survive byte-for-byte logically.
    """
    current,before_records=map_schedule(raw,mapper)
    if not isinstance(slots,list) or len(slots)!=36:raise ScheduleError('custom schedule must contain exactly 36 slots')
    target_by_order={r.order:r for r in current};target_uids={r.uid for r in current}
    seen_targets=[];normalized=[]
    for i,item in enumerate(slots,1):
        try:
            target_uid=int(item.get('target_uid',target_by_order[i].uid));event_uid=int(item['event_uid'])
            event_name=str(item['event_name']);laps=int(item['laps'])
        except Exception:raise ScheduleError(f'slot {i}: invalid target/event/laps payload')
        if target_uid!=target_by_order[i].uid:raise ScheduleError(f'slot {i}: target UID must remain {target_by_order[i].uid}')
        if not event_name:raise ScheduleError(f'slot {i}: event name is empty')
        if not (1<=laps<=999):raise ScheduleError(f'slot {i}: laps must be 1-999')
        seen_targets.append(target_uid);normalized.append(dict(slot=i,target_uid=target_uid,event_uid=event_uid,event_name=event_name,laps=laps,source_uid=item.get('source_uid')))
    if set(seen_targets)!=target_uids or len(set(seen_targets))!=36:raise ScheduleError('target slot UIDs are not the current unique 36-race set')

    codes=repoint.parse(raw)
    lap_idx,lap_diag=_infer_arg_index(current,before_records,codes,repoint,mapper,lambda row,rec:int(row.laps),'RaceLaps')
    name_idx,name_diag=_infer_arg_index(current,before_records,codes,repoint,mapper,lambda row,rec:str(row.event),'EventName')

    mapper_root=mapper.parse_pyc(raw);mapper_ins=list(_mapper_instructions(mapper,mapper_root.value));repoint_root=_root_repoint_code(codes,mapper_root)
    patched=bytearray(raw);allowed=set();changes=[];event_diags=[]
    for item in normalized:
        old=target_by_order[item['slot']];rec=before_records[item['target_uid']];co,args=_record_args(repoint,codes,item['target_uid'])
        for idx,value,label in ((lap_idx,item['laps'],'RaceLaps'),(name_idx,item['event_name'],'EventName')):
            if idx>=len(args):raise ScheduleError(f'slot {item["slot"]}: inferred {label} arg {idx} is missing')
            off,old_const,new_const=_patch_arg(patched,co,args[idx],value,f'slot {item["slot"]} {label}')
            allowed.update((off,off+1))
        eco,earg,ediag=_event_uid_operand(raw,mapper,repoint,codes,rec,mapper_root,mapper_ins,repoint_root)
        off,old_const,new_const=_patch_arg(patched,eco,earg,item['event_uid'],f'slot {item["slot"]} RaceEvent')
        allowed.update((off,off+1));ediag.update(slot=item['slot'],new_uid=item['event_uid']);event_diags.append(ediag)
        changes.append(dict(slot=item['slot'],target_uid=item['target_uid'],source_uid=item.get('source_uid'),
                            old_event=old.event,new_event=item['event_name'],old_track=old.track,
                            old_event_uid=old.event_uid,new_event_uid=item['event_uid'],old_laps=old.laps,new_laps=item['laps']))
    after=bytes(patched)
    if len(after)!=len(raw):raise ScheduleError('custom schedule PYC size changed')
    diff={i for i,(a,b) in enumerate(zip(raw,after)) if a!=b}
    if not diff.issubset(allowed):raise ScheduleError('unapproved custom-schedule bytes changed: '+str(sorted(diff-allowed)[:20]))
    after_rows,after_records=map_schedule(after,mapper);after_by_order={r.order:r for r in after_rows}
    for item in normalized:
        row=after_by_order[item['slot']]
        if row.uid!=item['target_uid'] or row.event!=item['event_name'] or row.event_uid!=item['event_uid'] or row.laps!=item['laps']:
            raise ScheduleError(f'slot {item["slot"]} failed custom schedule readback')
    allowed_fields={'EventName','RaceEvent','RaceLaps'}
    for uid,brec in before_records.items():
        arec=after_records.get(uid)
        if arec is None:raise ScheduleError(f'UID {uid} vanished after custom schedule patch')
        for field in set(brec.fields)|set(arec.fields):
            a=display(mapper,brec.fields.get(field));b=display(mapper,arec.fields.get(field))
            if a!=b and field not in allowed_fields:raise ScheduleError(f'collateral change UID {uid} {field}: {a} -> {b}')
    return after,changes,dict(laps=lap_diag,event_name=name_diag,event_pointer=dict(method='nested_EVENT_c',located=len(event_diags),samples=event_diags[:4]))

def apply_order(raw:bytes,desired:dict[int,int],mapper,repoint):
    current,before_records=map_schedule(raw,mapper); by={x.uid:x for x in current}
    if set(desired)!=set(by): raise ScheduleError('submitted UIDs do not match the current 36-race schedule')
    if sorted(desired.values())!=list(range(1,37)): raise ScheduleError('order must contain every number 1 through 36 exactly once')
    slot_dates={x.order:x.date for x in current}; codes=repoint.parse(raw); patched=bytearray(raw); allowed=set(); changes=[]
    for uid,new_order in sorted(desired.items(),key=lambda kv:kv[1]):
        old=by[uid]; new_date=slot_dates[new_order]
        if old.order==new_order and old.date==new_date: continue
        oc,oa=locate_record_arg(repoint,codes,uid,6); dc,da=locate_record_arg(repoint,codes,uid,7)
        if oc is not dc: raise ScheduleError(f'UID {uid} order/date are not in the same code object')
        oi=repoint.find_const_with_value(oc,int(new_order)); di=repoint.find_const_with_value(dc,int(new_date))
        if oi is None or di is None: raise ScheduleError(f'UID {uid}: target order/date constant missing; same-size repoint impossible')
        if oi>0xffff or di>0xffff: raise ScheduleError('target constant requires EXTENDED_ARG; refused')
        oo=oc.code_off+oa['arg_off']; do=dc.code_off+da['arg_off']
        struct.pack_into('<H',patched,oo,oi); struct.pack_into('<H',patched,do,di)
        allowed.update((oo,oo+1,do,do+1))
        changes.append(dict(uid=uid,track=old.track,old_order=old.order,new_order=new_order,old_date=old.date,new_date=new_date))
    after=bytes(patched)
    if len(after)!=len(raw): raise ScheduleError('PYC size changed; refused')
    diff={i for i,(a,b) in enumerate(zip(raw,after)) if a!=b}
    if not diff.issubset(allowed): raise ScheduleError('unapproved bytes changed: '+str(sorted(diff-allowed)[:10]))
    after_rows,after_records=map_schedule(after,mapper); after_by={x.uid:x for x in after_rows}
    for uid,target in desired.items():
        row=after_by[uid]
        if row.order!=target or row.date!=slot_dates[target]: raise ScheduleError(f'UID {uid} failed readback verification')
    for uid,brec in before_records.items():
        arec=after_records.get(uid)
        if arec is None: raise ScheduleError(f'UID {uid} vanished after patch')
        for field in set(brec.fields)|set(arec.fields):
            a=display(mapper,brec.fields.get(field)); b=display(mapper,arec.fields.get(field))
            if a!=b and field not in {'NumberInSeries','RaceDate'}:
                raise ScheduleError(f'collateral change UID {uid} {field}: {a} -> {b}')
    return after,changes

def load_helpers(mapper_path,repoint_path):
    key=(os.path.realpath(mapper_path),os.path.getmtime(mapper_path),
         os.path.realpath(repoint_path),os.path.getmtime(repoint_path))
    if key not in _HELPER_CACHE:
        _HELPER_CACHE.clear()
        _HELPER_CACHE[key]=(load_module(mapper_path,'n15_schedule_mapper'),
                            load_module(repoint_path,'n15_schedule_repoint'))
    return _HELPER_CACHE[key]

def extract(archive,cdfiles,pyc_name,mapper_path,repoint_path):
    mapper,repoint=load_helpers(mapper_path,repoint_path)
    raw,off,size=repoint.extract_from_archive(archive,cdfiles,pyc_name)
    if raw is None: raise ScheduleError(f'{pyc_name} not found')
    return raw,off,size,mapper,repoint
