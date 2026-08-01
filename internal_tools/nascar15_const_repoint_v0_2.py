#!/usr/bin/env python3
"""
NASCAR 15 ISOLATED CONST REPOINT  v0.2

Problem: the record mapper patches the *value* of a marshalled constant. When
several RACEDATA_c records share a constant (e.g. RaceLaps=267 used by 31 rows),
editing it changes all of them.

Fix: don't touch the constant object. Find the LOAD_CONST instruction that feeds
the selected record's RaceLaps argument, and repoint its 2-byte operand to a
different const index that already holds the desired value.

Because we only rewrite an existing 2-byte operand, the PYC (and archive entry)
stays exactly the same size. If the desired value is NOT already present in that
code object's const table, we REJECT rather than fall back to a shared edit.

Read-only by default. --apply writes a same-size patched copy.

Usage:
  py nascar15_const_repoint_v0_1.py inspect --pyc DB_GAME_LOCAL_SCRIPT.PYC \
      --class RACEDATA_c --uid 25064 --field-index 4
  py nascar15_const_repoint_v0_1.py repoint --pyc IN.PYC --uid 25064 \
      --field-index 4 --new-value 300 --out OUT.PYC
"""
import argparse, struct, sys, os

# ---------------- archive plumbing (same format as the recon probes) ----------------
def parse_cdfiles(path):
    d=open(path,'rb').read()
    if d[:4]!=b'filC': raise ValueError('cdfiles.dat does not start with filC')
    count=struct.unpack_from('<I',d,0x20)[0]
    marker=d.find(b'NAS4\\LANG\\')
    if marker<0: raise ValueError('cdfiles string table marker not found')
    strbase=marker-1
    def nm(off):
        p=strbase+off
        e=d.find(b'\0',p)
        return d[p:e].decode('ascii','replace')
    out=[]
    pos=0x40
    for _ in range(count):
        f=struct.unpack_from('<8I',d,pos)
        name_off,size,arc_off=f[1],f[2],f[5]
        try: s=nm(name_off)
        except Exception: s=''
        if s: out.append((arc_off,size,s))
        pos+=32
    return out

def extract_from_archive(archive, cdfiles, wanted):
    for off,sz,nm in parse_cdfiles(cdfiles):
        if nm.upper().endswith(wanted.upper()):
            with open(archive,'rb') as f:
                f.seek(off); return f.read(sz), off, sz
    return None,None,None

def load_pyc(a):
    """Return (bytes, source_label). Accepts a loose --pyc or --archive/--cdfiles/--file."""
    if getattr(a,'pyc',None) and os.path.exists(a.pyc):
        return open(a.pyc,'rb').read(), a.pyc
    if getattr(a,'archive',None) and getattr(a,'cdfiles',None):
        name=getattr(a,'file',None) or 'DB_GAME_LOCAL_SCRIPT.PYC'
        data,off,sz=extract_from_archive(a.archive,a.cdfiles,name)
        if data is None: raise SystemExit(f'[!] {name} not found in archive')
        print(f'[extract] {name} from archive @ 0x{off:X} ({sz:,} bytes)')
        return data, f'{name} (in {os.path.basename(a.archive)})'
    raise SystemExit('[!] give --pyc FILE, or --archive AR --cdfiles CDF [--file NAME]')

# ---------------- Python 2.6 opcodes we care about ----------------
HAVE_ARGUMENT = 90
LOAD_CONST    = 100
CALL_FUNCTION = 131
EXTENDED_ARG  = 143

# ---------------- marshal reader that keeps offsets ----------------
class Code:
    __slots__ = ('name','code_off','code','consts','const_offs','children')
    def __init__(self):
        self.name=''; self.code_off=0; self.code=b''
        self.consts=[]; self.const_offs=[]; self.children=[]

class M:
    """Python 2.6 marshal reader. Records the file offset of every const value."""
    def __init__(self, data, start=8):
        self.d=data; self.i=start; self.refs=[]; self.codes=[]

    def u8(self):  v=self.d[self.i]; self.i+=1; return v
    def i32(self): v,=struct.unpack_from('<i',self.d,self.i); self.i+=4; return v
    def u32(self): v,=struct.unpack_from('<I',self.d,self.i); self.i+=4; return v

    def read(self, depth=0):
        """Returns (value, value_offset). value_offset points at the payload bytes."""
        if self.i>=len(self.d) or depth>300: return (None,None)
        t=self.u8() & 0x7f
        c=chr(t)
        if c=='N': return (None,None)
        if c=='T': return (True,None)
        if c=='F': return (False,None)
        if c=='.': return (Ellipsis,None)
        if c=='0': return (None,None)
        if c=='i':
            off=self.i; return (self.i32(), off)
        if c=='I':
            off=self.i; v,=struct.unpack_from('<q',self.d,self.i); self.i+=8; return (v,off)
        if c=='f':
            n=self.u8(); off=self.i; s=self.d[self.i:self.i+n]; self.i+=n
            try: return (float(s.decode('ascii')), off)
            except Exception: return (None, off)
        if c=='g':
            off=self.i; v,=struct.unpack_from('<d',self.d,self.i); self.i+=8; return (v,off)
        if c=='l':  # long
            n=self.i32(); self.i+=2*abs(n); return (None,None)
        if c in ('s','t','u'):
            n=self.u32(); off=self.i; b=self.d[self.i:self.i+n]; self.i+=n
            v=b.decode('latin1')
            if c=='t': self.refs.append(v)
            return (v, off)
        if c=='R':
            n=self.i32()
            return (self.refs[n] if 0<=n<len(self.refs) else None, None)
        if c in ('(','['):
            n=self.u32(); out=[]; offs=[]
            for _ in range(n):
                v,o=self.read(depth+1); out.append(v); offs.append(o)
            return (TupleWithOffsets(out,offs), None)
        if c=='{':
            out={}
            while True:
                k,_=self.read(depth+1)
                if k is None: break
                v,_=self.read(depth+1)
                try: out[k]=v
                except TypeError: pass
            return (out,None)
        if c=='c':
            return (self.read_code(depth), None)
        return (None,None)

    def read_code(self, depth):
        co=Code()
        self.i32(); self.i32(); self.i32(); self.i32()   # argcount nlocals stacksize flags
        # code string
        t=self.u8() & 0x7f
        assert chr(t) in ('s','t'), 'code object: expected string for bytecode'
        n=self.u32(); co.code_off=self.i; co.code=self.d[self.i:self.i+n]; self.i+=n
        # consts tuple
        cv,_=self.read(depth+1)
        if isinstance(cv,TupleWithOffsets):
            co.consts=cv.values; co.const_offs=cv.offsets
            co.children=[x for x in cv.values if isinstance(x,Code)]
        self.read(depth+1)  # names
        self.read(depth+1)  # varnames
        self.read(depth+1)  # freevars
        self.read(depth+1)  # cellvars
        self.read(depth+1)  # filename
        nm,_=self.read(depth+1); co.name=nm if isinstance(nm,str) else ''
        self.i32()          # firstlineno
        self.read(depth+1)  # lnotab
        self.codes.append(co)
        return co

class TupleWithOffsets:
    __slots__=('values','offsets')
    def __init__(self,v,o): self.values=v; self.offsets=o

def parse(data):
    m=M(data); m.read(); return m.codes

# ---------------- bytecode walking ----------------
def instructions(code_bytes):
    """Yield (i, op, oparg, arg_off) where arg_off is the offset of the 2 arg bytes
    relative to the start of code_bytes (None if no arg). Handles EXTENDED_ARG."""
    i=0; ext=0; n=len(code_bytes)
    while i<n:
        op=code_bytes[i]
        if op>=HAVE_ARGUMENT:
            arg=code_bytes[i+1] | (code_bytes[i+2]<<8)
            full=arg | ext
            yield (i, op, full, i+1)
            ext = (arg<<16) if op==EXTENDED_ARG else 0
            i+=3
        else:
            yield (i, op, None, None)
            ext=0
            i+=1

def find_record_loadconsts(co, uid):
    """In code object `co`, find the LOAD_CONST that pushes `uid`, then collect the
    LOAD_CONST instructions that follow it up to the next CALL_FUNCTION.
    Returns list of dicts describing the argument LOAD_CONSTs, in order."""
    ins=list(instructions(co.code))
    # locate LOAD_CONST whose const == uid (int)
    start=None
    for k,(i,op,arg,aoff) in enumerate(ins):
        if op==LOAD_CONST and arg is not None and arg<len(co.consts):
            v=co.consts[arg]
            if isinstance(v,int) and not isinstance(v,bool) and v==uid:
                start=k; break
    if start is None: return None
    args=[]
    for (i,op,arg,aoff) in ins[start:]:
        if op==CALL_FUNCTION: break
        if op==LOAD_CONST and arg is not None and arg<len(co.consts):
            args.append(dict(instr_off=i, const_index=arg, arg_off=aoff,
                             value=co.consts[arg]))
    return args

def const_index_usage(co, idx):
    """How many LOAD_CONST instructions in this code object reference const idx."""
    return sum(1 for (i,op,arg,aoff) in instructions(co.code)
               if op==LOAD_CONST and arg==idx)

def find_const_with_value(co, value):
    """Index of an existing const equal to `value` (int match, not bool)."""
    for idx,v in enumerate(co.consts):
        if isinstance(v,bool): continue
        if isinstance(v,int) and isinstance(value,int) and v==value: return idx
        if isinstance(v,float) and isinstance(value,float) and abs(v-value)<1e-9: return idx
    return None

def lap_like_consts(co, lo=1, hi=1000):
    """Existing int consts in a plausible lap range -> what a repoint can target."""
    out=[]
    for idx,v in enumerate(co.consts):
        if isinstance(v,bool): continue
        if isinstance(v,int) and lo<=v<=hi: out.append((idx,v))
    return out

def autodetect_field_index(args, old_value):
    """Find which arg slot currently holds old_value. Returns (index, ambiguous_count)."""
    hits=[n for n,x in enumerate(args) if isinstance(x['value'],int)
          and not isinstance(x['value'],bool) and x['value']==old_value]
    if not hits: return None,0
    return hits[0], len(hits)

def locate(data, uid, field_index):
    """Find the code object + the LOAD_CONST feeding the record's field_index-th arg."""
    for co in parse(data):
        args=find_record_loadconsts(co, uid)
        if not args: continue
        if field_index>=len(args): continue
        return co, args, args[field_index]
    return None,None,None

# ---------------- commands ----------------
def cmd_inspect(a):
    data,label=load_pyc(a)
    if a.field_index is None:
        if a.old_value is None:
            # show the record's args so the user can pick
            for co in parse(data):
                args=find_record_loadconsts(co,a.uid)
                if args:
                    print(f'[+] code object {co.name!r}: record UID {a.uid} args:')
                    for n,x in enumerate(args[:16]):
                        print(f'    arg[{n}] const[{x["const_index"]}] = {x["value"]!r}')
                    print('\n[i] rerun with --field-index N (or --old-value V to auto-pick)')
                    return 0
            print(f'[!] UID {a.uid} not found'); return 2
        for co in parse(data):
            args=find_record_loadconsts(co,a.uid)
            if args:
                idx,n=autodetect_field_index(args,a.old_value)
                if idx is None:
                    print(f'[!] no arg holds {a.old_value} in record {a.uid}'); return 2
                if n>1: print(f'[warn] {n} args hold {a.old_value}; using arg[{idx}] (override with --field-index)')
                a.field_index=idx; break
    co,args,target=locate(data,a.uid,a.field_index)
    if not co:
        print(f'[!] UID {a.uid} not found as a LOAD_CONST in any code object'); return 2
    print(f'[+] code object: {co.name!r}  consts={len(co.consts)}  bytecode@0x{co.code_off:X}')
    print(f'[+] record UID {a.uid}: {len(args)} LOAD_CONST args before CALL_FUNCTION')
    for n,x in enumerate(args[:12]):
        mark=' <== target' if n==a.field_index else ''
        print(f'    arg[{n}] const[{x["const_index"]}] = {x["value"]!r}{mark}')
    idx=target['const_index']
    uses=const_index_usage(co, idx)
    print(f'\n[+] target const index {idx} value={target["value"]!r}')
    print(f'    LOAD_CONST references to this index in this code object: {uses}')
    print(f'    operand file offset: 0x{co.code_off + target["arg_off"]:X} (2 bytes)')
    if uses>1:
        print('    -> SHARED: editing the constant value would change every user.')
        print('       Isolated edit requires repointing this operand.')
    else:
        print('    -> index used once here (still verify with a full record diff).')
    if a.new_value is not None:
        ni=find_const_with_value(co, a.new_value)
        if ni is None:
            print(f'\n[reject] no existing const holds {a.new_value}; isolated repoint impossible')
            print('         (adding a const would change the PYC size).')
            avail=sorted({v for _,v in lap_like_consts(co)})
            print(f'         values available for repoint in this code object ({len(avail)}):')
            print('         '+', '.join(str(v) for v in avail[:60]) + (' …' if len(avail)>60 else ''))
        else:
            print(f'\n[ok] const index {ni} already holds {a.new_value} -> repoint is same-size and safe')
    return 0

def cmd_repoint(a):
    raw,label=load_pyc(a)
    data=bytearray(raw)
    if a.field_index is None:
        if a.old_value is None: raise SystemExit('--field-index or --old-value required')
        for co0 in parse(bytes(data)):
            args0=find_record_loadconsts(co0,a.uid)
            if args0:
                idx,_=autodetect_field_index(args0,a.old_value)
                if idx is None: raise SystemExit(f'[!] no arg holds {a.old_value}')
                a.field_index=idx; break
    co,args,target=locate(bytes(data),a.uid,a.field_index)
    if not co:
        print(f'[!] UID {a.uid} not found'); return 2
    old_idx=target['const_index']
    new_idx=find_const_with_value(co, a.new_value)
    if new_idx is None:
        print(f'[reject] value {a.new_value} not in const table; refusing shared-constant edit')
        avail=sorted({v for _,v in lap_like_consts(co)})
        print('         available: '+', '.join(str(v) for v in avail[:60]))
        return 3
    if new_idx==old_idx:
        print('[i] already points at that value; nothing to do'); return 0
    if new_idx>0xFFFF:
        print('[reject] const index needs EXTENDED_ARG; not supported'); return 4
    off=co.code_off + target['arg_off']
    before=struct.unpack_from('<H',data,off)[0]
    assert before==old_idx, f'operand mismatch: {before} != {old_idx}'
    struct.pack_into('<H',data,off,new_idx)
    print(f'[+] repointed UID {a.uid} field[{a.field_index}]: const[{old_idx}]={target["value"]!r}'
          f' -> const[{new_idx}]={a.new_value!r}')
    assert len(data)==len(raw), 'internal: size changed'
    if a.out_archive:
        if not (a.archive and a.cdfiles): raise SystemExit('--out-archive needs --archive/--cdfiles')
        name=getattr(a,'file',None) or 'DB_GAME_LOCAL_SCRIPT.PYC'
        _,off,sz=extract_from_archive(a.archive,a.cdfiles,name)
        if sz!=len(data): raise SystemExit('[reject] patched pyc size differs; refusing')
        import shutil as _sh
        _sh.copyfile(a.archive,a.out_archive)
        with open(a.out_archive,'r+b') as f:
            f.seek(off); f.write(bytes(data))
        print(f'[+] wrote patched archive copy: {a.out_archive} (same size, in-place entry)')
        return 0
    out=a.out or ((a.pyc or 'out')+'.repointed')
    open(out,'wb').write(bytes(data))
    print(f'[+] wrote {out}  same_size=True')
    return 0

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest='cmd', required=True)
    for name in ('inspect','repoint'):
        p=sub.add_parser(name)
        p.add_argument('--pyc')
        p.add_argument('--archive'); p.add_argument('--cdfiles')
        p.add_argument('--file', default='DB_GAME_LOCAL_SCRIPT.PYC')
        p.add_argument('--uid', type=int, required=True)
        p.add_argument('--field-index', type=int, default=None,
                       help='0-based index among the record call LOAD_CONST args (auto if --old-value given)')
        p.add_argument('--old-value', type=int, default=None,
                       help='current value of the field; used to auto-pick --field-index')
        p.add_argument('--new-value', type=int, default=None)
        if name=='repoint':
            p.add_argument('--out'); p.add_argument('--out-archive')
    a=ap.parse_args()
    if a.cmd=='inspect':
        sys.exit(cmd_inspect(a))
    else:
        if a.new_value is None: print('--new-value required'); sys.exit(2)
        sys.exit(cmd_repoint(a))

if __name__=='__main__':
    main()
