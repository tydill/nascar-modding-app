#!/usr/bin/env python3
r"""
NASCAR 15 native PAINTSCHEME thumbnail import probe v1

Safely replaces ONLY the pixel payload of an already-existing native
PAINTSCHEME_<UID> entry in a 2DRIVERSELECTTD_*.ARC container.

No entry is added, renamed, resized, or repointed. cdfiles1.dat is never changed.
The exact original container is saved beside this script and can be restored.

Commands:
  py nascar15_thumbnail_import_probe_v1.py list
  py nascar15_thumbnail_import_probe_v1.py apply --uid 25599 --image thumbnail_test_25599.png
  py nascar15_thumbnail_import_probe_v1.py restore --uid 25599
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

VERSION = "1.0"
SCRIPT_DIR = Path(__file__).resolve().parent


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_game(explicit: str | None) -> Path:
    cands = []
    if explicit:
        cands.append(Path(explicit))
    for cfg in (SCRIPT_DIR / "config.json", Path(os.environ.get("LOCALAPPDATA", "")) / "NASCAR15ModdingApp" / "config.json"):
        try:
            if cfg.exists():
                g = json.loads(cfg.read_text(encoding="utf-8")).get("game")
                if g:
                    cands.append(Path(g))
        except Exception:
            pass
    cands += [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\NASCAR 15"),
        Path(r"D:\SteamLibrary\steamapps\common\NASCAR 15"),
        Path(r"E:\SteamLibrary\steamapps\common\NASCAR 15"),
    ]
    for p in cands:
        if (p / "data" / "ARCHIVE1.AR").exists() and (p / "data" / "cdfiles1.dat").exists():
            return p
    raise FileNotFoundError('NASCAR 15 not found. Pass --game "...\\common\\NASCAR 15"')


def game_running() -> bool:
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NASCAR15.exe"], capture_output=True, text=True, timeout=10)
        return "nascar15.exe" in (r.stdout or "").lower()
    except Exception:
        return False


def parse_cdf(path: Path):
    d = bytearray(path.read_bytes())
    if len(d) < 48 or struct.unpack_from("<I", d, 0)[0] != 0x436C6966:
        raise ValueError("cdfiles1.dat is not filC")
    hdr = struct.unpack_from("<12I", d, 0)
    count, strsz = hdr[8], hdr[10]
    base = len(d) - strsz
    def nm(off):
        if off >= strsz:
            return ""
        p = base + off
        e = d.find(b"\0", p)
        return d[p:e].decode("ascii", "replace") if e >= p else ""
    choices = []
    for start, layout, ni, si, oi in ((0x40, "A", 1, 2, 5), (0x50, "B", 3, 4, 7)):
        rows, valid, pos = [], 0, start
        for _ in range(count):
            if pos + 32 > base:
                break
            f = struct.unpack_from("<8I", d, pos)
            name = nm(f[ni])
            if name and all(32 <= ord(c) < 127 for c in name):
                valid += 1
            rows.append(dict(name=name, offset=int(f[oi]), size=int(f[si]), record_pos=pos, layout=layout))
            pos += 32
        choices.append((valid, rows))
    valid, rows = max(choices, key=lambda x: x[0])
    if valid < count * .8:
        raise ValueError("unrecognized cdfiles1.dat layout")
    return [r for r in rows if r["name"]]


def locate_name_blob(arc: bytes, recs):
    refs = [r for _, r in recs]
    ok = re.compile(rb"^[A-Za-z0-9_\-. ]{2,64}$")
    tail = max(0, len(arc) - max(refs, default=0) - 8192)
    candidates = []
    for pat in (rb"[A-Z0-9][A-Za-z0-9_]{3,}", rb"[A-Za-z0-9][A-Za-z0-9_\-. ]{3,}"):
        candidates += [tail + m.start() for m in re.finditer(pat, arc[tail:])]
    for start in candidates:
        blob = start - min(refs, default=0)
        if blob < 0:
            continue
        if all((lambda p: p < len(arc) and (lambda e: e > p and bool(ok.match(arc[p:e])))(arc.find(b"\0", p)))(blob + ref) for ref in refs):
            return blob
    raise ValueError("could not locate container name blob")


def resolve_dims(w, h, dsz, bpb):
    candidates = {(max(1,w)*sx, max(1,h)*sy) for sx in (1,2,4) for sy in (1,2,4)}
    matches = [(a,b) for a,b in candidates if max(1,a//4)*max(1,b//4)*bpb == dsz]
    if not matches:
        return w,h
    aspect = (w/h) if h else 1.0
    matches.sort(key=lambda q: abs((q[0]/q[1])-aspect))
    return matches[0]


def parse_multi(arc: bytes):
    if len(arc) < 0xA0 or arc[:4] != b"ARCC":
        raise ValueError("not an ARCC multi-texture container")
    count = struct.unpack_from("<I", arc, 8)[0]
    if not 0 < count < 1000:
        raise ValueError(f"invalid entry count {count}")
    recs=[]; pos=0x80
    for _ in range(count):
        r2=struct.unpack_from("<4I",arc,pos+16)
        recs.append((int(r2[1]),int(r2[2])))
        pos += 32
    base=pos; blob=locate_name_blob(arc,recs)
    def name(ref):
        p=blob+ref; e=arc.find(b"\0",p)
        return arc[p:e].decode("latin1")
    offsets=sorted(o for o,_ in recs)
    out=[]
    for i,(off,ref) in enumerate(recs):
        chunk=base+off
        w1,h1=struct.unpack_from("<2H",arc,chunk+32)
        fmtb=arc[chunk+44:chunk+48]
        fmt=fmtb.decode("ascii") if fmtb in (b"DXT1",b"DXT5") else "DXT1"
        dsz=struct.unpack_from("<I",arc,chunk+48)[0]
        bpb=16 if fmt=="DXT5" else 8
        w,h=resolve_dims(w1,h1,dsz,bpb)
        need=max(1,w//4)*max(1,h//4)*bpb
        nxt=min((x for x in offsets if x>off),default=blob-base)
        avail=base+nxt-(chunk+96)
        out.append(dict(index=i,name=name(ref),w=w,h=h,fmt=fmt,needed=need,payload_abs=chunk+96,payload_size=min(need,avail)))
    return out


def _565(r,g,b):
    return ((r>>3).astype(np.uint16)<<11)|((g>>2).astype(np.uint16)<<5)|(b>>3).astype(np.uint16)

def _rgb(c):
    r=((c>>11)&31).astype(np.int32); g=((c>>5)&63).astype(np.int32); b=(c&31).astype(np.int32)
    return np.stack([(r*255)//31,(g*255)//63,(b*255)//31],-1)


def swap_dxt5(payload: bytes) -> bytes:
    whole=(len(payload)//16)*16
    if not whole:
        return payload
    b=np.frombuffer(payload[:whole],np.uint8).reshape(-1,16)
    return np.concatenate([b[:,8:],b[:,:8]],axis=1).tobytes()+payload[whole:]


def dxt5_encode(arr):
    H,W,_=arr.shape
    bl=arr.reshape(H//4,4,W//4,4,4).transpose(0,2,1,3,4).reshape(-1,16,4).astype(np.int32)
    rgb=bl[:,:,:3]; a=bl[:,:,3]
    a0=a.max(1); a1=a.min(1); flat=a0==a1
    apal=np.zeros((len(bl),8),np.int32); apal[:,0]=a0; apal[:,1]=a1
    for i in range(1,7): apal[:,1+i]=((7-i)*a0+i*a1)//7
    ai=np.abs(a[:,None,:]-apal[:,:,None]).argmin(1).astype(np.uint64); ai[flat]=0
    ab=np.zeros(len(bl),np.uint64)
    for i in range(16): ab |= ai[:,i] << np.uint64(3*i)
    mx=rgb.max(1); mn=rgb.min(1)
    c0=_565(mx[:,0],mx[:,1],mx[:,2]); c1=_565(mn[:,0],mn[:,1],mn[:,2])
    sw=c0<c1; c0,c1=np.where(sw,c1,c0),np.where(sw,c0,c1)
    p0=_rgb(c0); p1=_rgb(c1); pal=np.stack([p0,p1,(2*p0+p1)//3,(p0+2*p1)//3],1)
    ci=((rgb[:,None,:,:]-pal[:,:,None,:])**2).sum(-1).argmin(1).astype(np.uint32)
    cb=np.zeros(len(bl),np.uint32)
    for i in range(16): cb |= ci[:,i] << (2*i)
    out=np.zeros((len(bl),16),np.uint8); out[:,0]=a0; out[:,1]=a1
    for i in range(6): out[:,2+i]=((ab>>np.uint64(8*i))&255).astype(np.uint8)
    out[:,8],out[:,9]=c0&255,c0>>8; out[:,10],out[:,11]=c1&255,c1>>8
    for i in range(4): out[:,12+i]=(cb>>(8*i))&255
    return out.tobytes()


def dxt5_decode(payload,W,H):
    N=(W//4)*(H//4); b=np.frombuffer(payload[:N*16],np.uint8).reshape(N,16)
    a0=b[:,0].astype(np.int32); a1=b[:,1].astype(np.int32)
    ab=np.zeros(N,np.uint64)
    for i in range(6): ab|=b[:,2+i].astype(np.uint64)<<(8*i)
    ap=np.zeros((N,8),np.int32); ap[:,0]=a0; ap[:,1]=a1; g=a0>a1
    for i in range(1,7): ap[:,1+i]=np.where(g,((7-i)*a0+i*a1)//7,0)
    ng=~g
    for i in range(1,5): ap[ng,1+i]=((5-i)*a0[ng]+i*a1[ng])//5
    ap[ng,6]=0; ap[ng,7]=255
    ai=np.stack([((ab>>(3*i))&7).astype(np.int64) for i in range(16)],1)
    alpha=np.take_along_axis(ap,ai,1)
    cb=b[:,8:]; c0=cb[:,0].astype(np.uint16)|(cb[:,1].astype(np.uint16)<<8); c1=cb[:,2].astype(np.uint16)|(cb[:,3].astype(np.uint16)<<8)
    bits=sum(cb[:,4+i].astype(np.uint32)<<(8*i) for i in range(4)); p0=_rgb(c0); p1=_rgb(c1)
    pal=np.stack([p0,p1,(2*p0+p1)//3,(p0+2*p1)//3],1).astype(np.uint8)
    idx=np.stack([(bits>>(2*i))&3 for i in range(16)],1)
    rgb=np.take_along_axis(pal,idx[:,:,None].astype(np.int64),1).reshape(H//4,W//4,4,4,3).transpose(0,2,1,3,4).reshape(H,W,3)
    al=alpha.reshape(H//4,W//4,4,4).transpose(0,2,1,3).reshape(H,W).astype(np.uint8)
    return np.dstack([rgb,al[:,:,None]])


def texconv_encode(image: Image.Image, fmt="DXT5"):
    tx=SCRIPT_DIR/"texconv.exe"
    if os.name!="nt" or not tx.exists():
        return None
    with tempfile.TemporaryDirectory() as td:
        src=Path(td)/"thumb.png"; image.save(src)
        r=subprocess.run([str(tx),'-y','-ft','dds','-dx9','-f',fmt,'-m','1','-o',td,str(src)],capture_output=True)
        dds=Path(td)/"thumb.dds"
        if r.returncode==0 and dds.exists():
            return dds.read_bytes()[128:]
    return None


def encode_target(image: Image.Image, entry):
    im=image.convert("RGBA").resize((entry["w"],entry["h"]),Image.Resampling.LANCZOS if hasattr(Image,"Resampling") else Image.LANCZOS)
    if entry["fmt"]!="DXT5":
        raise ValueError(f'probe only supports DXT5 PAINTSCHEME entries, got {entry["fmt"]}')
    enc=texconv_encode(im,"DXT5")
    encoder="texconv DXT5"
    if enc is None:
        enc=dxt5_encode(np.asarray(im)); encoder="built-in DXT5"
    return swap_dxt5(enc),encoder


def replace_payload(arc: bytes, entry, image: Image.Image):
    enc,encoder=encode_target(image,entry)
    pa,ps=entry["payload_abs"],entry["payload_size"]
    original=arc[pa:pa+ps]; whole=(ps//16)*16
    final=(enc[:whole]+original[whole:ps])
    if len(final)<ps: final=(final+original)[:ps]
    out=bytearray(arc); out[pa:pa+ps]=final; out=bytes(out)
    if len(out)!=len(arc): raise ValueError("container size changed")
    before={e["name"]:e for e in parse_multi(arc)}; after={e["name"]:e for e in parse_multi(out)}
    if before.keys()!=after.keys(): raise ValueError("container entry names changed")
    for name,e1 in before.items():
        e2=after[name]
        if name==entry["name"]: continue
        if arc[e1["payload_abs"]:e1["payload_abs"]+e1["payload_size"]] != out[e2["payload_abs"]:e2["payload_abs"]+e2["payload_size"]]:
            raise ValueError(f"write touched neighboring entry {name}")
    t=after[entry["name"]]; pay=out[t["payload_abs"]:t["payload_abs"]+t["payload_size"]]
    if len(pay)<t["needed"]: pay+=b"\0"*(t["needed"]-len(pay))
    dxt5_decode(swap_dxt5(pay),t["w"],t["h"])
    return out,encoder


def find_target(game: Path, uid: int):
    archive=game/"data"/"ARCHIVE1.AR"; rows=parse_cdf(game/"data"/"cdfiles1.dat")
    target=f"PAINTSCHEME_{uid}"
    for row in rows:
        if not row["name"].upper().startswith("2DRIVERSELECTTD_"):
            continue
        with archive.open("rb") as f:
            f.seek(row["offset"]); arc=f.read(row["size"])
        if len(arc)!=row["size"]: continue
        try: entries=parse_multi(arc)
        except Exception: continue
        hit=next((e for e in entries if e["name"]==target),None)
        if hit: return archive,row,arc,hit
    return None


def make_diag(uid: int, path: Path):
    im=Image.new("RGBA",(256,256),(15,18,24,255)); d=ImageDraw.Draw(im)
    cols=[(230,45,45,255),(35,155,230,255),(245,190,35,255),(55,190,95,255)]
    d.rectangle((0,0,127,127),fill=cols[0]);d.rectangle((128,0,255,127),fill=cols[1]);d.rectangle((0,128,127,255),fill=cols[2]);d.rectangle((128,128,255,255),fill=cols[3])
    d.rectangle((12,76,244,180),fill=(0,0,0,215),outline=(255,255,255,255),width=4)
    font=ImageFont.load_default()
    lines=["CUSTOM THUMB",str(uid),"IMPORT TEST"]
    ys=[86,116,150]
    for text,y in zip(lines,ys):
        box=d.textbbox((0,0),text,font=font); x=(256-(box[2]-box[0]))//2
        d.text((x,y),text,font=font,fill=(255,255,255,255))
    im.save(path)


def files(uid):
    return SCRIPT_DIR/f"thumbnail_import_probe_{uid}.manifest.json", SCRIPT_DIR/f"thumbnail_import_probe_{uid}.container.bak"


def cmd_list(args):
    game=detect_game(args.game); archive=game/"data"/"ARCHIVE1.AR"; rows=parse_cdf(game/"data"/"cdfiles1.dat")
    found=[]
    for row in rows:
        if not row["name"].upper().startswith("2DRIVERSELECTTD_"): continue
        with archive.open("rb") as f: f.seek(row["offset"]); arc=f.read(row["size"])
        try: entries=parse_multi(arc)
        except Exception: continue
        for e in entries:
            if e["name"].startswith("PAINTSCHEME_"):
                found.append((int(e["name"].split("_")[-1]),row["name"],e["w"],e["h"],e["fmt"],e["payload_size"]))
    for x in sorted(found): print(f"UID {x[0]:5d}  {x[1]:30s} {x[2]}x{x[3]} {x[4]} slot={x[5]}")
    print(f"\n{len(found)} native PAINTSCHEME entries")


def cmd_apply(args):
    if game_running(): raise RuntimeError("NASCAR15.exe is running; close it first")
    game=detect_game(args.game); hit=find_target(game,args.uid)
    if not hit: raise ValueError(f"PAINTSCHEME_{args.uid} was not found in any native driver-select container")
    archive,row,arc,entry=hit; manifest,backup=files(args.uid)
    if manifest.exists():
        old=json.loads(manifest.read_text(encoding="utf-8"))
        if old.get("applied") and not old.get("restored"): raise RuntimeError("this thumbnail probe is already active; restore it first")
    image_path=Path(args.image) if args.image else SCRIPT_DIR/f"thumbnail_test_{args.uid}.png"
    if not image_path.exists(): make_diag(args.uid,image_path)
    image=Image.open(image_path); image.load()
    rebuilt,encoder=replace_payload(arc,entry,image)
    backup.write_bytes(arc)
    before=sha(arc); after=sha(rebuilt)
    with archive.open("r+b") as f:
        f.seek(row["offset"]); f.write(rebuilt); f.flush(); os.fsync(f.fileno()); f.seek(row["offset"]); back=f.read(row["size"])
    if back!=rebuilt:
        with archive.open("r+b") as f: f.seek(row["offset"]); f.write(arc)
        raise ValueError("readback mismatch; original container restored")
    m=dict(version=VERSION,uid=args.uid,game=str(game),archive=str(archive),container=row["name"],offset=row["offset"],size=row["size"],entry=entry["name"],image=str(image_path),encoder=encoder,before_sha256=before,after_sha256=after,applied=True,restored=False)
    manifest.write_text(json.dumps(m,indent=2),encoding="utf-8")
    print("THUMBNAIL IMPORT PROBE OK")
    print(f"Container: {row['name']}")
    print(f"Entry: {entry['name']}  {entry['w']}x{entry['h']} {entry['fmt']}")
    print(f"Encoder: {encoder}")
    print("Only the target entry's complete DXT5 blocks changed; container size and all neighbors were preserved.")
    print("Open Paint Select and check the imported thumbnail. Do not test another thumbnail until this one is confirmed.")


def cmd_restore(args):
    if game_running(): raise RuntimeError("NASCAR15.exe is running; close it first")
    manifest,backup=files(args.uid)
    if not manifest.exists() or not backup.exists(): raise FileNotFoundError("probe manifest/backup not found")
    m=json.loads(manifest.read_text(encoding="utf-8")); archive=Path(m["archive"]); original=backup.read_bytes()
    with archive.open("rb") as f: f.seek(m["offset"]); current=f.read(m["size"])
    if sha(current)!=m["after_sha256"] and current!=original:
        raise RuntimeError("live container changed after this probe; restore refused to avoid overwriting newer edits")
    with archive.open("r+b") as f: f.seek(m["offset"]); f.write(original); f.flush(); os.fsync(f.fileno())
    m["restored"]=True; manifest.write_text(json.dumps(m,indent=2),encoding="utf-8")
    print("THUMBNAIL IMPORT PROBE RESTORED")


def main():
    ap=argparse.ArgumentParser(description="Import a PNG into an existing native PAINTSCHEME thumbnail slot")
    ap.add_argument("command",choices=["list","apply","restore"])
    ap.add_argument("--uid",type=int,default=25599)
    ap.add_argument("--image")
    ap.add_argument("--game")
    args=ap.parse_args()
    try:
        return {"list":cmd_list,"apply":cmd_apply,"restore":cmd_restore}[args.command](args) or 0
    except Exception as e:
        print(f"ERROR: {e}")
        return 1

if __name__=="__main__": raise SystemExit(main())
