#!/usr/bin/env python3
# containers.py - v0.4 support: DXT5, multi-texture ARCs, LDA rebuild, thumbnails
import re, struct, zlib
import numpy as np
from PIL import Image

# ---------- DXT5 ----------
def _565(r,g,b):
    return ((r>>3).astype(np.uint16)<<11)|((g>>2).astype(np.uint16)<<5)|(b>>3).astype(np.uint16)
def _rgb(c):
    r=((c>>11)&31).astype(np.int32); g=((c>>5)&63).astype(np.int32); b=(c&31).astype(np.int32)
    return np.stack([(r*255)//31,(g*255)//63,(b*255)//31],-1)

def swap_dxt5_halves(payload):
    """NASCAR 15 stores DXT5 blocks as [color 8B][alpha 8B] (console-order).
    Swap to/from standard layout. Involution: applying twice is identity."""
    b=np.frombuffer(payload[:len(payload)//16*16],np.uint8).reshape(-1,16)
    out=np.concatenate([b[:,8:],b[:,:8]],axis=1).tobytes()
    return out+payload[len(out):]

def dxt5_decode(payload,W,H):
    N=(W//4)*(H//4)
    b=np.frombuffer(payload[:N*16],np.uint8).reshape(N,16)
    a0=b[:,0].astype(np.int32); a1=b[:,1].astype(np.int32)
    abits=np.zeros(N,dtype=np.uint64)
    for i in range(6): abits |= b[:,2+i].astype(np.uint64)<<(8*i)
    apal=np.zeros((N,8),np.int32); apal[:,0]=a0; apal[:,1]=a1
    g=a0>a1
    for i in range(1,7): apal[:,1+i]=np.where(g,((7-i)*a0+i*a1)//7,0)
    ng=~g
    for i in range(1,5): apal[ng,1+i]=((5-i)*a0[ng]+i*a1[ng])//5
    apal[ng,6]=0; apal[ng,7]=255
    aidx=np.stack([((abits>>(3*i))&7).astype(np.int64) for i in range(16)],1)
    alpha=np.take_along_axis(apal,aidx,1)
    cb=b[:,8:]
    c0=cb[:,0].astype(np.uint16)|(cb[:,1].astype(np.uint16)<<8)
    c1=cb[:,2].astype(np.uint16)|(cb[:,3].astype(np.uint16)<<8)
    bits=sum(cb[:,4+i].astype(np.uint32)<<(8*i) for i in range(4))
    p0=_rgb(c0);p1=_rgb(c1)
    pal=np.stack([p0,p1,(2*p0+p1)//3,(p0+2*p1)//3],1).astype(np.uint8)
    idx=np.stack([(bits>>(2*i))&3 for i in range(16)],1)
    px=np.take_along_axis(pal,idx[:,:,None].astype(np.int64),1)
    rgb=px.reshape(H//4,W//4,4,4,3).transpose(0,2,1,3,4).reshape(H,W,3)
    al=alpha.reshape(H//4,W//4,4,4).transpose(0,2,1,3).reshape(H,W).astype(np.uint8)
    return np.dstack([rgb,al[:,:,None]])

def dxt5_encode(img_rgba):
    """img: HxWx4 uint8. Color endpoints via min/max axis; alpha 8-level."""
    H,W,_=img_rgba.shape
    bl=img_rgba.reshape(H//4,4,W//4,4,4).transpose(0,2,1,3,4).reshape(-1,16,4).astype(np.int32)
    rgb=bl[:,:,:3]; a=bl[:,:,3]
    # alpha block: a0=max,a1=min (7-interp mode)
    amax=a.max(1); amin=a.min(1)
    a0=amax; a1=amin
    flat=a0==a1
    apal=np.zeros((len(bl),8),np.int32)
    apal[:,0]=a0; apal[:,1]=a1
    for i in range(1,7): apal[:,1+i]=((7-i)*a0+i*a1)//7
    d=np.abs(a[:,None,:]-apal[:,:,None])
    aidx=d.argmin(1).astype(np.uint64)
    aidx[flat]=0
    abits=np.zeros(len(bl),np.uint64)
    for i in range(16): abits |= aidx[:,i]<<np.uint64(3*i)
    # color block (4-color mode)
    mx=rgb.max(1); mn=rgb.min(1)
    c0=_565(mx[:,0],mx[:,1],mx[:,2]); c1=_565(mn[:,0],mn[:,1],mn[:,2])
    sw=c0<c1
    c0,c1=np.where(sw,c1,c0),np.where(sw,c0,c1)
    p0=_rgb(c0); p1=_rgb(c1)
    pal=np.stack([p0,p1,(2*p0+p1)//3,(p0+2*p1)//3],1)
    cd=((rgb[:,None,:,:]-pal[:,:,None,:])**2).sum(-1)
    cidx=cd.argmin(1).astype(np.uint32)
    cbits=np.zeros(len(bl),np.uint32)
    for i in range(16): cbits |= cidx[:,i]<<(2*i)
    out=np.zeros((len(bl),16),np.uint8)
    out[:,0]=a0; out[:,1]=a1
    for i in range(6): out[:,2+i]=((abits>>np.uint64(8*i))&np.uint64(0xFF)).astype(np.uint8)
    out[:,8],out[:,9]=c0&0xFF,c0>>8
    out[:,10],out[:,11]=c1&0xFF,c1>>8
    for i in range(4): out[:,12+i]=(cbits>>(8*i))&0xFF
    return out.tobytes()


# ---------- dimension resolution (v0.6 fix: number cards were cut off) ----------
# Some containers report unreliable meta dims; a datasize can match more than one
# W/H (e.g. 4096 bytes DXT1 == 128x64 AND 64x128). We resolve by: (a) a known-dims
# override per container family when we are certain of the real shape, else
# (b) inference that preserves the meta's aspect ratio (keeps square logos square).
import math as _math

# Container name substrings whose textures we KNOW are 128x64 landscape number
# cards. Passed in by the caller; matched case-insensitively.
def _legacy_lin(w,h,bpb): return max(1,w//4)*max(1,h//4)*bpb

def _legacy_resolve_dims(w1,h1,dsz,bpb,known=None):
    """known=(W,H): if given and its byte size matches dsz, use it outright.
    Otherwise infer from the meta aspect ratio."""
    if known is not None and _legacy_lin(known[0],known[1],bpb)==dsz:
        return known
    meta_aspect=(w1/h1) if h1 else 1.0
    cand=set()
    for sw in (1,2,4):
        for sh in (1,2,4):
            cand.add((max(1,w1)*sw, max(1,h1)*sh))
    matches=[(cw,ch) for (cw,ch) in cand if _legacy_lin(cw,ch,bpb)==dsz]
    if not matches:
        return (w1,h1)
    matches.sort(key=lambda c:(abs(_math.log((c[0]/c[1]) or 1)-_math.log(meta_aspect or 1)),
                               0 if c[0]>=c[1] else 1))
    return matches[0]

# ---------- multi-texture ARCC (SPRINTNUMS / thumbnails / logos) ----------
#
# NASCAR 15's normal ARCC texture banks use a 16-byte physical record table:
#   header +0x04 = physical record count
#   header +0x08 = logical texture count
#   table starts at 0x80
#   type 0x01 records point to a 24-byte texture header followed by the BC data
#
# Older app builds paired two physical records into one synthetic 32-byte row,
# then treated the payload as chunk+96 and swapped the halves of every DXT5
# block.  On the clean game that starts every image 40 bytes late.  The two
# mistakes partly cancel visually, which is why an export->import round trip
# could appear to work while externally-authored templates were shifted.
#
# Keep a legacy fallback for uncommon banks, but all normal NASCAR 15 UI/paint
# resources use the primary 16-byte layout below.

def _bc_size(w,h,bpb):
    return max(1,(int(w)+3)//4)*max(1,(int(h)+3)//4)*bpb

def _primary_resolve_dims(w1,h1,dsz,bpb,known=None):
    if known is not None and _bc_size(known[0],known[1],bpb) <= int(dsz):
        return int(known[0]),int(known[1])
    return int(w1),int(h1)

def _record_type_size(packed):
    raw=int(packed).to_bytes(4,'little')
    return raw[0],int.from_bytes(raw[1:4],'big')

def _collect_tail_strings(blob,tail_start):
    out=[];seen=set()
    tail=blob[tail_start:]
    for rx in (re.compile(rb'[A-Za-z0-9_./\\\- ]{2,120}\x00'),
               re.compile(rb'[A-Za-z0-9_\-. ]{2,120}\x00')):
        for m in rx.finditer(tail):
            raw=m.group()[:-1]
            try:text=raw.decode('latin1')
            except Exception:continue
            key=(tail_start+m.start(),text)
            if key not in seen:seen.add(key);out.append(key)
    return sorted(out)

def _name_area_from_directory(records,table_end,blob_len):
    """Locate the resource-name area deterministically.

    FIX (v1.0.2-dev6): every NASCAR 15 ARCC container ends with exactly one
    type-0xFD record, and that record's payload IS the name area.  A texture
    record's ``name_ref`` is a plain byte offset into it.  Verified against a
    full archive map: 2668/2668 containers that carry named records satisfy
    ``0 <= name_ref < fd_size``, with zero counterexamples.

    dev5 instead scanned up to 2.5 MB of the container for anything that looked
    like a string, then guessed a base by CRC-matching record keys.  On ~1.4%
    of containers that guess landed a few bytes off and produced names like
    '1JJJJ', 'TTT' and 'ce1' -- and on the rest it was simply a slow way to
    rediscover an offset the directory already states outright.

    Returns (name_base, name_area_end) or (None, None).
    """
    fd=[r for r in records if r['type']==0xFD]
    if len(fd)!=1:return None,None
    rec=fd[0]
    base=table_end+int(rec['data_off'])
    end=base+int(rec['size'])
    if base<table_end or end>blob_len or end<=base:return None,None
    return base,end


def _name_at(arc,name_base,name_area_end,name_ref):
    """Read one NUL-terminated resource name out of the name area."""
    if name_base is None:return ''
    nr=int(name_ref)
    if nr==0xffffffff:return ''
    p=name_base+nr
    if not (name_base<=p<name_area_end):return ''
    e=arc.find(b'\0',p,name_area_end)
    if e<=p:return ''
    try:text=arc[p:e].decode('latin1')
    except Exception:return ''
    return text if re.fullmatch(r'[A-Za-z0-9_./\\\- ]{1,120}',text) else ''


def _derive_name_base(records,strings,blob_len,table_end):
    by_crc={}
    for pos,text in strings:
        by_crc.setdefault(zlib.crc32(text.lower().encode('latin1'))&0xffffffff,[]).append((pos,text))
    candidates={}
    for rec in records:
        nr=rec['name_ref']
        if nr==0xffffffff:continue
        for pos,_ in by_crc.get(rec['key'],[]):
            base=pos-nr
            if table_end<=base<blob_len:candidates[base]=candidates.get(base,0)+10
    refs=[r['name_ref'] for r in records if r['name_ref'] not in (0xffffffff,0)]
    for nr in refs[:100]:
        for pos,_ in strings[:500]:
            base=pos-nr
            if table_end<=base<blob_len:candidates.setdefault(base,0)
    by_pos={p:t for p,t in strings}
    best=None;score_best=-1
    for base,bonus in candidates.items():
        score=bonus
        for rec in records:
            nr=rec['name_ref']
            if nr!=0xffffffff and base+nr in by_pos:score+=1
        if score>score_best:best,score_best=base,score
    return best if score_best>=2 else None

def _primary_name(rec,name_base,string_by_pos,crc_names,arc):
    nr=rec['name_ref'];name=''
    if name_base is not None and nr!=0xffffffff:
        name=string_by_pos.get(name_base+nr,'')
        if not name and 0<=name_base+nr<len(arc):
            p=name_base+nr;e=arc.find(b'\0',p,min(len(arc),p+160))
            if e>p:
                try:
                    c=arc[p:e].decode('latin1')
                    if re.fullmatch(r'[A-Za-z0-9_./\\\- ]{2,120}',c):name=c
                except Exception:pass
    if not name:
        hits=crc_names.get(rec['key'],[])
        if hits:name=hits[0]
    return name or f'texture_record_{rec["index"]:04d}'


def _legacy_logical_names(arc):
    """Recover the friendly logical names from the paired-record view only."""
    try:
        count=struct.unpack_from('<I',arc,8)[0]
        if count<=0 or count>10000:return []
        refs=[];pos=0x80
        for _ in range(count):
            if pos+32>len(arc):return []
            r2=struct.unpack_from('<4I',arc,pos+16);refs.append(int(r2[2]));pos+=32
        table_end=pos
        ok_re=re.compile(rb'^[A-Za-z0-9_\-. ]{2,64}$')
        tail_start=max(table_end,len(arc)-max(refs,default=0)-8192)
        cands=[]
        for pat in (rb'[A-Z0-9][A-Za-z0-9_]{3,}',rb'[A-Za-z0-9][A-Za-z0-9_\-. ]{3,}'):
            cands += [tail_start+m.start() for m in re.finditer(pat,arc[tail_start:])]
        for cs in cands:
            base=cs-min(refs,default=0)
            if base<table_end:continue
            names=[]
            for ref in refs:
                q=base+ref;e=arc.find(b'\0',q,min(len(arc),q+160))
                if e<=q or not ok_re.match(arc[q:e]):break
                names.append(arc[q:e].decode('latin1'))
            if len(names)==len(refs):return names
    except Exception:pass
    return []

def _parse_multi_arc_primary(arc,known_dims=None):
    if len(arc)<0x98 or arc[:4]!=b'ARCC':raise ValueError('not ARCC')
    physical_count=struct.unpack_from('<I',arc,4)[0]
    logical_hint=struct.unpack_from('<I',arc,8)[0]
    if physical_count<=0 or physical_count>100000:raise ValueError('invalid physical record count')
    table_end=0x80+physical_count*16
    if table_end>len(arc):raise ValueError('physical record table exceeds container')
    records=[]
    for i in range(physical_count):
        key,data_off,name_ref,packed=struct.unpack_from('<4I',arc,0x80+i*16)
        typ,size=_record_type_size(packed)
        records.append(dict(index=i,key=int(key),data_off=int(data_off),name_ref=int(name_ref),
                            type=int(typ),size=int(size),record_pos=0x80+i*16,
                            record=bytes(arc[0x80+i*16:0x90+i*16])))
    textures=[r for r in records if r['type']==0x01 and r['size']>=24]
    if not textures:raise ValueError('no primary texture records')
    # FIX (v1.0.2-dev6): ask the directory where the names are before guessing.
    name_base,name_area_end=_name_area_from_directory(records,table_end,len(arc))
    string_by_pos={};crc_names={};legacy_names=[]
    if name_base is None:
        # No single 0xFD record: fall back to dev5's heuristic search so odd
        # containers still parse, just without the deterministic guarantee.
        tail_start=max(table_end,len(arc)-min(len(arc),2500000))
        strings=_collect_tail_strings(arc,tail_start)
        name_base=_derive_name_base(textures,strings,len(arc),table_end)
        name_area_end=len(arc)
        string_by_pos={p:t for p,t in strings}
        for _,text in strings:
            crc_names.setdefault(zlib.crc32(text.lower().encode('latin1'))&0xffffffff,[]).append(text)
        legacy_names=_legacy_logical_names(arc)
    used=set();entries=[];resolved_names=0
    for logical_index,rec in enumerate(textures):
        a=table_end+rec['data_off']
        if a<table_end or a+24>len(arc) or a+rec['size']>len(arc):continue
        w1,h1,w2,h2=struct.unpack_from('<4H',arc,a)
        mip_count=int(arc[a+8])
        fmt_raw=arc[a+12:a+16];data_size=struct.unpack_from('<I',arc,a+16)[0]
        dxt5_swapped=False
        if fmt_raw in (b'DXT1',b'DXT5'):
            fmt=fmt_raw.decode('ascii');w,h=_primary_resolve_dims(w1,h1,data_size,8 if fmt=='DXT1' else 16,known_dims)
        else:
            code=struct.unpack_from('<I',fmt_raw)[0]
            if code==0x19:
                fmt='DXT1'
                if known_dims is not None:w,h=map(int,known_dims)
                else:w,h=int(w1)*4,int(h1)*4
            elif code==0x15:
                fmt='A8R8G8B8';w,h=int(w1),int(h1)
            else:
                continue
        payload_abs=a+24
        capacity=max(0,min(rec['size']-24,len(arc)-payload_abs))
        payload_size=min(int(data_size),capacity)
        if payload_size<=0:continue
        name=_name_at(arc,name_base,name_area_end,rec['name_ref']) if name_area_end else ''
        if not name:
            name=_primary_name(rec,name_base,string_by_pos,crc_names,arc)
            if name.startswith('texture_record_') and logical_index<len(legacy_names):name=legacy_names[logical_index]
        if not name.startswith('texture_record_'):resolved_names+=1
        if name in used:name=f'{name}__r{rec["index"]}'
        used.add(name)
        bpb=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else 4
        needed=_bc_size(w,h,bpb) if fmt in ('DXT1','DXT5') else int(w)*int(h)*4
        # The preceding 0xFF identity row plus this 0x01 row are the logical
        # 32-byte table record used by the historical template builders.
        # FIX (v1.0.2-dev9): the logical 32-byte record is the 0xFF identity row
        # immediately followed by this 0x01 texture row.  dev5 located that row by
        # matching name_ref, but an ALIAS legitimately carries a different
        # name_ref -- that is what makes it an alias.  The clean game contains 679
        # such pairs.  When the match failed, dev5 zero-filled the first half, so
        # word 2 read back as 0, _identity_entry resolved nothing, and
        # _paint_identity_chain reported "identity reference does not resolve in
        # this bank" for every aliased PAINTSCHEME.  Pair by adjacency instead.
        prev=records[rec['index']-1] if rec['index']>0 else None
        ident=prev if (prev is not None and prev['type']==0xff
                       and prev['record_pos']+16==rec['record_pos']) else None
        pair_start=ident['record_pos'] if ident is not None else rec['record_pos']
        pair=bytes(arc[pair_start:rec['record_pos']+16])
        if len(pair)==16:pair=b'\0'*16+pair;pair_start=rec['record_pos']-16
        entries.append(dict(index=logical_index,name=name,data_off=rec['data_off'],name_ref=rec['name_ref'],
            w=w,h=h,fmt=fmt,needed=needed,payload_abs=payload_abs,payload_size=payload_size,
            mip_count=mip_count,data_size=int(data_size),
            header_abs=a,chunk_start=a,chunk_end=a+rec['size'],record_size=rec['size'],
            physical_record_index=rec['index'],table_start=pair_start,table_record=pair,
            dxt5_swapped=dxt5_swapped,layout='primary16',name_blob=name_base,
            name_area_end=name_area_end,
            physical_count=physical_count,logical_count=logical_hint))
    if not entries:raise ValueError('primary table contained no supported textures')
    # FIX (v1.0.2-dev6): dev5 returned success even when NOT ONE resource name
    # could be recovered, handing callers entries called 'texture_record_0001'.
    # Every by-name lookup then failed, which is what produced "required
    # driver-select resources are missing: DRIVERPAINT_..." on team transfers
    # and made a modded game look clean.  A bank we cannot name at all is a bank
    # we did not understand, so raise and let the caller say so honestly.
    # Partial resolution is still returned: some containers legitimately mix
    # named and unnamed resources, and dropping those would lose real function.
    if resolved_names==0:
        raise ValueError('primary table parsed but no resource name could be resolved '
                         '(%d textures, name area %s)'%(len(entries),
                          'absent' if name_base is None else 'unreadable'))
    for e in entries:e['names_resolved']=resolved_names
    return entries,table_end

def _parse_multi_arc_legacy(arc,known_dims=None):
    count=struct.unpack_from('<I',arc,8)[0]
    recs=[];pos=0x80
    for i in range(count):
        if pos+32>len(arc):raise ValueError('legacy table exceeds container')
        r2=struct.unpack_from('<4I',arc,pos+16)
        recs.append((r2[1],r2[2],pos,bytes(arc[pos:pos+32])))
        pos+=32
    base=pos
    refs=[r for _,r,_,_ in recs]
    ok_re=re.compile(rb'^[A-Za-z0-9_\-. ]{2,48}$')
    tail_start=max(0,len(arc)-max(refs,default=0)-4096)
    blob=None;cands=[]
    for pat in (rb'[A-Z0-9][A-Za-z0-9_]{3,}',rb'[A-Za-z0-9][A-Za-z0-9_\-. ]{3,}'):
        cands += [tail_start+m.start() for m in re.finditer(pat,arc[tail_start:])]
    for cstart in cands:
        cand=cstart-min(refs,default=0)
        if cand<0:continue
        good=0
        for ref in refs:
            p=cand+ref;e=arc.find(b'\0',p)
            if e>p and ok_re.match(arc[p:e]):good+=1
            else:break
        if good==len(refs):blob=cand;break
    if blob is None:raise ValueError('could not locate legacy name blob')
    def name_at(ref):
        p=blob+ref;e=arc.find(b'\0',p);return arc[p:e].decode('latin1')
    offs=sorted(o for o,_,_,_ in recs);entries=[]
    for i,(off,ref,tpos,trec) in enumerate(recs):
        chunk=base+off
        w1,h1,w2,h2=struct.unpack_from('<4H',arc,chunk+32)
        mip_count=int(arc[chunk+40])
        fmtb=arc[chunk+44:chunk+48]
        dsz=struct.unpack_from('<I',arc,chunk+48)[0]
        fmt=fmtb.decode() if fmtb in (b'DXT1',b'DXT5') else 'DXT1'
        bpb=8 if fmt=='DXT1' else 16
        w,h=_legacy_resolve_dims(w1,h1,dsz,bpb,known_dims)
        needed=_bc_size(w,h,bpb)
        nxt=min([o for o in offs if o>off],default=blob-base)
        avail=base+nxt-(chunk+96)
        entries.append(dict(index=i,name=name_at(ref),data_off=off,name_ref=ref,w=w,h=h,fmt=fmt,
            needed=needed,payload_abs=chunk+96,payload_size=min(needed,max(0,avail)),
            mip_count=mip_count,data_size=int(dsz),
            header_abs=chunk+32,chunk_start=chunk,chunk_end=base+nxt,record_size=base+nxt-chunk,
            table_start=tpos,table_record=trec,dxt5_swapped=True,layout='legacy32',name_blob=blob,
            physical_count=count*2,logical_count=count))
    return entries,base

def _score_parse(arc,entries):
    """Rate how well a candidate interpretation actually explains the bytes.

    FIX (v1.0.2-dev6): dev5 kept whichever reader did not raise first, so a
    bank the primary reader merely *survived* beat the reader that genuinely
    understood it.  Score both instead.  A negative score means structurally
    impossible, never merely worse.

    Note deliberately absent from the scoring: payload_size == needed.  Most
    stock textures carry a mip chain, so the stored size legitimately exceeds
    the base surface; rewarding an exact match would punish the correct read.
    """
    if not entries:return -1
    score=0;spans=[]
    for e in entries:
        pa=int(e['payload_abs']);ps=int(e['payload_size'])
        if ps<=0 or pa<0 or pa+ps>len(arc):return -1
        spans.append((pa,pa+ps))
        if not str(e['name']).startswith('texture_record_'):score+=5
        if e['fmt'] in ('DXT1','DXT5','A8R8G8B8'):score+=1
        if any(arc[pa:pa+min(ps,64)]):score+=1
    spans.sort()
    for (_,end),(nxt,_) in zip(spans,spans[1:]):
        if nxt<end:return -1
    return score

def parse_multi_arc(arc,known_dims=None):
    results=[];errors=[]
    for label,fn in (('primary',_parse_multi_arc_primary),('legacy',_parse_multi_arc_legacy)):
        try:
            ents,base=fn(arc,known_dims=known_dims)
            results.append((_score_parse(arc,ents),label,ents,base))
        except Exception as ex:
            errors.append(f'{label}: {ex}')
    usable=[r for r in results if r[0]>=0]
    if not usable:
        errors += [f'{lbl}: parsed but failed structural validation' for s,lbl,_,_ in results]
        raise ValueError('could not parse ARCC texture bank ('+'; '.join(errors)+')')
    usable.sort(key=lambda r:-r[0])
    return usable[0][2],usable[0][3]

def _dxt1_decode(payload,W,H):
    N=(W//4)*(H//4)
    a=np.frombuffer(payload[:N*8],np.uint8).reshape(N,8)
    c0=a[:,0].astype(np.uint16)|(a[:,1].astype(np.uint16)<<8)
    c1=a[:,2].astype(np.uint16)|(a[:,3].astype(np.uint16)<<8)
    bits=sum(a[:,4+i].astype(np.uint32)<<(8*i) for i in range(4))
    p0=_rgb(c0);p1=_rgb(c1);four=(c0>c1)[:,None]
    p2=np.where(four,(2*p0+p1)//3,(p0+p1)//2);p3=np.where(four,(p0+2*p1)//3,0)
    pal=np.stack([p0,p1,p2,p3],1).astype(np.uint8)
    idx=np.stack([(bits>>(2*i))&3 for i in range(16)],1)
    px=np.take_along_axis(pal,idx[:,:,None].astype(np.int64),1)
    return px.reshape(H//4,W//4,4,4,3).transpose(0,2,1,3,4).reshape(H,W,3)

def multi_read_png(arc,entry):
    pay=arc[entry['payload_abs']:entry['payload_abs']+entry['payload_size']]
    if len(pay)<entry['needed']:pay=pay+b'\0'*(entry['needed']-len(pay))
    if entry['fmt']=='DXT5':
        if entry.get('dxt5_swapped'):pay=swap_dxt5_halves(pay)
        return Image.fromarray(dxt5_decode(pay,entry['w'],entry['h']),'RGBA')
    if entry['fmt']=='DXT1':
        return Image.fromarray(_dxt1_decode(pay,entry['w'],entry['h'])).convert('RGBA')
    if entry['fmt']=='A8R8G8B8':
        need=entry['w']*entry['h']*4
        raw=(pay+b'\0'*need)[:need]
        a=np.frombuffer(raw,np.uint8).reshape(entry['h'],entry['w'],4)
        return Image.fromarray(a[:,:,[2,1,0,3]],'RGBA')
    raise ValueError('unsupported texture format '+str(entry['fmt']))

def multi_write_png(arc_bytes,entry,img,encode_fn=None):
    img=img.convert('RGBA').resize((entry['w'],entry['h']))
    enc=None
    if entry['fmt']=='A8R8G8B8':
        a=np.asarray(img,np.uint8);enc=a[:,:,[2,1,0,3]].tobytes()
    else:
        if encode_fn:
            try:enc=encode_fn(img,entry['fmt'])
            except TypeError:enc=encode_fn(img)
        if enc is None:enc=dxt5_encode(np.asarray(img)) if entry['fmt']=='DXT5' else None
        if enc is None:raise ValueError('no encoder supplied for '+str(entry['fmt']))
        if entry['fmt']=='DXT5' and entry.get('dxt5_swapped'):enc=swap_dxt5_halves(enc)
    ps=int(entry['payload_size']);pa=int(entry['payload_abs'])
    original_slot=bytes(arc_bytes[pa:pa+ps])
    block=16 if entry['fmt']=='DXT5' else 8 if entry['fmt']=='DXT1' else 4
    # Some mapped resources reserve non-image bytes after a shorter encoded
    # surface, so preserve any bytes the encoder does not replace. SPRINTNUMS
    # BIG_* is not one of those cases: callers pass its mapped 128x64 geometry,
    # producing the complete 4096-byte DXT1 surface.
    replace_len=min(len(enc),ps)
    replace_len-=replace_len%block
    if replace_len<=0:raise ValueError('encoder returned no complete texture blocks')
    final=enc[:replace_len]+original_slot[replace_len:]
    if len(final)!=ps:raise ValueError('encoded texture did not preserve the native payload size')
    out=bytearray(arc_bytes);out[pa:pa+ps]=final
    return bytes(out)

def multi_write_png_validated(arc_bytes,entry,img,encode_fn=None,known_dims=None):
    original_len=len(arc_bytes);new=multi_write_png(arc_bytes,entry,img,encode_fn=encode_fn)
    if len(new)!=original_len:raise ValueError(f'write changed container size {original_len}->{len(new)} (refused)')
    try:ent2,_=parse_multi_arc(new,known_dims=known_dims)
    except Exception as ex:raise ValueError(f'rewritten container no longer parses: {ex} (refused)')
    ent1,_=parse_multi_arc(arc_bytes,known_dims=known_dims);by_name1={e['name']:e for e in ent1}
    for e2 in ent2:
        if e2['name']==entry['name']:continue
        e1=by_name1.get(e2['name'])
        if e1 is None:raise ValueError(f'entry {e2["name"]} vanished after write (refused)')
        a=arc_bytes[e1['payload_abs']:e1['payload_abs']+e1['payload_size']]
        b=new[e2['payload_abs']:e2['payload_abs']+e2['payload_size']]
        if a!=b:raise ValueError(f'write bled into neighbor {e2["name"]} (refused)')
    tgt=next((e for e in ent2 if e['name']==entry['name']),None)
    if tgt is None:raise ValueError('target entry missing after write (refused)')
    try:
        test_img=multi_read_png(new,tgt)
        if test_img.size!=(entry['w'],entry['h']):raise ValueError(f'target decodes to wrong size {test_img.size} (refused)')
    except Exception as ex:raise ValueError(f'target no longer decodes: {ex} (refused)')
    return new

# ---------- LDA string tables (fully decoded) ----------
# Layout: [0x14 header][u32 offsets x count][4 bytes][string blob]
#   count at 0x10; offsets are blob-relative; entry k = null-terminated
#   string at blob+off[k]. Verified against the David Ragan anchor.
def lda_parse(blob):
    count=struct.unpack_from('<I',blob,0x10)[0]
    offs=list(struct.unpack_from('<%dI'%count, blob, 0x14))
    mid=blob[0x14+count*4:0x14+count*4+4]
    base=0x14+count*4+4
    strs=[]
    for o in offs:
        p=base+o; e=blob.find(b'\0',p)
        strs.append(blob[p:e])
    return count, base, mid, strs

def lda_rebuild(blob, replace_map):
    """Rebuild with the offset table kept consistent. replace_map:
    {old_bytes: new_bytes}. Returns (new_file_bytes, n_replaced).
    Length may differ from the original file - caller decides placement."""
    count, base, mid, strs = lda_parse(blob)
    n=0
    out_strs=[]
    for s in strs:
        if s in replace_map:
            out_strs.append(replace_map[s]); n+=1
        else:
            out_strs.append(s)
    pool=bytearray(); offs=[]
    for s in out_strs:
        offs.append(len(pool))
        pool += s + b'\0'
    head=bytearray(blob[:0x14])
    struct.pack_into('<I', head, 0x10, count)
    body=b''.join(struct.pack('<I',o) for o in offs)
    new=bytes(head)+body+mid+bytes(pool)
    # keep the size field at +4 accurate if it matched before
    old_size=struct.unpack_from('<I',blob,4)[0]
    if old_size==len(blob):
        new=bytearray(new); struct.pack_into('<I',new,4,len(new)); new=bytes(new)
    return new, n


def lda_entries(blob):
    """Return exact indexed string records from a NASCAR 15 LDA table.

    Each record contains the table index, pool-relative offset, absolute byte
    range, and raw string bytes. The returned byte range excludes the trailing
    NUL terminator. This is used by the UI Text Editor so one exact reference is
    edited rather than every duplicate string with the same text.
    """
    if len(blob) < 0x18:
        raise ValueError('LDA file is too small')
    count=struct.unpack_from('<I',blob,0x10)[0]
    if count<0 or count>2_000_000:
        raise ValueError(f'implausible LDA string count: {count}')
    table_end=0x14+count*4
    if table_end+4>len(blob):
        raise ValueError('LDA offset table exceeds the file')
    offs=list(struct.unpack_from('<%dI'%count,blob,0x14)) if count else []
    base=table_end+4
    out=[]
    for i,o in enumerate(offs):
        p=base+o
        if p<base or p>=len(blob):
            raise ValueError(f'LDA string {i} offset exceeds the file')
        e=blob.find(b'\0',p)
        if e<0:
            raise ValueError(f'LDA string {i} has no terminator')
        out.append(dict(index=i,offset=o,start=p,end=e,raw=blob[p:e]))
    return out


def lda_rebuild_indices(blob, replacements):
    """Rebuild an LDA table while changing only selected string indexes.

    replacements is ``{index: new_bytes}``. The offset table is regenerated,
    duplicate strings remain distinct records, and the string count is never
    changed. Returns ``(new_file_bytes, changed_count)``.
    """
    count, base, mid, strs=lda_parse(blob)
    clean={int(k):bytes(v) for k,v in replacements.items()}
    for i,v in clean.items():
        if i<0 or i>=count:
            raise IndexError(f'LDA string index out of range: {i}')
        if b'\0' in v:
            raise ValueError('LDA replacement contains a NUL byte')
    out_strs=list(strs);changed=0
    for i,v in clean.items():
        if out_strs[i]!=v:
            out_strs[i]=v;changed+=1
    pool=bytearray();offs=[]
    for text in out_strs:
        offs.append(len(pool));pool+=text+b'\0'
    head=bytearray(blob[:0x14])
    struct.pack_into('<I',head,0x10,count)
    body=b''.join(struct.pack('<I',o) for o in offs)
    new=bytes(head)+body+mid+bytes(pool)
    old_size=struct.unpack_from('<I',blob,4)[0] if len(blob)>=8 else 0
    if old_size==len(blob):
        new=bytearray(new);struct.pack_into('<I',new,4,len(new));new=bytes(new)
    # Parse the result before returning it. This catches overflow/truncation in
    # the generated table instead of passing malformed bytes to the installer.
    chk=lda_entries(new)
    if len(chk)!=count:
        raise ValueError('rebuilt LDA string count changed unexpectedly')
    return new,changed

# ---------- thumbnail auto-generation ----------
def make_thumb(scheme_img, size=256):
    """Stylized preview card from a 2048x1024 atlas: scaled strip on transparency."""
    img=scheme_img.convert('RGB')
    strip=img.resize((int(size*0.9), int(size*0.45)))
    card=Image.new('RGBA',(size,size),(0,0,0,0))
    card.paste(strip, ((size-strip.width)//2,(size-strip.height)//2))
    return card
