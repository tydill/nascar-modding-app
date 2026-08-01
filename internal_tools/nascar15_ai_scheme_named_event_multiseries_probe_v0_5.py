#!/usr/bin/env python3
"""Python-2.5 marshal/bytecode helpers for named-race AI paint assignment.

Restored because the packaged extra-scheme manager imports this proven helper.
Every mutation reparses the complete PYC in the caller before installation.
"""
from __future__ import annotations
import struct
VERSION='0.5-compat-rc11.1'
POP_TOP=1
RETURN_VALUE=83
HAVE_ARGUMENT=90
STORE_ATTR=95
LOAD_CONST=100
BUILD_TUPLE=102
LOAD_ATTR=105
COMPARE_OP=106
IMPORT_NAME=108
JUMP_IF_FALSE=111
JUMP_ABSOLUTE=113
LOAD_GLOBAL=116
LOAD_FAST=124
CALL_FUNCTION=131

def emit(op,arg=None):
    if arg is None:return bytes([int(op)])
    if not 0<=int(arg)<=0xffff:raise ValueError('bytecode operand needs EXTENDED_ARG')
    return bytes([int(op),int(arg)&255,(int(arg)>>8)&255])

def _walk_mvals(obj):
    yield obj
    value=getattr(obj,'value',None)
    if hasattr(value,'consts'):
        for x in value.consts:yield from _walk_mvals(x)
    elif isinstance(value,list):
        for x in value:yield from _walk_mvals(x)
    elif isinstance(value,dict):
        for k,v in value.items():
            if hasattr(k,'value'):yield from _walk_mvals(k)
            if hasattr(v,'value'):yield from _walk_mvals(v)

def find_code_mval(mapper,root,name):
    hits=[m for m in _walk_mvals(root) if hasattr(getattr(m,'value',None),'name') and getattr(m.value,'name',None)==name]
    if len(hits)!=1:raise ValueError(f'expected one code object {name}; found {len(hits)}')
    return hits[0]

def code_object(mapper,pyc,name):
    return find_code_mval(mapper,mapper.parse_pyc(pyc),name).value

class _Skip:
    def __init__(self,data,pos):self.d=data;self.p=pos
    def take(self,n):
        if self.p+n>len(self.d):raise ValueError('truncated marshal object')
        q=self.p;self.p+=n;return q
    def i32(self):return struct.unpack_from('<i',self.d,self.take(4))[0]
    def obj(self,depth=0):
        if depth>400:raise ValueError('marshal nesting too deep')
        tag=chr(self.d[self.take(1)]&0x7f)
        if tag in 'NTFS.0':return
        if tag=='i':self.take(4);return
        if tag=='I':self.take(8);return
        if tag=='g':self.take(8);return
        if tag=='y':self.take(16);return
        if tag=='f':self.take(self.d[self.take(1)]);return
        if tag=='x':self.take(self.d[self.take(1)]);self.take(self.d[self.take(1)]);return
        if tag=='l':self.take(abs(self.i32())*2);return
        if tag in 'stu':
            n=self.i32()
            if n<0:raise ValueError('negative marshal string length')
            self.take(n);return
        if tag=='R':self.take(4);return
        if tag in '([':
            n=self.i32()
            for _ in range(n):self.obj(depth+1)
            return
        if tag in '<>':
            n=self.i32()
            for _ in range(n):self.obj(depth+1)
            return
        if tag=='{':
            while True:
                if chr(self.d[self.p]&0x7f)=='0':self.p+=1;break
                self.obj(depth+1);self.obj(depth+1)
            return
        if tag=='c':
            self.take(16)
            for _ in range(8):self.obj(depth+1)
            self.take(4);self.obj(depth+1);return
        raise ValueError(f'unsupported marshal tag {tag!r}')

def _tuple_layout(data,pos):
    if chr(data[pos]&0x7f)!='(':raise ValueError('expected marshal tuple')
    count_pos=pos+1;count=struct.unpack_from('<i',data,count_pos)[0]
    r=_Skip(data,count_pos+4)
    for _ in range(count):r.obj(1)
    return {'count_pos':count_pos,'count':count,'payload':count_pos+4,'end':r.p}

def code_layout(pyc,mapper,name):
    m=find_code_mval(mapper,mapper.parse_pyc(pyc),name);p=int(m.tag_offset)
    if chr(pyc[p]&0x7f)!='c':raise ValueError('target is not a marshal code object')
    p+=1+16
    tag=chr(pyc[p]&0x7f)
    if tag not in ('s','t'):raise ValueError('code bytecode is not a marshal string')
    code_len_pos=p+1;code_len=struct.unpack_from('<i',pyc,code_len_pos)[0];code_payload=p+5;code_end=code_payload+code_len
    consts=_tuple_layout(pyc,code_end);names=_tuple_layout(pyc,consts['end'])
    return {'code_len_pos':code_len_pos,'code_len':code_len,'code_payload':code_payload,'code_end':code_end,
            'const_count_pos':consts['count_pos'],'const_count':consts['count'],'const_end':consts['end'],
            'name_count_pos':names['count_pos'],'name_count':names['count'],'name_end':names['end']}

def _marshal(value):
    if value is None:return b'N'
    if value is True:return b'T'
    if value is False:return b'F'
    if isinstance(value,int):
        return b'i'+struct.pack('<i',value) if -(2**31)<=value<2**31 else b'I'+struct.pack('<q',value)
    if isinstance(value,str):
        raw=value.encode('ascii','strict');return b't'+struct.pack('<i',len(raw))+raw
    raise TypeError(f'unsupported marshal constant {value!r}')

def ensure_const(mapper,pyc,code_name,value):
    co=code_object(mapper,pyc,code_name)
    plain=getattr(mapper,'value_plain_for_compare',lambda x:getattr(x,'value',x))
    for i,item in enumerate(co.consts):
        if plain(item)==value:return pyc,i,False
    layout=code_layout(pyc,mapper,code_name);encoded=_marshal(value);out=bytearray(pyc)
    out[layout['const_end']:layout['const_end']]=encoded
    struct.pack_into('<i',out,layout['const_count_pos'],layout['const_count']+1)
    rebuilt=bytes(out);co2=code_object(mapper,rebuilt,code_name)
    if plain(co2.consts[-1])!=value:raise ValueError('constant append readback failed')
    return rebuilt,len(co.consts),True

def ensure_name(mapper,pyc,code_name,value):
    co=code_object(mapper,pyc,code_name);value=str(value)
    if value in co.names:return pyc,co.names.index(value),False
    layout=code_layout(pyc,mapper,code_name);raw=value.encode('ascii','strict');encoded=b't'+struct.pack('<i',len(raw))+raw
    out=bytearray(pyc);out[layout['name_end']:layout['name_end']]=encoded
    struct.pack_into('<i',out,layout['name_count_pos'],layout['name_count']+1)
    rebuilt=bytes(out);co2=code_object(mapper,rebuilt,code_name)
    if co2.names[-1]!=value:raise ValueError('name append readback failed')
    return rebuilt,len(co.names),True
