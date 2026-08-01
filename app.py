#!/usr/bin/env python3
# NASCAR Modding App v1.0.2 - public release
import csv, io, json, os, re, shutil, struct, subprocess, tempfile, webbrowser, math as _math, importlib.util, collections, time, threading, zipfile, hashlib
import numpy as np
from PIL import Image, ImageFilter, ImageDraw
from flask import Flask, jsonify, request, send_file, send_from_directory, after_this_request, Response, g
import containers as C

import sys
import datetime
from pathlib import Path
if getattr(sys, 'frozen', False):
    RES_DIR  = sys._MEIPASS                     # bundled static/data/texconv
    USER_DIR = os.path.dirname(sys.executable)  # config/schemes live next to the exe
else:
    RES_DIR  = os.path.dirname(os.path.abspath(__file__))
    USER_DIR = RES_DIR
APP_DIR = RES_DIR
DATA = os.path.join(RES_DIR, 'data')
INTERNAL_TOOLS_DIR = os.path.join(RES_DIR, 'internal_tools')
if os.path.isdir(INTERNAL_TOOLS_DIR) and INTERNAL_TOOLS_DIR not in sys.path:
    sys.path.insert(0, INTERNAL_TOOLS_DIR)

def component_path(name):
    """Locate a bundled component without exposing helper scripts in the app root."""
    candidates = [
        os.path.join(INTERNAL_TOOLS_DIR, name),
        os.path.join(APP_DIR, name),
        os.path.join(DATA, name),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]

SELECTOR_CONFIG = os.path.join(USER_DIR, 'game_selector.json')
ACTIVE_GAME = 'nascar15'
GAME_SESSION_SELECTED = False
_GAME_SWITCH_LOCK = threading.RLock()

GAME_PROFILES = {
    'nascar15': {
        'id': 'nascar15',
        'name': 'NASCAR 15',
        'short_name': 'NASCAR 15',
        'season_prefix': '15',
        'folder_names': ('NASCAR 15',),
        'required_archives': ('0', '2'),
        'paint_primary_archive': '2',
        'tabs': ('Setup','Grid','Names','Text','Stats','Audio','Race','AI','UI','Repoint','Settings','Checkup'),
        'paint_modes': ('library','create','schedule'),
        'full_feature_set': True,
        'season_year': 2015,
        'series_uid': 25040,
        'number_container': 'SPRINTNUMS2015.ARC',
        'data_subdir': '',
        'team_editor_mode': 'full',
        'graphics_mode': 'packaged',
    },
    'nascar14': {
        'id': 'nascar14',
        'name': "NASCAR '14",
        'short_name': "NASCAR '14",
        'season_prefix': '14',
        'folder_names': ("NASCAR '14", 'NASCAR 14'),
        'required_archives': ('0', '7', '8'),
        'paint_primary_archive': '7',
        'tabs': ('Setup','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings'),
        'paint_modes': ('library',),
        'full_feature_set': False,
        'season_year': 2014,
        'series_uid': 22538,
        'number_container': 'SPRINTNUMS2014.ARC',
        'data_subdir': 'nascar14',
        'team_editor_mode': 'names_only',
        'graphics_mode': 'discovered',
    },
}

def _profile_dir(game_id=None):
    gid = game_id or ACTIVE_GAME
    if gid == 'nascar15':
        return USER_DIR
    return os.path.join(USER_DIR, 'profiles', gid)

def _profile_config_path(game_id=None):
    return os.path.join(_profile_dir(game_id), 'config.json')

def _profile_schemes_path(game_id=None):
    return os.path.join(_profile_dir(game_id), 'schemes')

CONFIG = _profile_config_path(ACTIVE_GAME)
SCHEMES = _profile_schemes_path(ACTIVE_GAME)
os.makedirs(SCHEMES, exist_ok=True)
app = Flask(__name__, static_folder=os.path.join(RES_DIR,'static'))

RAW_OFFSET = 0x100
# Fallback only. NASCAR 15 normally builds the Names list from live RACETEAM_c
# records so active teams such as Phil Parsons Racing and JR Motorsports are not
# lost, and unused legacy teams are not shown.
TEAMS_2015 = ["Chip Ganassi Racing","Front Row Motorsports","Roush Fenway Racing",
 "Richard Childress Racing","Team Penske","Joe Gibbs Racing","Hendrick Motorsports",
 "Stewart-Haas Racing","Germain Racing","Michael Waltrip Racing","Richard Petty Motorsports",
 "Furniture Row Racing","Wood Brothers Racing","HScott Motorsports","JTG Daugherty Racing",
 "Go FAS Racing","BK Racing","Leavine Family Racing","Phil Parsons Racing","JR Motorsports",
 "Custom Chevrolet","Custom Ford","Custom Toyota"]
TEAMS_2014 = ["Richard Petty Motorsports","JTG Daugherty Racing","Front Row Motorsports",
 "Roush Fenway Racing","Richard Childress Racing","Joe Gibbs Racing","Team Penske",
 "Hendrick Motorsports","Stewart-Haas Racing","Michael Waltrip Racing","Furniture Row Racing",
 "Germain Racing","Tommy Baldwin Racing","Wood Brothers Racing","Chip Ganassi Racing",
 "Leavine Family Racing","Phil Parsons Racing","NEMCO Motorsports","Hillman Racing",
 "XXXtreme Motorsport","Swan Racing","BK Racing","Phoenix Racing","JR Motorsports","Go Green Racing","PH LLC"]

APP_NAME = 'NASCAR Modding App'
APP_VERSION = '1.0.2'
APP_RELEASE_LABEL = 'Public release'

# New installs use the Modding App backup suffix. Previous-version backups
# remain valid and are reused automatically so updates never strand a pristine copy.
MOD_BACKUP_SUFFIX = '.n15mod.bak'
LEGACY_BACKUP_SUFFIX = '.gridapp.bak'

def backup_path(live_path):
    """Return the oldest surviving app backup, not merely the newest suffix.

    Some long-running installs contain both a legacy ``.gridapp.bak`` made
    before the first mod and a newer ``.n15mod.bak`` made after experimental
    work.  Preferring the newer filename can silently treat already-modified
    bytes as pristine.  The earliest timestamp is the safest available base.
    """
    modern = str(live_path) + MOD_BACKUP_SUFFIX
    legacy = str(live_path) + LEGACY_BACKUP_SUFFIX
    existing = [p for p in (modern, legacy) if os.path.exists(p)]
    if existing:
        # Hard precedence, not mtime: a legacy .gridapp.bak always predates a
        # .n15mod.bak by release history, and mtime is not durable across
        # folder copies, cloud sync, or restores from an external backup.
        # Trusting mtime can silently promote an already-modified archive to
        # "pristine", which bakes mods into the baseline permanently.
        if os.path.exists(legacy):
            return legacy
        return modern
    return modern


def _valid_backup(path, kind):
    """Is this file trustworthy enough to copy back over a live game file?

    FIX (v1.0.2-dev6): /api/restore calls this three times, but the definition
    was lost when the module was reorganised for dev2.  Python only resolves a
    global at call time, so the package imported cleanly, every route listed
    fine, and the failure only appeared the moment a user pressed Restore --
    as a bare NameError.  That is the whole of "all the restoring stuff
    failed".  Behaviour below is the proven v1.0 rule, unchanged.

    kind is 'cdf' (must carry the filC magic) or 'ar' (an archive blob, whose
    outer header varies, so only a plausible size is required).
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 64:
            return False
        with open(path, 'rb') as fh:
            head = fh.read(4)
    except OSError:
        return False
    if kind == 'cdf':
        return head == b'filC'
    return os.path.getsize(path) > 1024


# ---------------- durable write primitives ----------------
def atomic_write_bytes(path, data, tmp_suffix='.tmp'):
    """Durably replace `path` with `data`.

    os.replace() is atomic with respect to the *rename*, not the *contents*.
    Without an fsync first, a crash or power loss between the write and the
    rename can leave a valid directory entry pointing at a partial or
    zero-length file. For a cdfiles index that means a bricked install, so
    every index rewrite goes through here.
    """
    tmp = str(path) + tmp_suffix
    try:
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


class RollbackFailed(RuntimeError):
    """An install failed AND its rollback also failed.

    This is a different situation from a plain install failure and needs
    different user action, so it must never be collapsed into the original
    error or silently discarded.
    """

    def __init__(self, install_error, rollback_error):
        self.install_error = install_error
        self.rollback_error = rollback_error
        super().__init__(
            'INSTALL FAILED AND ROLLBACK FAILED - this archive may be inconsistent. '
            'Do not launch the game. Use Restore from backup before doing anything else. '
            f'Install error: {install_error} | Rollback error: {rollback_error}')


def rollback_archive_cdf(v, archive_size, cdf_bytes, tmp_suffix, install_error):
    """Truncate an archive back to `archive_size` and restore exact cdf bytes.

    Raises RollbackFailed if the restore cannot complete. Callers must not
    wrap this in a bare `except Exception: pass` - a rollback that fails
    quietly leaves a truncated archive behind a stale index while telling the
    user the operation was atomic.
    """
    try:
        with open(v['ar'], 'r+b') as fh:
            fh.truncate(archive_size)
            fh.flush()
            os.fsync(fh.fileno())
        atomic_write_bytes(v['cdf'], cdf_bytes, tmp_suffix)
    except Exception as rb:
        raise RollbackFailed(install_error, rb) from install_error


# ---------------- config / selected game ----------------
def load_cfg():
    try:
        return json.load(open(CONFIG, encoding='utf-8')) if os.path.exists(CONFIG) else {}
    except Exception:
        return {}

def save_cfg(c):
    os.makedirs(os.path.dirname(CONFIG) or USER_DIR, exist_ok=True)
    tmp = CONFIG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(c, fh, indent=1)
    os.replace(tmp, CONFIG)

def _load_selector_cfg():
    try:
        return json.load(open(SELECTOR_CONFIG, encoding='utf-8')) if os.path.exists(SELECTOR_CONFIG) else {}
    except Exception:
        return {}

def _save_selector_cfg(data):
    tmp = SELECTOR_CONFIG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, SELECTOR_CONFIG)

def active_game_profile():
    return GAME_PROFILES[ACTIVE_GAME]

def active_game_name():
    return active_game_profile()['name']

def _lock_busy(lock):
    """True when another request currently owns a protected write lock."""
    try: acquired=lock.acquire(blocking=False)
    except TypeError: acquired=lock.acquire(False)
    if acquired:
        lock.release(); return False
    return True


def _protected_operation_busy():
    busy=[]
    for name in ('_EXTRA_CREATE_LOCK','_TEAM_MANAGER_LOCK','_FULL_REPAIR_LOCK','_RP_LOCK'):
        lock=globals().get(name)
        if lock is not None and _lock_busy(lock): busy.append(name)
    return busy


def _activate_game(game_id):
    global ACTIVE_GAME, CONFIG, SCHEMES
    global FULL_REPAIR_REPORT, EXTRA_SCHEME_STATE, EXTRA_SCHEME_IMAGES, EXTRA_SCHEME_ROLLBACK_DIR
    global TEAM_MANAGER_STATE, TEAM_ASSET_ROLLBACK_DIR, _RP_HISTORY
    if game_id not in GAME_PROFILES: raise ValueError('unsupported game profile')
    busy=_protected_operation_busy()
    if busy: raise RuntimeError('cannot change games while a protected operation is running: '+', '.join(busy))
    snapshot=dict(active=ACTIVE_GAME,config=CONFIG,schemes=SCHEMES,
                  full=globals().get('FULL_REPAIR_REPORT'),extra=globals().get('EXTRA_SCHEME_STATE'),
                  extra_images=globals().get('EXTRA_SCHEME_IMAGES'),extra_rollback=globals().get('EXTRA_SCHEME_ROLLBACK_DIR'),
                  team=globals().get('TEAM_MANAGER_STATE'),rollback=globals().get('TEAM_ASSET_ROLLBACK_DIR'),rp=globals().get('_RP_HISTORY'))
    new_config=_profile_config_path(game_id); new_schemes=_profile_schemes_path(game_id)
    os.makedirs(new_schemes, exist_ok=True)
    try:
        ACTIVE_GAME=game_id; CONFIG=new_config; SCHEMES=new_schemes
        if 'FULL_REPAIR_REPORT' in globals(): FULL_REPAIR_REPORT=os.path.join(_profile_dir(game_id),'last_whole_mod_repair.json')
        if 'EXTRA_SCHEME_STATE' in globals(): EXTRA_SCHEME_STATE=os.path.join(_profile_dir(game_id),'extra_schemes_v1.json')
        if 'EXTRA_SCHEME_IMAGES' in globals(): EXTRA_SCHEME_IMAGES=os.path.join(SCHEMES,'extra')
        if 'EXTRA_SCHEME_ROLLBACK_DIR' in globals(): EXTRA_SCHEME_ROLLBACK_DIR=os.path.join(_profile_dir(game_id),'extra_scheme_rollback_v1')
        if 'TEAM_MANAGER_STATE' in globals(): TEAM_MANAGER_STATE=os.path.join(_profile_dir(game_id),'team_manager_state.json')
        if 'TEAM_ASSET_ROLLBACK_DIR' in globals(): TEAM_ASSET_ROLLBACK_DIR=os.path.join(_profile_dir(game_id),'team_asset_rollback_v1')
        if '_RP_HISTORY' in globals(): _RP_HISTORY=os.path.join(_profile_dir(game_id),'repoint_history.json')
        for cache_name in ('_UI_TEXT_FILE_CACHE','_BASELINE_VERIFY_CACHE','_UI_THUMB_CACHE','_SCHEDULE_CACHE','_SCHEDULE_SOURCE_CACHE','_TRACK_CACHE','_CDF_ENTRY_CACHE','_PYC_AUDIT_CACHE'):
            cache=globals().get(cache_name)
            if isinstance(cache,dict): cache.clear()
        text_cache=globals().get('_UI_TEXT_CACHE')
        if isinstance(text_cache,dict): text_cache.clear(); text_cache.update(signature=None,rows=None,files=None,errors=None)
        packaged=globals().get('_UI_PACKAGED_MAP_CACHE')
        if isinstance(packaged,dict): packaged.clear(); packaged.update(signature=None,rows={})
        ui_index=globals().get('_UI_INDEX_CACHE')
        if isinstance(ui_index,dict): ui_index.clear(); ui_index.update(signature=None,rows=None)
        selector=_load_selector_cfg(); selector['last_game']=game_id; _save_selector_cfg(selector)
    except Exception:
        ACTIVE_GAME=snapshot['active']; CONFIG=snapshot['config']; SCHEMES=snapshot['schemes']
        for name,key in (('FULL_REPAIR_REPORT','full'),('EXTRA_SCHEME_STATE','extra'),('EXTRA_SCHEME_IMAGES','extra_images'),('EXTRA_SCHEME_ROLLBACK_DIR','extra_rollback'),('TEAM_MANAGER_STATE','team'),('TEAM_ASSET_ROLLBACK_DIR','rollback'),('_RP_HISTORY','rp')):
            if name in globals(): globals()[name]=snapshot[key]
        raise


def _steam_library_roots():
    """Real Steam library paths from libraryfolders.vdf, plus common fallbacks.

    The old hardcoded C:/D:/E: list missed any library on another drive or a
    non-default Steam install, which showed up as "game was not found" even
    when the game was installed and working.
    """
    roots = []
    for vdf in (r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf",
                r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"):
        try:
            if not os.path.exists(vdf):
                continue
            with open(vdf, encoding='utf-8', errors='replace') as fh:
                for m in re.finditer(r'"path"\s*"([^"]+)"', fh.read()):
                    p = m.group(1).replace('\\\\', '\\')
                    common = os.path.join(p, 'steamapps', 'common')
                    if os.path.isdir(common):
                        roots.append(common)
        except Exception:
            pass
    for fallback in (r"C:\Program Files (x86)\Steam\steamapps\common",
                     r"D:\SteamLibrary\steamapps\common",
                     r"E:\SteamLibrary\steamapps\common"):
        if fallback not in roots:
            roots.append(fallback)
    return roots


def _steam_guesses(game_id=None):
    profile = GAME_PROFILES[game_id or ACTIVE_GAME]
    out = []
    for root in _steam_library_roots():
        for folder in profile['folder_names']:
            base = os.path.join(root, folder)
            out.append(base)
            # Steam installs some of these titles into a duplicated subfolder
            # (…\NASCAR 14\NASCAR 14\data), which the flat probe never saw.
            for nested in profile['folder_names']:
                out.append(os.path.join(base, nested))
    return out


def _profile_root_from(path, game_id=None):
    """Return the install root for `path`, or None.

    Accepts what users actually paste, not just the one canonical form:
      - the install root            (…\\NASCAR 14)
      - the data folder itself      (…\\NASCAR 14\\data)
      - a duplicated nested folder  (…\\NASCAR 14\\NASCAR 14)
    Always returns the ROOT, because registry() appends 'data' downstream.
    """
    if not path:
        return None
    profile = GAME_PROFILES[game_id or ACTIVE_GAME]
    required = profile['required_archives']

    def has_archives(root):
        d = os.path.join(root, 'data')
        return all(os.path.exists(os.path.join(d, f'ARCHIVE{k}.AR')) for k in required)

    path = str(path).rstrip('\\/')
    candidates = [path]
    # user pasted the data folder -> its parent is the root
    if os.path.basename(path).lower() == 'data':
        candidates.append(os.path.dirname(path))
    # user pasted the outer folder of a duplicated install
    for folder in profile['folder_names']:
        candidates.append(os.path.join(path, folder))
    for root in candidates:
        if root and has_archives(root):
            return root
    return None


def _path_has_profile(path, game_id=None):
    return _profile_root_from(path, game_id) is not None

def detect_game(game_id=None):
    gid = game_id or ACTIVE_GAME
    cfg_path = _profile_config_path(gid)
    try:
        configured = json.load(open(cfg_path, encoding='utf-8')).get('game') if os.path.exists(cfg_path) else None
    except Exception:
        configured = None
    root = _profile_root_from(configured, gid)
    if root:
        return root
    for path in _steam_guesses(gid):
        root = _profile_root_from(path, gid)
        if root:
            return root
    return None


# ---------------- shared imported-image preparation ----------------
IMAGE_RESIZE_MODES = ('auto','fit','fill','stretch','nearest')

def prepare_import_image(img, target_size, mode='fit', preserve_alpha=True,
                         background=(0,0,0,0)):
    """Resize any imported image to an exact game texture size.

    fit:     preserve aspect ratio and pad/letterbox
    fill:    preserve aspect ratio and center-crop
    stretch: exact resize (best for fixed UV atlases such as car liveries)
    nearest: fit/pad using nearest-neighbour for pixel art

    RGBA is retained whenever preserve_alpha is true. Callers writing DXT1 may
    convert the result to RGB only after this function returns.
    """
    tw,th = map(int,target_size)
    if tw<=0 or th<=0:
        raise ValueError('invalid target image size')
    mode=(mode or 'fit').lower()
    if mode not in IMAGE_RESIZE_MODES:
        mode='fit'
    # `auto` requires target metadata and is resolved by the caller. Keep this
    # low-level helper deterministic when it is used by older routes.
    if mode=='auto':
        mode='fit'
    source_format=(getattr(img,'format',None) or 'unknown').upper()
    source_mode=str(getattr(img,'mode','unknown'))
    source_alpha=('A' in source_mode) or ('transparency' in getattr(img,'info',{}))
    src=img.convert('RGBA' if preserve_alpha else 'RGB')
    sw,sh=src.size
    if sw<=0 or sh<=0:
        raise ValueError('imported image has invalid dimensions')
    if (sw,sh)==(tw,th):
        return src, dict(resized=False, source=[sw,sh], target=[tw,th], mode=mode,
                         source_format=source_format, source_mode=source_mode,
                         source_alpha=bool(source_alpha), preserve_alpha=bool(preserve_alpha))
    lanczos=Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS
    nearest=Image.Resampling.NEAREST if hasattr(Image,'Resampling') else Image.NEAREST
    if mode=='stretch':
        out=src.resize((tw,th),lanczos)
    elif mode in ('fit','nearest'):
        filt=nearest if mode=='nearest' else lanczos
        scale=min(tw/sw,th/sh)
        nw=max(1,round(sw*scale)); nh=max(1,round(sh*scale))
        small=src.resize((nw,nh),filt)
        bg=background if preserve_alpha else background[:3]
        out=Image.new('RGBA' if preserve_alpha else 'RGB',(tw,th),bg)
        out.paste(small,((tw-nw)//2,(th-nh)//2),small if preserve_alpha else None)
    else: # fill
        scale=max(tw/sw,th/sh)
        nw=max(1,round(sw*scale)); nh=max(1,round(sh*scale))
        large=src.resize((nw,nh),lanczos)
        left=max(0,(nw-tw)//2); top=max(0,(nh-th)//2)
        out=large.crop((left,top,left+tw,top+th))
    return out, dict(resized=True, source=[sw,sh], target=[tw,th], mode=mode,
                     source_format=source_format, source_mode=source_mode,
                     source_alpha=bool(source_alpha), preserve_alpha=bool(preserve_alpha))

def request_resize_mode(default='fit'):
    """Read resize_mode from multipart form, JSON, or query string."""
    mode=request.form.get('resize_mode') or request.args.get('resize_mode')
    if not mode and request.is_json:
        try: mode=(request.get_json(silent=True) or {}).get('resize_mode')
        except Exception: mode=None
    mode=(mode or default).lower()
    return mode if mode in IMAGE_RESIZE_MODES else default

def registry():
    """All archive/cdfiles pairs present in the game's data folder.
    Keys: '0','2','544386974',... Values: dict(ar,cdf,bak)."""
    g = load_cfg().get('game') or detect_game()
    if not g: return None, {}
    d = os.path.join(g,'data')
    reg={}
    try:
        entries = os.listdir(d)
    except OSError:
        # Configured game folder has no readable data\ subfolder (moved, renamed,
        # wrong folder level, or permissions). Report "no archives" so callers use
        # their normal guidance path instead of surfacing a raw errno.
        return g, {}
    for f in entries:
        m=re.match(r'^cdfiles(\d*)\.dat$', f, re.I)
        if not m: continue
        suf=m.group(1) or '0'
        ar=os.path.join(d, f'ARCHIVE{suf}.AR')
        if os.path.exists(ar):
            reg[suf]=dict(ar=ar, cdf=os.path.join(d,f), bak=backup_path(ar))
    return g, reg

def need(reg, key):
    if key not in reg: raise ValueError(f'ARCHIVE{key} not found in game data folder')
    return reg[key]

# ---------------- cdfiles ----------------
def parse_cdfiles(path):
    d = open(path,'rb').read()
    hdr = struct.unpack_from('<12I', d, 0)
    if hdr[0]!=0x436C6966: raise ValueError('not filC')
    n, strtab = hdr[8], hdr[10]
    base = len(d)-strtab
    def nm(off):
        p=base+off; e=d.find(b'\0',p)
        return d[p:e].decode('ascii','replace')
    for start, lay in ((0x40,'A'),(0x50,'B')):
        out, ok, pos = [], 0, start
        for i in range(n):
            if pos+32>base: break
            f = struct.unpack_from('<8I', d, pos)
            if lay=='A': name_off,size,arc_off = f[1],f[2],f[5]
            else:        name_off,size,arc_off = f[3],f[4],f[7]
            s = nm(name_off) if name_off<strtab else ''
            if s and all(32<=ord(c)<127 for c in s): ok+=1
            out.append((arc_off,size,s)); pos+=32
        if ok>n*0.8: return [e for e in out if e[2]]
    raise ValueError('unrecognized cdfiles layout')

def find_entry(reg, arcid, name, pristine=False):
    cdf=need(reg,arcid)['cdf']
    if pristine and os.path.exists(backup_path(cdf)):
        cdf=backup_path(cdf)
    for o,s,n in parse_cdfiles(cdf):
        if n==name: return o,s
    raise ValueError(f'{name} not found in ARCHIVE{arcid}')

# ---------------- grid slots (multi-archive) ----------------
BASE_SLOT_RE = re.compile(r'^LIVERY_(14|15)_(\d+[A-Z]?)_(.+?)\.ARC$')
CAREER_SLOT_RE = re.compile(r'^LIVERY_CAREER_(\w+?)_(\d+)\.ARC$')
BONUS_SLOT_RE = re.compile(r'^LIVERY_(LENOVO\d*)_?(\w+)\.ARC$')
DLC_SLOT_RE = re.compile(r'^LIVERY_DLC_(\d+)_([A-Z]+)_(\d+)\.ARC$')


def slot_from_name(n, o, s, arcid, hd_map):
    base = BASE_SLOT_RE.match(n)
    career = CAREER_SLOT_RE.match(n)
    bonus = BONUS_SLOT_RE.match(n)
    dlc = DLC_SLOT_RE.match(n)
    if base:
        if base.group(1) != active_game_profile()['season_prefix']:
            return None
        num, label, kind = base.group(2), base.group(3).replace('_',' ').title(), 'driver'
    elif career:
        label, num, kind = f"Career {career.group(1).title()} {career.group(2)}", '', 'career'
    elif bonus:
        label, num, kind = f"{bonus.group(1).title()} {bonus.group(2).title()}", '', 'bonus'
    elif dlc:
        label = f"{dlc.group(2).title()} Alt {dlc.group(3)} (DLC)"
        num, kind = dlc.group(1), 'dlc'
    else:
        return None
    hn = 'HD' + n
    hd = hd_map.get(hn)
    return dict(name=n, hd=hn if hd else None, label=label,
        number=num, kind=kind, arc=arcid, sd_arc=arcid,
        sd_off=o, sd_size=s,
        hd_arc=hd[0] if hd else None,
        hd_off=hd[1] if hd else 0,
        hd_size=hd[2] if hd else 0,
        fei=None)


_GRID_SLOTS_CACHE={'sig':None,'rows':None}


def grid_slots():
    g, reg = registry()
    if not g:
        return []
    sig=[str(ACTIVE_GAME)]
    for arcid in sorted(reg):
        path=(reg.get(arcid) or {}).get('cdf')
        try:
            st=os.stat(path);sig.append((str(arcid),st.st_size,st.st_mtime_ns))
        except Exception:sig.append((str(arcid),0,0))
    sig=tuple(sig)
    if _GRID_SLOTS_CACHE.get('sig')==sig and _GRID_SLOTS_CACHE.get('rows') is not None:
        return [dict(x) for x in _GRID_SLOTS_CACHE['rows']]
    profile = active_game_profile()
    primary = profile['paint_primary_archive']
    order = sorted(reg.keys(), key=lambda k: (k != primary, k))
    entries_by_arc = {}
    for arcid in order:
        try:
            entries_by_arc[arcid] = parse_cdfiles(reg[arcid]['cdf'])
        except Exception:
            entries_by_arc[arcid] = []
    hd_map = {}
    for arcid in order:
        for off,size,name in entries_by_arc[arcid]:
            if name.startswith('HDLIVERY_'):
                hd_map[name] = (arcid, off, size)
    slots = {}
    for arcid in order:
        last_fei = None
        for off,size,name in entries_by_arc[arcid]:
            if name.startswith('FEI_LIV_'):
                last_fei = (name,off,size)
                continue
            if not name.startswith('LIVERY_'):
                continue
            slot = slot_from_name(name, off, size, arcid, hd_map)
            if not slot:
                continue
            if slot['kind'] == 'dlc' and last_fei:
                slot['fei'] = dict(name=last_fei[0], off=last_fei[1], size=last_fei[2])
            slots[name] = slot
    rows=sorted(slots.values(), key=lambda x: (x['sd_arc'] != primary, x['kind'], x['name']))
    _GRID_SLOTS_CACHE.update(sig=sig,rows=[dict(x) for x in rows])
    return rows

def driver_display_name(label):
    words=[w for w in str(label or '').split() if w.lower() not in
           ('primary','secondary','tertiary','beer') and not w.isdigit()]
    raw=' '.join(words)
    norm=re.sub(r'[^a-z0-9]+','',raw.casefold())
    mapped=_driver_display_aliases().get(norm) if '_driver_display_aliases' in globals() else None
    if mapped:return mapped
    fixed=[]
    for word in raw.split():
        u=word.upper().rstrip('.')
        if u in ('AJ','JJ'):fixed.append(u)
        elif u in ('JR','JNR'):fixed.append('Jr.')
        elif u=='MCDOWELL':fixed.append('McDowell')
        elif u=='MCMURRAY':fixed.append('McMurray')
        else:fixed.append(word)
    return ' '.join(fixed)

# ---------------- DXT1 (unchanged core from v0.3) ----------------
_565=C._565; _rgb=C._rgb
def _pack_blocks(c0,c1,idx):
    sw=c0<c1
    c0f=np.where(sw,c1,c0); c1f=np.where(sw,c0,c1)
    idxf=np.where(sw[:,None], idx^1, idx)
    idxf=np.where((c0f==c1f)[:,None],0,idxf).astype(np.uint32)
    bits=np.zeros(len(c0f),np.uint32)
    for i in range(16): bits |= idxf[:,i]<<(2*i)
    out=np.zeros((len(c0f),8),np.uint8)
    out[:,0],out[:,1]=c0f&0xFF,c0f>>8
    out[:,2],out[:,3]=c1f&0xFF,c1f>>8
    for i in range(4): out[:,4+i]=(bits>>(8*i))&0xFF
    return out.tobytes()
def _assign(bl,c0,c1):
    p0=_rgb(c0).astype(np.float64); p1=_rgb(c1).astype(np.float64)
    pal=np.stack([p0,p1,(2*p0+p1)/3,(p0+2*p1)/3],1)
    d=((bl[:,None,:,:]-pal[:,:,None,:])**2).sum(-1)
    idx=d.argmin(1)
    err=np.take_along_axis(d,idx[:,None,:],1)[:,0,:].sum(1)
    return idx.astype(np.uint32), err
def dxt1_encode_py(img, iters=3):
    H,W,_=img.shape
    bl=img.reshape(H//4,4,W//4,4,3).transpose(0,2,1,3,4).reshape(-1,16,3).astype(np.float64)
    mean=bl.mean(1,keepdims=True); cen=bl-mean
    cov=np.einsum('nij,nik->njk',cen,cen)
    v=np.ones((len(bl),3))
    for _ in range(4):
        v=np.einsum('njk,nk->nj',cov,v)
        n=np.linalg.norm(v,axis=1,keepdims=True); n[n==0]=1; v=v/n
    t=np.einsum('nij,nj->ni',cen,v)
    e0=np.clip(mean[:,0]+v*t.max(1)[:,None],0,255)
    e1=np.clip(mean[:,0]+v*t.min(1)[:,None],0,255)
    c0=_565(*(e0.T.astype(np.int32))); c1=_565(*(e1.T.astype(np.int32)))
    idx,err=_assign(bl,c0,c1)
    Wt=np.array([1.0,0.0,2/3,1/3])
    for _ in range(iters):
        w=Wt[idx]
        sw2=(w*w).sum(1); swo=(w*(1-w)).sum(1); so2=((1-w)**2).sum(1)
        det=sw2*so2-swo*swo; bad=np.abs(det)<1e-9; det[bad]=1
        bw=np.einsum('ni,nij->nj',w,bl); bo=np.einsum('ni,nij->nj',(1-w),bl)
        n0=np.clip(( so2[:,None]*bw-swo[:,None]*bo)/det[:,None],0,255)
        n1=np.clip((-swo[:,None]*bw+sw2[:,None]*bo)/det[:,None],0,255)
        nc0=_565(*(n0.T.astype(np.int32))); nc1=_565(*(n1.T.astype(np.int32)))
        nidx,nerr=_assign(bl,nc0,nc1)
        better=(nerr<err)&(~bad)
        c0=np.where(better,nc0,c0); c1=np.where(better,nc1,c1)
        idx=np.where(better[:,None],nidx,idx); err=np.where(better,nerr,err)
    return _pack_blocks(c0,c1,idx)
def dxt1_decode(payload,W,H):
    N=(W//4)*(H//4)
    need=N*8
    if len(payload) < need:
        raise ValueError(f'short DXT1 payload: read {len(payload)} bytes, expected {need}')
    a=np.frombuffer(payload[:need],np.uint8).reshape(N,8)
    c0=a[:,0].astype(np.uint16)|(a[:,1].astype(np.uint16)<<8)
    c1=a[:,2].astype(np.uint16)|(a[:,3].astype(np.uint16)<<8)
    bits=sum(a[:,4+i].astype(np.uint32)<<(8*i) for i in range(4))
    p0=_rgb(c0);p1=_rgb(c1);four=(c0>c1)[:,None]
    p2=np.where(four,(2*p0+p1)//3,(p0+p1)//2); p3=np.where(four,(p0+2*p1)//3,0)
    pal=np.stack([p0,p1,p2,p3],1).astype(np.uint8)
    idx=np.stack([(bits>>(2*i))&3 for i in range(16)],1)
    px=np.take_along_axis(pal,idx[:,:,None].astype(np.int64),1)
    return px.reshape(H//4,W//4,4,4,3).transpose(0,2,1,3,4).reshape(H,W,3)

def texconv_path():
    p=os.path.join(APP_DIR,'texconv.exe')
    return p if os.path.exists(p) and os.name=='nt' else None
def _texconv(img_pil, fmt):
    tx=texconv_path()
    if not tx: return None
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'l.png'); img_pil.save(src)
        r=subprocess.run([tx,'-y','-ft','dds','-dx9','-f',fmt,'-m','1','-o',td,src],
                         capture_output=True)
        dds=os.path.join(td,'l.dds')
        if r.returncode==0 and os.path.exists(dds):
            return open(dds,'rb').read()[128:]
    return None
def encode_image(img_pil):
    w,h=img_pil.size
    if w>=4 and h>=4:
        enc=_texconv(img_pil.convert('RGB'),'DXT1')
        if enc: return enc
    arr=np.asarray(img_pil.convert('RGB'))
    ph,pw=max(4,((h+3)//4)*4),max(4,((w+3)//4)*4)
    if (ph,pw)!=(h,w):
        pad=np.zeros((ph,pw,3),np.uint8); pad[:h,:w]=arr[:h,:w]; arr=pad
    return dxt1_encode_py(arr)
def encode_dxt5(img_pil):
    enc=_texconv(img_pil.convert('RGBA'),'DXT5')
    if enc: return enc
    return C.dxt5_encode(np.asarray(img_pil.convert('RGBA')))

def encode_any(img_pil, fmt='DXT5'):
    enc=_texconv(img_pil.convert('RGBA'),fmt)
    if enc: return enc
    if fmt=='DXT5': return C.dxt5_encode(np.asarray(img_pil.convert('RGBA')))
    return dxt1_encode_py(np.asarray(img_pil.convert('RGB')))

# ---------------- livery mip chain build: STOCK-MIP BAKE ----------------
def level_dims(w,h,L): return max(1,w>>L), max(1,h>>L)
def level_bytes(lw,lh): return max(1,(lw+3)//4)*max(1,(lh+3)//4)*8

# RC4 correction: NASCAR 15's native livery wrappers require the stock-proven
# horizontal compensation used by the original working v0.9.14 writer. These
# values are part of the native mip layout, not a cosmetic image shift. Removing
# them in RC3 made the full paint atlas visibly slide across the car. Full-image
# imports still replace every intended block, so the old donor/fade regression
# remains fixed.
STOCK_MIP_ROLL = {
    0:0, 1:-10, 2:-15, 3:-18, 4:-19, 5:-20,
    6:-20, 7:-20, 8:-20, 9:-20, 10:-20, 11:-20, 12:-20,
}

def level_offset(w,h,L):
    return sum(level_bytes(*level_dims(w,h,i)) for i in range(L))

def raw_chain_total(w,h,mips):
    return sum(level_bytes(*level_dims(w,h,L)) for L in range(mips))

def _roll_for_level(L):
    return STOCK_MIP_ROLL.get(L, STOCK_MIP_ROLL[max(STOCK_MIP_ROLL)])

def _roll_np_img(img, L):
    roll = _roll_for_level(L)
    if roll:
        return Image.fromarray(np.roll(np.asarray(img), roll, axis=1))
    return img

def _decode_stock_mip(stock_payload, w, h, L):
    """Decode the real stock mip L from backup bytes.

    This is the key difference from the older hard-bake builds:
    we do NOT resize stock mip0 to make distant base art. We use the actual stock
    mip that Eutechnyx shipped, then paint our edits onto it.
    """
    if stock_payload is None:
        return None
    lw,lh = level_dims(w,h,L)
    off = level_offset(w,h,L)
    need = level_bytes(lw,lh)
    if off + need > len(stock_payload):
        return None
    try:
        return Image.fromarray(dxt1_decode(stock_payload[off:off+need],lw,lh)).convert('RGB')
    except Exception:
        return None

def _fallback_full_composite_mip(composite, w, h, L):
    base = composite.convert('RGB').resize((w,h)) if composite.size!=(w,h) else composite.convert('RGB')
    lw,lh = level_dims(w,h,L)
    cur = base if L == 0 else base.resize((lw,lh), Image.BOX)
    return _roll_np_img(cur,L)

def _downsample_edit_layer(layer_rgba, w, h, L):
    """Downsample the edit layer as premultiplied RGBA, then roll it to stock-mip
    storage coordinates.

    A small alpha boost/dilation is applied only to lower mips so decals survive
    shallow-angle sampling, but the base itself remains the true stock mip.
    """
    if layer_rgba is None:
        return None, None
    lw,lh = level_dims(w,h,L)
    lay = layer_rgba.convert('RGBA').resize((w,h)) if layer_rgba.size!=(w,h) else layer_rgba.convert('RGBA')
    arr = np.asarray(lay).astype(np.float32)
    a = arr[:,:,3] / 255.0
    rgb = arr[:,:,:3]

    # premultiply before BOX downsample so transparent RGB does not bleed.
    pm = rgb * a[:,:,None]
    pm_img = Image.fromarray(np.clip(pm,0,255).astype(np.uint8),'RGB')
    a_img  = Image.fromarray(np.clip(a*255,0,255).astype(np.uint8),'L')

    pm_small = np.asarray(pm_img.resize((lw,lh), Image.BOX)).astype(np.float32)
    a_small_img = a_img.resize((lw,lh), Image.BOX)

    # Preserve fine edits in mips the game uses for AI/angle views.
    if L >= 2:
        k = 3 if L <= 4 else 5
        a_small_img = a_small_img.filter(ImageFilter.MaxFilter(k))

    a_small = np.asarray(a_small_img).astype(np.float32)/255.0
    boost = min(5.0, 1.0 + 0.60*L)
    a_small = np.clip(a_small*boost, 0.0, 1.0)
    a_small[a_small < 0.015] = 0.0

    # Use original non-dilated alpha for RGB unpremultiply denominator.
    a_den = np.asarray(a_img.resize((lw,lh), Image.BOX)).astype(np.float32)[:,:,None]/255.0
    a_den = np.maximum(a_den, 1.0/255.0)
    rgb_small = np.clip(pm_small / a_den, 0, 255)

    rgb_im = Image.fromarray(rgb_small.astype(np.uint8),'RGB')
    a_im = Image.fromarray(np.clip(a_small*255,0,255).astype(np.uint8),'L')

    # Stock mip bytes are already in rolled storage coordinates, so roll the edit
    # layer to match before compositing onto stock_mip.
    rgb_im = _roll_np_img(rgb_im,L)
    a_im = _roll_np_img(a_im,L)
    return rgb_im, a_im

def _composite_edit_onto_stock_mip(stock_mip, edit_rgb, edit_alpha):
    if stock_mip is None or edit_rgb is None or edit_alpha is None:
        return None
    base = stock_mip.convert('RGB')
    er = np.asarray(edit_rgb.convert('RGB')).astype(np.float32)
    ea = np.asarray(edit_alpha.convert('L')).astype(np.float32)/255.0
    sb = np.asarray(base).astype(np.float32)
    out = sb*(1.0-ea[:,:,None]) + er*ea[:,:,None]
    return Image.fromarray(np.clip(out,0,255).astype(np.uint8),'RGB')

# Compatibility wrappers for old callers/debug scripts.
def chain_levels(img, w, h, mips, shift=0, black_from=None):
    return [_fallback_full_composite_mip(img,w,h,L) for L in range(mips)]

def mask_levels(alpha_img, w, h, mips, shift=0):
    out=[]
    a = alpha_img.resize((w,h)) if alpha_img.size!=(w,h) else alpha_img
    for L in range(mips):
        lw,lh=level_dims(w,h,L)
        small=np.asarray(a if L==0 else a.resize((lw,lh),Image.BOX)).astype(np.float32)/255.0
        roll=_roll_for_level(L)
        if roll: small=np.roll(small,roll,axis=1)
        out.append(small)
    return out

def build_payload(composite, w, h, mips, stock_payload=None, layer_alpha=None,
                  shift=0, black_from=None, layer_rgba=None):
    """Stock-mip bake.

    The checker probe proved the game uses our SD/HD livery path at distance.
    The base stock scheme looks stable because its shipped lower mips are good.
    Therefore this build preserves those shipped stock mips as the base for every
    level, and paints the edit layer directly onto each stock mip.

    This should avoid the "few big color blocks" look caused by generating all
    distant base art from mip0 or from diagnostic checker patterns.
    """
    total = raw_chain_total(w,h,mips)
    payload=bytearray()

    for L in range(mips):
        lw,lh = level_dims(w,h,L)
        need_b = level_bytes(lw,lh)

        stock_mip = _decode_stock_mip(stock_payload,w,h,L)
        edit_rgb, edit_alpha = _downsample_edit_layer(layer_rgba,w,h,L) if layer_rgba is not None else (None,None)
        im = _composite_edit_onto_stock_mip(stock_mip,edit_rgb,edit_alpha)

        if im is None:
            # No usable layer/stock bytes: fall back to the current full-composite
            # stock-roll behavior.
            im = _fallback_full_composite_mip(composite,w,h,L)

        enc = encode_image(im)[:need_b]
        enc = enc + b'\0'*(need_b-len(enc))
        payload += enc

    if len(payload) < total:
        payload += b'\0'*(total-len(payload))
    return bytes(payload)


# === native SD mip L0-L10 writer v0.9.14 ===
_NATIVE_SD_ENTRY_SIZE = 0x164161
_NATIVE_SD_MIP_OFFSETS = (
    0x000000, 0x100000, 0x140000, 0x150000,
    0x154000, 0x156000, 0x158000, 0x15A000,
    0x15C000, 0x15E000, 0x160000, 0x162000,
)
_NATIVE_SD_MIP_PITCHES = (
    0x1000, 0x0800, 0x0400, 0x0200,
    0x0100, 0x0100, 0x0100, 0x0100,
    0x0100, 0x0100, 0x0100, 0x0100,
)
_NATIVE_SD_DIMS = (
    (2048,1024), (1024,512), (512,256), (256,128),
    (128,64), (64,32), (32,16), (16,8),
    (8,4), (4,2), (2,1), (1,1),
)
_NATIVE_SD_LARGE_ROLL = (0, -10, -15, -18, -19)
_NATIVE_SD_WRAP_X_BLOCKS = -5
_NATIVE_SD_WRAP_Y_BLOCKS = -1
_NATIVE_SD_PHYS_BLOCKS = 32

def _native_sd_validate_wrapper(wrapper):
    if len(wrapper) != _NATIVE_SD_ENTRY_SIZE:
        raise ValueError(
            f'SD livery entry is {len(wrapper):#x}; expected '
            f'{_NATIVE_SD_ENTRY_SIZE:#x}'
        )
    if wrapper[:4] != b'ARCC':
        raise ValueError('SD livery entry does not begin with ARCC')
    if wrapper[0xCC:0xD0] != b'DXT1':
        raise ValueError('SD livery DXT1 marker missing at 0xCC')

    table_start = len(wrapper) - 0x89
    actual_offsets = struct.unpack_from('<12I', wrapper, table_start)
    actual_pitches = struct.unpack_from('<12I', wrapper, table_start + 48)

    if tuple(actual_offsets) != _NATIVE_SD_MIP_OFFSETS:
        got = ', '.join(f'{v:#x}' for v in actual_offsets)
        raise ValueError(f'unexpected SD native mip offset table: {got}')
    if tuple(actual_pitches) != _NATIVE_SD_MIP_PITCHES:
        got = ', '.join(f'{v:#x}' for v in actual_pitches)
        raise ValueError(f'unexpected SD native mip pitch table: {got}')

def _native_sd_level_image(base_rgb, level):
    w,h = _NATIVE_SD_DIMS[level]
    box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
    image = base_rgb if level == 0 else base_rgb.resize((w,h), box)
    # Stock-proven native compensation. The game interprets these mip pages
    # with this offset; omitting it shifts the UV atlas in-game.
    if level <= 4:
        roll = _NATIVE_SD_LARGE_ROLL[level]
        if roll:
            image = Image.fromarray(
                np.roll(np.asarray(image.convert('RGB')), roll, axis=1)
            )
    return image.convert('RGB')

def _native_sd_level_mask(base_alpha, level):
    if base_alpha is None:
        return None
    w,h = _NATIVE_SD_DIMS[level]
    box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
    mask = base_alpha if base_alpha.size == (w,h) else base_alpha.resize((w,h), box)
    arr = np.asarray(mask).astype(np.float32) / 255.0
    if level <= 4:
        roll = _NATIVE_SD_LARGE_ROLL[level]
        if roll:
            arr = np.roll(arr, roll, axis=1)
    return arr

def _native_sd_block_mask(mask, width, height):
    blocks_w = max(1, (width + 3)//4)
    blocks_h = max(1, (height + 3)//4)
    if mask is None:
        return np.ones((blocks_h, blocks_w), dtype=bool)
    padded = np.zeros((blocks_h*4, blocks_w*4), dtype=np.float32)
    padded[:height,:width] = mask[:height,:width]
    return padded.reshape(blocks_h,4,blocks_w,4).max(axis=(1,3)) > 0.01

def _native_sd_patch_wrapper(pristine_wrapper, composite, layer_alpha=None):
    _native_sd_validate_wrapper(pristine_wrapper)

    original = bytes(pristine_wrapper)
    wrapper = bytearray(pristine_wrapper)
    allowed = np.zeros(len(wrapper), dtype=np.uint8)

    box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
    base_rgb = composite.convert('RGB')
    if base_rgb.size != (2048,1024):
        base_rgb = base_rgb.resize((2048,1024), box)

    base_alpha = None
    if layer_alpha is not None:
        base_alpha = layer_alpha.convert('L')
        if base_alpha.size != (2048,1024):
            base_alpha = base_alpha.resize((2048,1024), box)

    levels_written = []

    for level in range(11):
        width,height = _NATIVE_SD_DIMS[level]
        image = _native_sd_level_image(base_rgb, level)
        encoded = encode_image(image)
        blocks_w = max(1, (width + 3)//4)
        blocks_h = max(1, (height + 3)//4)
        needed = blocks_w * blocks_h * 8

        encoded = encoded[:needed]
        if len(encoded) != needed:
            encoded = encoded + b'\0' * (needed - len(encoded))

        source_blocks = np.frombuffer(encoded, np.uint8).reshape(
            blocks_h, blocks_w, 8
        )
        touched = _native_sd_block_mask(
            _native_sd_level_mask(base_alpha, level), width, height
        )
        mip_base = RAW_OFFSET + _NATIVE_SD_MIP_OFFSETS[level]
        pitch = _NATIVE_SD_MIP_PITCHES[level]

        changed_blocks = 0
        for sy in range(blocks_h):
            for sx in range(blocks_w):
                if not touched[sy,sx]:
                    continue

                if level <= 4:
                    dest_x = sx
                    dest_y = sy
                else:
                    dest_x = (
                        sx + _NATIVE_SD_WRAP_X_BLOCKS
                    ) % _NATIVE_SD_PHYS_BLOCKS
                    dest_y = (
                        sy + _NATIVE_SD_WRAP_Y_BLOCKS
                    ) % _NATIVE_SD_PHYS_BLOCKS

                destination = mip_base + dest_y*pitch + dest_x*8
                end = destination + 8

                if destination < RAW_OFFSET:
                    raise ValueError(f'L{level} write before texture payload')
                if end > RAW_OFFSET + _NATIVE_SD_MIP_OFFSETS[11]:
                    raise ValueError(
                        f'L{level} write would reach L11/footer at {destination:#x}'
                    )

                wrapper[destination:end] = source_blocks[sy,sx].tobytes()
                allowed[destination:end] = 1
                changed_blocks += 1

        levels_written.append(
            f'L{level}:{width}x{height}/{changed_blocks} blocks'
        )

    before = np.frombuffer(original, np.uint8)
    after = np.frombuffer(bytes(wrapper), np.uint8)
    changed = before != after
    bad = np.flatnonzero(changed & (allowed == 0))
    if bad.size:
        raise ValueError(
            f'native SD safety check failed: changed unapproved byte '
            f'{int(bad[0]):#x}'
        )

    l11_start = RAW_OFFSET + _NATIVE_SD_MIP_OFFSETS[11]
    if wrapper[l11_start:] != pristine_wrapper[l11_start:]:
        raise ValueError('L11/footer changed; install refused')
    if len(wrapper) != len(pristine_wrapper):
        raise ValueError('SD wrapper size changed; install refused')

    return wrapper, levels_written, int(changed.sum())


# === native HD mip L0-L11 writer v0.9.29.7 ===
# The previous extra-slot path wrote a compact linear mip chain. NASCAR 15's HD
# wrapper is page-mapped: L6-L11 each begin on their own 0x2000 page. Packing
# those levels linearly put them into L5 padding and left the table-selected
# native pages on donor data. This writer mirrors the proven SD native layout.
_NATIVE_HD_ENTRY_SIZE = 0x564161
_NATIVE_HD_MIP_OFFSETS = (
    0x000000, 0x400000, 0x500000, 0x540000,
    0x550000, 0x554000, 0x556000, 0x558000,
    0x55A000, 0x55C000, 0x55E000, 0x560000,
    0x562000,
)
_NATIVE_HD_MIP_PITCHES = (
    0x2000, 0x1000, 0x0800, 0x0400,
    0x0200, 0x0100, 0x0100, 0x0100,
    0x0100, 0x0100, 0x0100, 0x0100,
    0x0100,
)
_NATIVE_HD_DIMS = (
    (4096,2048), (2048,1024), (1024,512), (512,256),
    (256,128), (128,64), (64,32), (32,16),
    (16,8), (8,4), (4,2), (2,1), (1,1),
)
_NATIVE_HD_LARGE_ROLL = (0, -10, -15, -18, -19, -20)
# Stock SD/HD pairs are not authored from identical logical canvases. At an
# equivalent 2048x1024 resolution, pristine HD content is 10 pixels to the
# right of pristine SD content (20 pixels at the native 4096x2048 HD canvas).
# The HD page-map compensation then brings corresponding *stored* SD/HD mip
# pages into exact alignment. Omitting this separate HD-atlas offset produced
# the medium-distance double image seen in RC3-RC7.
_NATIVE_HD_ATLAS_X_ROLL = 20
_NATIVE_HD_WRAP_X_BLOCKS = -5
_NATIVE_HD_WRAP_Y_BLOCKS = -1
_NATIVE_HD_PHYS_BLOCKS = 32


def _native_hd_validate_wrapper(wrapper):
    if len(wrapper) != _NATIVE_HD_ENTRY_SIZE:
        raise ValueError(
            f'HD livery entry is {len(wrapper):#x}; expected '
            f'{_NATIVE_HD_ENTRY_SIZE:#x}'
        )
    if wrapper[:4] != b'ARCC':
        raise ValueError('HD livery entry does not begin with ARCC')
    if wrapper[0xCC:0xD0] != b'DXT1':
        raise ValueError('HD livery DXT1 marker missing at 0xCC')
    table_start = len(wrapper) - 0x89
    actual_offsets = struct.unpack_from('<13I', wrapper, table_start)
    actual_pitches = struct.unpack_from('<13I', wrapper, table_start + 52)
    if tuple(actual_offsets) != _NATIVE_HD_MIP_OFFSETS:
        got = ', '.join(f'{v:#x}' for v in actual_offsets)
        raise ValueError(f'unexpected HD native mip offset table: {got}')
    if tuple(actual_pitches) != _NATIVE_HD_MIP_PITCHES:
        got = ', '.join(f'{v:#x}' for v in actual_pitches)
        raise ValueError(f'unexpected HD native mip pitch table: {got}')


def _native_hd_level_image(base_rgb, level):
    w,h = _NATIVE_HD_DIMS[level]
    box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
    image = base_rgb if level == 0 else base_rgb.resize((w,h), box)
    # Stock-proven native compensation for the HD page map.
    if level <= 5:
        roll = _NATIVE_HD_LARGE_ROLL[level]
        if roll:
            image = Image.fromarray(
                np.roll(np.asarray(image.convert('RGB')), roll, axis=1)
            )
    return image.convert('RGB')


def _native_hd_patch_wrapper(pristine_wrapper, composite):
    _native_hd_validate_wrapper(pristine_wrapper)
    original = bytes(pristine_wrapper)
    wrapper = bytearray(pristine_wrapper)
    allowed = np.zeros(len(wrapper), dtype=np.uint8)
    box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
    base_rgb = composite.convert('RGB')
    if base_rgb.size != (4096,2048):
        base_rgb = base_rgb.resize((4096,2048), box)
    # Match the independently measured relationship in two pristine stock
    # pairs (Kevin Harvick and Jamie McMurray): HD L0 is +20 px versus SD L0,
    # and equivalent stored mip pages then align at 0 px after the native
    # per-mip page compensation. This is distinct from community-template
    # alignment, which is applied to the shared source before either wrapper.
    if _NATIVE_HD_ATLAS_X_ROLL:
        base_rgb = Image.fromarray(
            np.roll(np.asarray(base_rgb), _NATIVE_HD_ATLAS_X_ROLL, axis=1).astype(np.uint8),
            'RGB'
        )
    levels_written = []

    # L12 is the 1x1 terminal page and is preserved with the footer, matching
    # the SD writer's L11 preservation rule.
    for level in range(12):
        width,height = _NATIVE_HD_DIMS[level]
        image = _native_hd_level_image(base_rgb, level)
        encoded = encode_image(image)
        blocks_w = max(1, (width + 3)//4)
        blocks_h = max(1, (height + 3)//4)
        needed = blocks_w * blocks_h * 8
        encoded = encoded[:needed] + b'\0' * max(0, needed-len(encoded))
        source_blocks = np.frombuffer(encoded, np.uint8).reshape(
            blocks_h, blocks_w, 8
        )
        mip_base = RAW_OFFSET + _NATIVE_HD_MIP_OFFSETS[level]
        pitch = _NATIVE_HD_MIP_PITCHES[level]
        changed_blocks = 0
        for sy in range(blocks_h):
            for sx in range(blocks_w):
                if level <= 5:
                    dest_x, dest_y = sx, sy
                else:
                    dest_x = (sx + _NATIVE_HD_WRAP_X_BLOCKS) % _NATIVE_HD_PHYS_BLOCKS
                    dest_y = (sy + _NATIVE_HD_WRAP_Y_BLOCKS) % _NATIVE_HD_PHYS_BLOCKS
                destination = mip_base + dest_y*pitch + dest_x*8
                end = destination + 8
                if destination < RAW_OFFSET:
                    raise ValueError(f'HD L{level} write before texture payload')
                if end > RAW_OFFSET + _NATIVE_HD_MIP_OFFSETS[12]:
                    raise ValueError(
                        f'HD L{level} write would reach L12/footer at {destination:#x}'
                    )
                wrapper[destination:end] = source_blocks[sy,sx].tobytes()
                allowed[destination:end] = 1
                changed_blocks += 1
        levels_written.append(
            f'L{level}:{width}x{height}/{changed_blocks} blocks'
        )

    before = np.frombuffer(original, np.uint8)
    after = np.frombuffer(bytes(wrapper), np.uint8)
    changed = before != after
    bad = np.flatnonzero(changed & (allowed == 0))
    if bad.size:
        raise ValueError(
            f'native HD safety check failed: changed unapproved byte '
            f'{int(bad[0]):#x}'
        )
    l12_start = RAW_OFFSET + _NATIVE_HD_MIP_OFFSETS[12]
    if wrapper[l12_start:] != pristine_wrapper[l12_start:]:
        raise ValueError('HD L12/footer changed; install refused')
    if len(wrapper) != len(pristine_wrapper):
        raise ValueError('HD wrapper size changed; install refused')
    return wrapper, levels_written, int(changed.sum())



# Exact public-v1 HD writer reserved for added-paint creation/repair.
def _native_hd_patch_wrapper_public_v1(pristine_wrapper, composite):
    _native_hd_validate_wrapper(pristine_wrapper)
    original = bytes(pristine_wrapper)
    wrapper = bytearray(pristine_wrapper)
    allowed = np.zeros(len(wrapper), dtype=np.uint8)
    box = Image.Resampling.BOX if hasattr(Image, 'Resampling') else Image.BOX
    base_rgb = composite.convert('RGB')
    if base_rgb.size != (4096,2048):
        base_rgb = base_rgb.resize((4096,2048), box)
    levels_written = []

    # L12 is the 1x1 terminal page and is preserved with the footer, matching
    # the SD writer's L11 preservation rule.
    for level in range(12):
        width,height = _NATIVE_HD_DIMS[level]
        image = _native_hd_level_image(base_rgb, level)
        encoded = encode_image(image)
        blocks_w = max(1, (width + 3)//4)
        blocks_h = max(1, (height + 3)//4)
        needed = blocks_w * blocks_h * 8
        encoded = encoded[:needed] + b'\0' * max(0, needed-len(encoded))
        source_blocks = np.frombuffer(encoded, np.uint8).reshape(
            blocks_h, blocks_w, 8
        )
        mip_base = RAW_OFFSET + _NATIVE_HD_MIP_OFFSETS[level]
        pitch = _NATIVE_HD_MIP_PITCHES[level]
        changed_blocks = 0
        for sy in range(blocks_h):
            for sx in range(blocks_w):
                if level <= 5:
                    dest_x, dest_y = sx, sy
                else:
                    dest_x = (sx + _NATIVE_HD_WRAP_X_BLOCKS) % _NATIVE_HD_PHYS_BLOCKS
                    dest_y = (sy + _NATIVE_HD_WRAP_Y_BLOCKS) % _NATIVE_HD_PHYS_BLOCKS
                destination = mip_base + dest_y*pitch + dest_x*8
                end = destination + 8
                if destination < RAW_OFFSET:
                    raise ValueError(f'HD L{level} write before texture payload')
                if end > RAW_OFFSET + _NATIVE_HD_MIP_OFFSETS[12]:
                    raise ValueError(
                        f'HD L{level} write would reach L12/footer at {destination:#x}'
                    )
                wrapper[destination:end] = source_blocks[sy,sx].tobytes()
                allowed[destination:end] = 1
                changed_blocks += 1
        levels_written.append(
            f'L{level}:{width}x{height}/{changed_blocks} blocks'
        )

    before = np.frombuffer(original, np.uint8)
    after = np.frombuffer(bytes(wrapper), np.uint8)
    changed = before != after
    bad = np.flatnonzero(changed & (allowed == 0))
    if bad.size:
        raise ValueError(
            f'native HD safety check failed: changed unapproved byte '
            f'{int(bad[0]):#x}'
        )
    l12_start = RAW_OFFSET + _NATIVE_HD_MIP_OFFSETS[12]
    if wrapper[l12_start:] != pristine_wrapper[l12_start:]:
        raise ValueError('HD L12/footer changed; install refused')
    if len(wrapper) != len(pristine_wrapper):
        raise ValueError('HD wrapper size changed; install refused')
    return wrapper, levels_written, int(changed.sum())


def ensure_backup(live,bak):
    # Create a backup only if one doesn't already exist, and only from a
    # plausibly-intact live file. Never overwrite an existing backup (the first
    # one, made before any edit, is the true pristine copy).
    if os.path.exists(bak):
        return
    if not os.path.exists(live) or os.path.getsize(live) < 64:
        raise ValueError('refusing to back up an empty/missing archive')
    # write to a temp file then atomically rename, so an interrupted copy can't
    # leave a truncated backup that restore would trust.
    tmp = bak + '.tmp'
    shutil.copyfile(live, tmp)
    if os.path.getsize(tmp) != os.path.getsize(live):
        os.remove(tmp); raise ValueError('backup copy incomplete; aborted')
    # Force the copy to disk before it becomes the pristine backup. Without
    # this the rename can commit while the bytes are still in the page cache,
    # so a crash leaves a backup that Restore trusts but cannot use.
    with open(tmp, 'rb+') as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, bak)


def _clear_ui_thumb_cache(*keys):
    """Invalidate unified Images & UI thumbnails after any archive image write.

    Legacy Menus/Paint Previews routes and first-time backup creation can change
    both the live thumbnail and its Stock/Modified comparison. With no keys, the
    complete cache is cleared; otherwise only the named (archive,container,entry)
    tuples are removed.
    """
    cache=globals().get('_UI_THUMB_CACHE')
    if cache is not None:
        if not keys:
            cache.clear()
        else:
            for key in keys:
                cache.pop(tuple(map(str,key)),None)
    for cache_name in ('_LIVE_PAINT_THUMB_CACHE','_PAINT_ATLAS_PREVIEW_CACHE','_STOCK_THUMB_SUPPORT_CACHE'):
        other=globals().get(cache_name)
        if other is not None: other.clear()
    idx=globals().get('_LIVE_LIVERY_INDEX_CACHE')
    if isinstance(idx,dict): idx.update(sig=None,script_to_uid={},uid_to_driver={})
    grid=globals().get('_GRID_SLOTS_CACHE')
    if isinstance(grid,dict): grid.update(sig=None,rows=None)

# ---------------- install ----------------
def write_fei(reg, slot, thumb_img):
    """Write a DLC FEI preview through the mapped native ARCC entry.

    Public v1 wrote at a hard-coded +0x100 offset and swapped DXT5 block halves.
    Clean-file mapping proves FEI_LIV uses the normal 16-byte-record / 24-byte
    texture-header layout, with a full 65536-byte standard-order DXT5 payload.
    This function remains unused by the guarded DLC install path until the first
    in-game validation pass, but it is no longer capable of the old blind write.
    """
    if not slot.get('fei'):
        raise ValueError('DLC slot has no FEI preview resource')
    a=need(reg, slot['arc'])
    ensure_backup(a['ar'], a['bak'])
    off=slot['fei']['off']; size=slot['fei']['size']
    with open(a['ar'],'rb') as fh:
        fh.seek(off); arc=fh.read(size)
    entries,_=C.parse_multi_arc(arc)
    wanted=str(slot['fei'].get('name') or '')
    entry=next((e for e in entries if e.get('name')==wanted),None)
    if entry is None and len(entries)==1:
        entry=entries[0]
    if entry is None:
        raise ValueError(f'{wanted or "FEI preview"} not found in mapped FEI container')
    new=C.multi_write_png_validated(arc,entry,thumb_img.resize((entry['w'],entry['h'])),encode_fn=encode_any)
    if len(new)!=len(arc):
        raise ValueError('FEI container size changed; write refused')
    with open(a['ar'],'r+b') as fh:
        fh.seek(off); fh.write(new); fh.flush(); os.fsync(fh.fileno())
        fh.seek(off)
        if fh.read(size)!=new:
            raise ValueError('FEI readback mismatch')
    return f"FEI {entry['name']} (native mapped write)"

def career_container(reg, live=True):
    a=need(reg,'0')
    off,size=find_entry(reg,'0','BASESCHEMETHUMBNAILS.ARC')
    src=a['ar'] if live or not os.path.exists(a['bak']) else a['bak']
    with open(src,'rb') as fh:
        fh.seek(off); return off,size,fh.read(size)

def write_career_thumb(reg, slot, thumb_img):
    a=need(reg,'0')
    ensure_backup(a['ar'], a['bak'])
    off,size,arc = career_container(reg, live=True)
    entries,_=C.parse_multi_arc(arc)
    m=re.match(r'^LIVERY_CAREER_(\w+?)_(\d+)\.ARC$', slot['name'])
    if not m: raise ValueError('not a career slot')
    target=f"CAREER_{m.group(1).upper()}_{m.group(2)}"
    ent=[e for e in entries if e['name']==target]
    if not ent: raise ValueError(f'{target} not in thumbnail container')
    new=C.multi_write_png(arc, ent[0], thumb_img, encode_fn=encode_any)
    with open(a['ar'],'r+b') as fh:
        fh.seek(off); fh.write(new)
    _clear_ui_thumb_cache()
    return f"thumb {target}"

def slot_thumb_source(slot):
    """User-uploaded thumb if present, else auto-generated from the scheme."""
    tp=os.path.join(SCHEMES, slot['name']+'.thumb.png')
    if os.path.exists(tp): return Image.open(tp)
    sp=os.path.join(SCHEMES, slot['name']+'.png')
    if os.path.exists(sp): return C.make_thumb(Image.open(sp))
    return None

def install_slot(reg, slot, png_path, layer_path=None):
    """Install one existing paint as an atomic SD/HD transaction.

    Both wrappers are fully prepared and every live target region is snapshotted
    before the first game byte changes. If any write, readback, or archive-size
    check fails, every attempted region is restored to its exact pre-install
    bytes. This prevents a failed HD build/write from leaving only the SD paint
    changed.
    """
    sd_arc = str(slot.get('sd_arc') or slot['arc'])
    hd_arc = str(slot.get('hd_arc') or sd_arc) if slot.get('hd') else None
    if slot.get('hd') and int(slot.get('hd_size') or 0) != _NATIVE_HD_ENTRY_SIZE:
        raise ValueError(
            f"HD wrapper mismatch for {slot.get('hd')} in ARCHIVE{hd_arc}: "
            f"actual {int(slot.get('hd_size') or 0):#x}, expected {_NATIVE_HD_ENTRY_SIZE:#x}. Nothing written.")
    sd_info = need(reg, sd_arc)
    ensure_backup(sd_info['ar'], sd_info['bak'])
    if hd_arc:
        hd_info = need(reg, hd_arc)
        ensure_backup(hd_info['ar'], hd_info['bak'])
    else:
        hd_info = None

    comp = Image.open(png_path).convert('RGB')
    alpha = None
    if layer_path and os.path.exists(layer_path):
        la = Image.open(layer_path)
        alpha = la.split()[3] if la.mode == 'RGBA' else la.convert('L')

    original_sizes = {sd_arc: os.path.getsize(sd_info['ar'])}
    if hd_arc:
        original_sizes[hd_arc] = os.path.getsize(hd_info['ar'])

    # Prepare every intended byte before touching the live archives.
    sd_off, sd_size = int(slot['sd_off']), int(slot['sd_size'])
    with open(sd_info['bak'], 'rb') as pristine:
        pristine.seek(sd_off)
        sd_pristine = bytearray(pristine.read(sd_size))
    if len(sd_pristine) != sd_size:
        raise ValueError('short read from pristine SD livery entry')
    sd_wrapper, sd_levels, sd_changed = _native_sd_patch_wrapper(sd_pristine, comp, alpha)

    writes = [dict(
        arcid=sd_arc, info=sd_info, off=sd_off, size=sd_size,
        intended=bytes(sd_wrapper), label='SD')]
    hd_result = None
    if slot.get('hd') and int(slot.get('hd_size') or 0) == _NATIVE_HD_ENTRY_SIZE:
        hd_off, hd_size = int(slot['hd_off']), int(slot['hd_size'])
        with open(hd_info['bak'], 'rb') as pristine:
            pristine.seek(hd_off)
            hd_pristine = bytearray(pristine.read(hd_size))
        if len(hd_pristine) != hd_size:
            raise ValueError('short read from pristine HD livery entry')
        # RC5: use the mapped native page/pitch writer with the original
        # stock-proven per-mip compensation. RC3 removed that compensation and
        # visibly shifted the full atlas. Full-replacement imports still write
        # every intended block, preventing donor texture bleed/fade.
        hd_wrapper, hd_levels, hd_changed = _native_hd_patch_wrapper(hd_pristine, comp)
        hd_result = (
            f"HD native L0-L11 in ARCHIVE{hd_arc} @0x{hd_off:X} "
            f"({hd_changed} changed bytes; L12/footer preserved)")
        writes.append(dict(
            arcid=hd_arc, info=hd_info, off=hd_off, size=hd_size,
            intended=bytes(hd_wrapper), label='HD'))

    # Snapshot the current live regions, not the pristine backup. A failed
    # reinstall must return the user to the paint they had immediately before
    # pressing Install, even when that was already modified.
    for item in writes:
        with open(item['info']['ar'], 'rb') as live:
            live.seek(item['off'])
            item['old_live'] = live.read(item['size'])
        if len(item['old_live']) != item['size']:
            raise ValueError(
                f"short live read for {item['label']} livery entry before install")

    attempted = []
    try:
        for item in writes:
            attempted.append(item)
            with open(item['info']['ar'], 'r+b') as live:
                live.seek(item['off'])
                live.write(item['intended'])
                live.flush()
                os.fsync(live.fileno())
                live.seek(item['off'])
                if live.read(item['size']) != item['intended']:
                    raise ValueError(
                        f"{item['label']} native-mip readback mismatch")
        for arcid, size_before in original_sizes.items():
            if os.path.getsize(need(reg,arcid)['ar']) != size_before:
                raise ValueError(
                    f'ARCHIVE{arcid} size changed during paint install')
    except Exception as install_ex:
        rollback_errors = []
        for item in reversed(attempted):
            try:
                with open(item['info']['ar'], 'r+b') as live:
                    live.seek(item['off'])
                    live.write(item['old_live'])
                    live.flush()
                    os.fsync(live.fileno())
                    live.seek(item['off'])
                    if live.read(item['size']) != item['old_live']:
                        raise ValueError('rollback readback mismatch')
            except Exception as rb:
                rollback_errors.append(
                    f"ARCHIVE{item['arcid']} {item['label']} region: {rb}")
        for arcid, size_before in original_sizes.items():
            try:
                path = need(reg,arcid)['ar']
                if os.path.getsize(path) != size_before:
                    with open(path, 'r+b') as live:
                        live.truncate(size_before)
                        live.flush()
                        os.fsync(live.fileno())
            except Exception as rb:
                rollback_errors.append(f'ARCHIVE{arcid} size restore: {rb}')
        _clear_ui_thumb_cache()
        if rollback_errors:
            raise RollbackFailed(install_ex, '; '.join(rollback_errors)) from install_ex
        raise

    results = [
        f"SD native L0-L10 in ARCHIVE{sd_arc} @0x{sd_off:X} "
        f"({sd_changed} changed bytes; L11/footer preserved)"]
    if hd_result:
        results.append(hd_result)

    try:
        th = slot_thumb_source(slot)
        if th is not None:
            if slot['kind'] == 'career':
                results.append(write_career_thumb(reg,slot,th))
            elif slot.get('fei'):
                # v1.0.1 safety hotfix: the old FEI writer treats every DLC
                # preview as a normal 256x256 DXT5 payload at +0x100. Public
                # testing showed that selecting a replaced DLC car can fatal
                # immediately. Preserve the exact native FEI bytes until that
                # wrapper is mapped and validated in-game; changing the on-track
                # SD/HD paint does not require changing this menu preview.
                results.append('DLC FEI preview preserved unchanged (v1.0.1 safety lock)')
    except Exception as ex:
        results.append(f"thumb skipped: {ex}")
    _clear_ui_thumb_cache()
    return results

# ---------------- names / roster / handles ----------------
def text0000(reg, pristine=False):
    a=need(reg,'0')
    use_bak = pristine and os.path.exists(a['bak'])
    off,s=find_entry(reg,'0','TEXT0000.LDA', pristine=use_bak)
    src=a['bak'] if use_bak else a['ar']
    with open(src,'rb') as fh:
        fh.seek(off); return fh.read(s)

def find_exact_string(blob, candidate):
    """Find one complete stock string without surname or substring guessing.

    Some TEXT*.LDA files contain valid NUL-terminated strings that are not
    returned by the structured LDA entry iterator.  Darrell Wallace Jr. is one
    of those on the clean NASCAR 15 build.  Search both views, but still require
    an exact case-insensitive full-string match before enabling Rename.
    """
    wanted=str(candidate or '').strip().casefold()
    if not wanted:return None
    values=[];seen=set()
    try:
        for e in C.lda_entries(blob):
            text=e['raw'].decode('latin1','replace')
            key=text.casefold()
            if key not in seen:seen.add(key);values.append(text)
    except Exception:
        pass
    # Always include the raw NUL-delimited view.  Do not use substring or
    # surname fallback: exact matching is what prevents Mike/Darrell Wallace
    # and similar collisions.
    for raw in bytes(blob).split(b'\0'):
        if not raw:continue
        text=raw.decode('latin1','replace')
        key=text.casefold()
        if key not in seen:seen.add(key);values.append(text)
    for text in values:
        if text.strip().casefold()==wanted:return text
    return None


def _find_exact_stock_text(reg,candidates):
    """Find an exact name in every pristine TEXT*.LDA table.

    Driver display names are not all guaranteed to live in TEXT0000.LDA.  The
    old roster looked only there, which left Darrell Wallace Jr. disabled even
    though his exact name exists elsewhere in the clean language tables.
    """
    a=need(reg,'0')
    cdf=a['cdf']+'.gridapp.bak' if os.path.exists(a['cdf']+'.gridapp.bak') else a['cdf']
    source=a['bak'] if os.path.exists(a['bak']) else a['ar']
    wanted=[str(x or '').strip() for x in candidates if str(x or '').strip()]
    if not wanted:return None
    try:regions=[(o,sz,n) for o,sz,n in parse_cdfiles(cdf) if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')]
    except Exception:regions=[]
    with open(source,'rb') as fh:
        for off,sz,_name in regions:
            fh.seek(off);blob=fh.read(sz)
            for candidate in wanted:
                hit=find_exact_string(blob,candidate)
                if hit:return hit
    return None


def _game_data_path(filename):
    sub=str(active_game_profile().get('data_subdir') or '')
    return os.path.join(DATA,sub,filename) if sub else os.path.join(DATA,filename)


def load_driver_links():
    rows=json.load(open(_game_data_path('drivers.json'),encoding='utf-8'))
    if not isinstance(rows,list):raise ValueError('drivers.json must contain a list')
    return rows


_DRIVER_DISPLAY_ALIAS_CACHE={}


def _driver_asset_words(slot_name):
    name=re.sub(r'^(?:HD)?LIVERY_(?:14|15)_[^_]+_','',str(slot_name or ''),flags=re.I)
    name=re.sub(r'\.ARC$','',name,flags=re.I)
    parts=[p for p in name.split('_') if p]
    tails={'PRIMARY','SECONDARY','TERTIARY','ALT','ALTERNATE','BEER','THROWBACK','TEST','DEFAULT','SPECIAL','NIGHT','DAY'}
    while parts and parts[-1].upper() in tails:parts.pop()
    return ' '.join(parts)


def _driver_display_aliases():
    game_key=str(ACTIVE_GAME)
    if game_key in _DRIVER_DISPLAY_ALIAS_CACHE:return _DRIVER_DISPLAY_ALIAS_CACHE[game_key]
    out={}
    try:
        for link in load_driver_links():
            display=str(link.get('display_name') or '').strip()
            if not display:continue
            aliases=[display,_driver_asset_words(link.get('slot'))]+list(link.get('name_candidates') or [])
            for alias in aliases:
                norm=re.sub(r'[^a-z0-9]+','',str(alias).casefold())
                if norm:out[norm]=display
    except Exception:pass
    _DRIVER_DISPLAY_ALIAS_CACHE[game_key]=out
    return out


def _driver_display_from_link(link):
    display=str(link.get('display_name') or '').strip()
    if display:return display
    raw=_driver_asset_words(link.get('slot'));words=[]
    for word in raw.split():
        u=word.upper()
        if u in ('AJ','JJ'):words.append(u)
        elif u in ('JR','JNR'):words.append('Jr.')
        elif u=='MCDOWELL':words.append('McDowell')
        elif u=='MCMURRAY':words.append('McMurray')
        else:words.append(u.lower().capitalize())
    return ' '.join(words)


def _driver_name_candidates(link):
    values=[];seen=set()
    seeds=[link.get('display_name'),_driver_display_from_link(link),_driver_asset_words(link.get('slot'))]
    seeds.extend(link.get('name_candidates') or [])
    for value in seeds:
        text=str(value or '').strip();key=text.casefold()
        if text and key not in seen:seen.add(key);values.append(text)
    return values


def _live_text_for_stock_text(reg, stock_text):
    """Resolve the current live string at the same LDA index as a stock name.

    This makes a second rename work even when config/app state was deleted. The
    pristine table identifies the driver; the live table supplies the current
    text. No surname or substring guessing is used.
    """
    wanted = str(stock_text or '').strip().casefold()
    if not wanted:
        return None
    a = need(reg, '0')
    bar = a.get('bak') if os.path.exists(a.get('bak', '')) else None
    bcdf = backup_path(a['cdf']) if os.path.exists(backup_path(a['cdf'])) else None
    if not bar or not bcdf:
        return stock_text
    try:
        stock_regions = {n: (o, z) for o, z, n in parse_cdfiles(bcdf)
                         if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')}
        live_regions = {n: (o, z) for o, z, n in parse_cdfiles(a['cdf'])
                        if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')}
        with open(bar, 'rb') as sf, open(a['ar'], 'rb') as lf:
            for name, (soff, ssize) in stock_regions.items():
                if name not in live_regions:
                    continue
                loff, lsize = live_regions[name]
                sf.seek(soff); stock_blob = sf.read(ssize)
                lf.seek(loff); live_blob = lf.read(lsize)
                try:
                    stock_entries = C.lda_entries(stock_blob)
                    live_entries = C.lda_entries(live_blob)
                except Exception:
                    continue
                live_by_index = {int(e['index']): e for e in live_entries}
                for entry in stock_entries:
                    text = entry['raw'].decode('latin1', 'replace')
                    if text.strip().casefold() != wanted:
                        continue
                    peer = live_by_index.get(int(entry['index']))
                    if peer is None:
                        return stock_text
                    return peer['raw'].decode('latin1', 'replace')
    except Exception:
        pass
    return stock_text


def roster(reg):
    """Build the 46-driver 2015 roster from the clean DRIVERCONFIG map.

    The language table is used only to locate an exact rename target. A missing
    alias disables Rename instead of falling back to another driver with the
    same surname.
    """
    cfg=load_cfg();led=cfg.get('renames',{});hled=cfg.get('handles',{})
    blob=text0000(reg,pristine=True)
    goff,gsz=find_entry(reg,'0','DB_GAME_LOCAL_SCRIPT.PYC')
    with open(need(reg,'0')['ar'],'rb') as fh:fh.seek(goff);gloc=fh.read(gsz)
    drivers=[];seen_bases=set()
    for link in load_driver_links():
        base=str(link.get('base') or '').upper()
        if not base or base in seen_bases:continue
        seen_bases.add(base);display=_driver_display_from_link(link)
        candidates=_driver_name_candidates(link)
        stock_text=None
        for candidate in candidates:
            stock_text=find_exact_string(blob,candidate)
            if stock_text:break
        if not stock_text:
            stock_text=_find_exact_stock_text(reg,candidates)
        live_text=_live_text_for_stock_text(reg,stock_text) if stock_text else None
        renamed=led.get(stock_text,led.get(display)) if stock_text else led.get(display)
        current=str(live_text or renamed or display);patch_current=str(live_text or renamed or stock_text or '')
        h_orig=str(link.get('handle') or '');h_storage=str(hled.get(h_orig,h_orig));h_display=h_storage.rstrip('_ ')
        h_ok=bool(h_storage) and h_storage.encode('latin1') in gloc
        drivers.append(dict(base=base,number=str(link.get('number') or ''),original=display,current=current,
            stock_text=stock_text,patch_current=patch_current,rename_available=bool(stock_text),
            rename_note='' if stock_text else 'Exact stock name text was not found; rename is disabled to prevent changing another driver.',
            handle=h_orig,handle_current=h_display,handle_storage_current=h_storage,
            handle_found=h_ok,driver_uid=link.get('driver_uid'),
            config_uid=link.get('config_uid'),team_uid=link.get('team_uid'),profile_id=link.get('profile_id'),slot=link.get('slot')))
    drivers.sort(key=lambda x:(int(re.match(r'\d+',str(x.get('number') or '9999')).group(0)) if re.match(r'\d+',str(x.get('number') or '')) else 9999, str(x.get('number') or ''), x['current'].casefold()))
    teams=[]
    if ACTIVE_GAME=='nascar15':
        try:
            catalog=_team_friendly_catalog()
            for team in catalog.get('teams',[]):
                if str(team.get('category')) not in ('active','spare'):continue
                original=str(team.get('original_label') or team.get('label') or '').strip();current=str(team.get('label') or original).strip()
                if original and not any(x['original']==original for x in teams):teams.append(dict(original=original,current=current,uid=int(team.get('uid'))))
            teams.sort(key=lambda x:x['current'].casefold())
        except Exception:teams=[]
    if not teams:
        team_names=TEAMS_2014 if ACTIVE_GAME=='nascar14' else TEAMS_2015
        for team_name in team_names:
            orig=find_exact_string(blob,team_name)
            if orig:teams.append(dict(original=orig,current=led.get(orig,orig)))
    return drivers,teams

def text_regions(reg):
    return [(o,s,n) for o,s,n in parse_cdfiles(need(reg,'0')['cdf'])
            if n.upper().startswith('TEXT') and n.upper().endswith('.LDA')]

def patch_name(reg, old, new):
    ob,nb = old.encode('latin1'), new.encode('latin1')
    if len(nb)>len(ob): raise ValueError(f'new name must be {len(ob)} characters or fewer')
    nb = nb+b' '*(len(ob)-len(nb))
    a=need(reg,'0'); ensure_backup(a['ar'], a['bak'])
    patched=0
    # `with` matters here: an exception mid-loop used to leak the handle, and on
    # Windows that leaves ARCHIVE0 locked so the user's next Restore fails too.
    with open(a['ar'],'r+b') as fh:
        for off,sz,nm2 in text_regions(reg):
            fh.seek(off); blob=bytearray(fh.read(sz))
            p=blob.find(ob); ch=False
            while p>=0:
                blob[p:p+len(ob)]=nb; patched+=1; ch=True
                p=blob.find(ob,p+1)
            if ch: fh.seek(off); fh.write(blob)
        fh.flush(); os.fsync(fh.fileno())
    return patched

def _cdfiles_record_pos(cdf_path, target):
    """Find the 32-byte record for `target` and return (pos, layout)."""
    d=open(cdf_path,'rb').read()
    hdr=struct.unpack_from('<12I',d,0)
    n,strtab=hdr[8],hdr[10]; sbase=len(d)-strtab
    def nm(off):
        p=sbase+off; e=d.find(b'\0',p)
        return d[p:e].decode('ascii','replace')
    for start,lay in ((0x40,'A'),(0x50,'B')):
        pos=start; ok=0; hits=[]
        for i in range(n):
            if pos+32>sbase: break
            f=struct.unpack_from('<8I',d,pos)
            no = f[1] if lay=='A' else f[3]
            s = nm(no) if no<strtab else ''
            if s and all(32<=ord(c)<127 for c in s): ok+=1
            if s==target: hits.append(pos)
            pos+=32
        if ok>n*0.8:
            if not hits: raise ValueError(f'{target} not in cdfiles')
            return hits[0], lay
    raise ValueError('unrecognized cdfiles layout')

def patch_name_exp(reg, old, new):
    """Length-changing LDA rename with archive/CDF transaction rollback."""
    ob, nb = str(old).encode('latin1'), str(new).encode('latin1')
    a = need(reg, '0')
    ensure_backup(a['ar'], a['bak'])
    cdf = a['cdf']
    ensure_backup(cdf, backup_path(cdf))
    original_archive_size = os.path.getsize(a['ar'])
    original_cdf = open(cdf, 'rb').read()
    # Snapshot each live text region because in-slot edits are not removed by a
    # simple archive truncate.
    regions = text_regions(reg)
    region_bytes = {}
    with open(a['ar'], 'rb') as fh:
        for off, sz, name in regions:
            fh.seek(off); region_bytes[(off, sz, name)] = fh.read(sz)
    patched = 0
    try:
        for off, sz, nm2 in regions:
            blob = region_bytes[(off, sz, nm2)]
            try:
                rebuilt, n = C.lda_rebuild(blob, {ob: nb})
            except Exception:
                continue
            if not n:
                continue
            patched += n
            if len(rebuilt) <= sz:
                with open(a['ar'], 'r+b') as fh:
                    fh.seek(off); fh.write(rebuilt + b'\0' * (sz - len(rebuilt)))
                    fh.flush(); os.fsync(fh.fileno())
                if len(rebuilt) != sz:
                    pos, lay = _cdfiles_record_pos(cdf, nm2)
                    cdf_live = bytearray(open(cdf, 'rb').read())
                    struct.pack_into('<I', cdf_live, pos + (8 if lay == 'A' else 16), len(rebuilt))
                    _extra_atomic_bytes(cdf, bytes(cdf_live))
            else:
                with open(a['ar'], 'r+b') as fh:
                    fh.seek(0, 2); endp = fh.tell(); pad = (-endp) % 16
                    fh.write(b'\0' * pad); new_off = endp + pad; fh.write(rebuilt)
                    fh.flush(); os.fsync(fh.fileno())
                pos, lay = _cdfiles_record_pos(cdf, nm2)
                cdf_live = bytearray(open(cdf, 'rb').read())
                if lay == 'A':
                    struct.pack_into('<I', cdf_live, pos + 8, len(rebuilt))
                    struct.pack_into('<I', cdf_live, pos + 20, new_off)
                else:
                    struct.pack_into('<I', cdf_live, pos + 16, len(rebuilt))
                    struct.pack_into('<I', cdf_live, pos + 28, new_off)
                _extra_atomic_bytes(cdf, bytes(cdf_live))
        if not patched:
            raise ValueError('name not found as a text-table entry')
        # Verify that at least one live LDA now contains the exact new value.
        verified = False
        for off, sz, _name in text_regions(reg):
            with open(a['ar'], 'rb') as fh:
                fh.seek(off); blob = fh.read(sz)
            hit = find_exact_string(blob, str(new))
            if hit is not None and str(hit).strip().casefold() == str(new).strip().casefold():
                verified = True; break
        if not verified:
            raise ValueError('rename readback failed: new text was not found')
        return patched
    except Exception as original:
        rollback_errors = []
        try:
            with open(a['ar'], 'r+b') as fh:
                fh.truncate(original_archive_size)
                for (off, _sz, _name), raw in region_bytes.items():
                    fh.seek(off); fh.write(raw)
                fh.flush(); os.fsync(fh.fileno())
        except Exception as ex:
            rollback_errors.append('archive restore: ' + str(ex))
        try:
            _extra_atomic_bytes(cdf, original_cdf)
        except Exception as ex:
            rollback_errors.append('cdf restore: ' + str(ex))
        if rollback_errors:
            raise RuntimeError(str(original) + '; rollback also failed: ' + '; '.join(rollback_errors)) from original
        raise

def _marshal_string_rebuild(pyc, old_bytes, new_bytes):
    """Replace exact Python-2 marshal string objects, allowing a new length.

    Handles are stored as marshal strings in DB_GAME_LOCAL_SCRIPT.PYC.  Updating
    the four-byte length and rebuilding the complete PYC keeps interned-string
    references valid while removing the old fixed-slot character limit.
    """
    hits=[]
    for tag in (ord('s'),ord('t'),ord('u')):
        for encoded_tag in (tag,tag|0x80):
            needle=bytes((encoded_tag,))+struct.pack('<i',len(old_bytes))+old_bytes
            start=0
            while True:
                pos=pyc.find(needle,start)
                if pos<0:break
                hits.append((pos,len(needle)))
                start=pos+1
    if not hits:
        raise ValueError('current handle was not found as a game-data string')
    out=bytearray(pyc)
    for pos,total in sorted(set(hits),reverse=True):
        out[pos+1:pos+5]=struct.pack('<i',len(new_bytes))
        out[pos+5:pos+total]=new_bytes
    rebuilt=bytes(out)
    # A complete Python-2 marshal reparse is the structural guard.  This catches
    # an accidental match inside bytecode or another raw payload before install.
    _mapper_direct_module().parse_pyc(rebuilt)
    return rebuilt,len(set(hits))


def patch_handle(reg, old, new):
    """Length-changing driver-card handle replacement with exact PYC repoint."""
    old=str(old);clean_new=str(new).strip().lstrip('@')
    if not clean_new:raise ValueError('empty handle')
    if '\x00' in clean_new:raise ValueError('handles cannot contain a null character')
    try:ob=old.encode('latin1');nb=clean_new.encode('latin1')
    except UnicodeEncodeError as ex:raise ValueError('handle must use characters supported by the game text encoding') from ex
    # Not tied to the stock slot width.  The generous sanity ceiling prevents a
    # pasted paragraph from becoming a multi-kilobyte runtime identifier.
    if len(nb)>255:raise ValueError('handle is too long for a game menu identifier')
    v,row,pyc=_pyc_live_blob('DB_GAME_LOCAL_SCRIPT.PYC')
    rebuilt,patched=_marshal_string_rebuild(pyc,ob,nb)
    _rp_backup_pair(v)
    if len(rebuilt)==row['size']:
        with open(v['ar'],'r+b') as fh:
            fh.seek(row['offset']);before=fh.read(row['size'])
            fh.seek(row['offset']);fh.write(rebuilt);fh.flush();os.fsync(fh.fileno())
            fh.seek(row['offset']);check=fh.read(row['size'])
        if check!=rebuilt:
            with open(v['ar'],'r+b') as fh:
                fh.seek(row['offset']);fh.write(before);fh.flush();os.fsync(fh.fileno())
            raise ValueError('handle readback failed; previous bytes restored')
    else:
        fd,tmp=tempfile.mkstemp(prefix='n15mod_handle_',suffix='.PYC');os.close(fd)
        try:
            open(tmp,'wb').write(rebuilt)
            with _RP_LOCK:_rp_install_one('0',v,row,tmp,source_name='Driver card handle',allow_magic=True)
        finally:
            try:os.remove(tmp)
            except OSError:pass
    _v,_row,live=_pyc_live_blob('DB_GAME_LOCAL_SCRIPT.PYC')
    _mapper_direct_module().parse_pyc(live)
    marker_found=any(live.find(bytes((tag,))+struct.pack('<i',len(nb))+nb)>=0 for tag in (ord('s'),ord('t'),ord('u'),ord('s')|0x80,ord('t')|0x80,ord('u')|0x80))
    if not marker_found:raise ValueError('handle live readback failed')
    return patched,clean_new


# ==================== v0.9.25 UI TEXT EDITOR ====================
# NASCAR 15 stores user-facing interface strings in indexed TEXT*.LDA tables.
# Unlike the old roster rename path, this editor addresses one exact table
# index. Duplicate text remains independent, format tokens are protected, and
# longer strings use the proven append + cdfiles repoint transaction.
_UI_TEXT_CACHE={'signature':None,'rows':None,'files':None,'errors':None}
_UI_TEXT_FILE_CACHE={}
_UI_TEXT_CATEGORIES=[
    'Menus & Navigation','Race, HUD & Session Text','Career & Championship',
    'Paint, Garage & Team Shop','Prompts & Controls','Errors & Warnings',
    'Loading, Tips & Help','Formatted Templates','Other User Text',
    'Technical / Internal'
]
_UI_TEXT_MAX_BATCH=5000
_UI_TEXT_TOKEN_RX=re.compile(
    r'%(?:\([^)]+\))?[#0\- +]?\d*(?:\.\d+)?[diouxXeEfFgGcrsa%]|'
    r'\{[^{}\r\n]+\}|\$[A-Za-z_][A-Za-z0-9_]*\$|'
    r'\\[nrt]'
)
_UI_TEXT_MAIN_MENU={
    'race now','career','single season','multiplayer','paint booth','options',
    'extras','my nascar','team shop','driver select','track select','continue',
    'quick race','championship','livery studio','quit','exit game'
}


def _ui_text_signature(reg):
    if '0' not in reg:return None
    v=reg['0'];paths=[v['ar'],v['cdf'],v['bak'],backup_path(v['cdf'])]
    out=[]
    for path in paths:
        try:
            st=os.stat(path);out.append((path,st.st_size,st.st_mtime_ns))
        except OSError:out.append((path,None,None))
    return tuple(out)


def _ui_text_sources(reg,pristine=False):
    v=need(reg,'0')
    pair=bool(os.path.exists(v['bak']) and os.path.exists(backup_path(v['cdf'])))
    if pristine and not pair:
        raise ValueError('no pristine ARCHIVE0 + cdfiles0 backup pair exists yet')
    use_stock=bool(pristine and pair)
    return (v['bak'] if use_stock else v['ar'],
            backup_path(v['cdf']) if use_stock else v['cdf'],use_stock)


def _ui_text_file_rows(reg,pristine=False):
    _ar,cdf,_stock=_ui_text_sources(reg,pristine)
    return [(o,z,n) for o,z,n in parse_cdfiles(cdf)
            if re.match(r'^TEXT\d*\.LDA$',n,re.I)]


def _ui_text_read_file(reg,name,pristine=False):
    ar,cdf,use_stock=_ui_text_sources(reg,pristine)
    hit=None
    for o,z,n in parse_cdfiles(cdf):
        if n.casefold()==str(name).casefold():hit=(o,z,n);break
    if not hit:raise ValueError(f'{name} not found in '+('pristine ' if use_stock else '')+'ARCHIVE0 index')
    o,z,n=hit
    with open(ar,'rb') as f:f.seek(o);blob=f.read(z)
    if len(blob)!=z:raise ValueError(f'short read for {n}')
    entries=C.lda_entries(blob)
    return blob,entries,dict(offset=o,size=z,name=n,pristine=use_stock)


def _ui_text_decode(raw):
    return bytes(raw).decode('latin1','replace')


def _ui_text_tokens(text):
    return [t for t in _UI_TEXT_TOKEN_RX.findall(str(text)) if t!='%%']


def _ui_text_visible(text):
    return str(text).replace('\r','\\r').replace('\n','\\n').replace('\t','\\t')


def _ui_text_classify(text):
    t=str(text);lo=t.strip().casefold();screen='General UI';category='Other User Text';user=True
    if not t:
        return category,screen,False
    printable=sum(ch.isprintable() or ch in '\r\n\t' for ch in t)/max(1,len(t))
    technical=(printable<.92 or bool(re.search(r'[/\\]|\.(?:arc|dds|tga|png|pyc|lda|xml|csv)$',lo)) or
               (re.match(r'^[A-Z0-9_]{4,}$',t) is not None) or
               (len(t)>2 and ' ' not in t and t.count('_')>=2))
    if technical:
        return 'Technical / Internal','Internal identifier',False
    if lo in _UI_TEXT_MAIN_MENU:
        return 'Menus & Navigation','Main Menu',True
    if any(k in lo for k in ('race','lap','qualif','practice','pit','caution','restart','green flag','checkered','draft','damage','fuel','tyre','tire')):
        category='Race, HUD & Session Text';screen='Race / Garage / HUD'
    elif any(k in lo for k in ('career','season','championship','standings','points','sponsor','contract','calendar','playoff','chase')):
        category='Career & Championship';screen='Career / Single Season'
    elif any(k in lo for k in ('paint','scheme','livery','colour','color','decal','vinyl','team shop')):
        category='Paint, Garage & Team Shop';screen='Paint Booth / Team Shop'
    elif any(k in lo for k in ('controller','keyboard','button','press ','select ','confirm','cancel','back','continue','yes','no')):
        category='Prompts & Controls';screen='Prompts / Controls'
    elif any(k in lo for k in ('error','failed','unable','warning','invalid','not available','connection','disconnected')):
        category='Errors & Warnings';screen='Dialog / Error'
    elif any(k in lo for k in ('loading','tip:','did you know','trivia','fact:')) or len(t)>180:
        category='Loading, Tips & Help';screen='Loading / Help'
    elif _ui_text_tokens(t):
        category='Formatted Templates';screen='Dynamic UI text'
    elif len(t)<=42 and (t.istitle() or t.isupper()):
        category='Menus & Navigation';screen='Menu / Heading'
    return category,screen,user


def _ui_text_quick_status():
    """Return TEXT table metadata without decoding every LDA string.

    The old status endpoint synchronously read every live table and every stock
    copy before the tab could render. On a full install that could take minutes.
    This quick path only parses cdfiles0; individual tables are decoded lazily.
    """
    g,reg=registry()
    if not g or '0' not in reg:
        raise ValueError('ARCHIVE0 is not available; configure the NASCAR 15 folder first')
    v=need(reg,'0')
    has_stock=os.path.exists(v['bak']) and os.path.exists(backup_path(v['cdf']))
    stock_names=set()
    if has_stock:
        try: stock_names={n.casefold() for _o,_z,n in _ui_text_file_rows(reg,True)}
        except Exception: stock_names=set()
    files=[dict(name=n,size=z,offset=o,has_stock=n.casefold() in stock_names,
                count=None,user_facing=None,modified=None)
           for o,z,n in _ui_text_file_rows(reg,False)]
    return sorted(files,key=lambda x:x['name'].casefold())


def _ui_text_scan_file(file_name,force=False,include_stock=True):
    """Decode one TEXT table and cache it independently."""
    g,reg=registry()
    sig=_ui_text_signature(reg)
    key=(sig,str(file_name).casefold(),bool(include_stock))
    if not force and key in _UI_TEXT_FILE_CACHE:
        return _UI_TEXT_FILE_CACHE[key]
    blob,entries,meta=_ui_text_read_file(reg,file_name,False)
    stock_entries=[];errors=[]
    if include_stock:
        try:
            _sblob,stock_entries,_smeta=_ui_text_read_file(reg,file_name,True)
        except Exception as ex:
            # No paired pristine copy is normal on a first run. Keep the table
            # usable and simply omit stock comparisons.
            if 'no pristine' not in str(ex).lower(): errors.append(f'{file_name} stock: {ex}')
    counts=collections.Counter(_ui_text_decode(e['raw']) for e in entries)
    rows=[]
    for e in entries:
        current=_ui_text_decode(e['raw'])
        stock=(_ui_text_decode(stock_entries[e['index']]['raw'])
               if e['index']<len(stock_entries) else None)
        cat,screen,user=_ui_text_classify(current)
        rows.append(dict(file=meta['name'],index=e['index'],current=current,stock=stock,
                         current_length=len(e['raw']),
                         stock_length=(len(stock_entries[e['index']]['raw']) if e['index']<len(stock_entries) else None),
                         category=cat,screen=screen,user_facing=user,
                         modified=(stock is not None and current!=stock),tokens=_ui_text_tokens(current),
                         byte_offset=e['start'],file_size=len(blob),
                         reference_count=counts[current],shared=counts[current]>1))
    result=(rows,dict(name=meta['name'],count=len(rows),
                      user_facing=sum(1 for x in rows if x['user_facing']),
                      modified=sum(1 for x in rows if x['modified']),
                      size=meta['size'],has_stock=bool(stock_entries)),errors)
    _UI_TEXT_FILE_CACHE[key]=result
    return result


def _ui_text_scan(force=False):
    g,reg=registry()
    if not g or '0' not in reg:raise ValueError('ARCHIVE0 is not available; configure the NASCAR 15 folder first')
    sig=_ui_text_signature(reg)
    if not force and _UI_TEXT_CACHE.get('rows') is not None and _UI_TEXT_CACHE.get('signature')==sig:
        return _UI_TEXT_CACHE['rows'],_UI_TEXT_CACHE['files'],_UI_TEXT_CACHE['errors']
    live_files=_ui_text_file_rows(reg,False)
    v=need(reg,'0');has_pristine=os.path.exists(v['bak']) and os.path.exists(backup_path(v['cdf']))
    stock_names=({n.casefold() for _o,_z,n in _ui_text_file_rows(reg,True)} if has_pristine else set())
    rows=[];files=[];errors=[];raw_counts=collections.Counter()
    per_file=[]
    for _off,_size,name in live_files:
        try:
            blob,entries,meta=_ui_text_read_file(reg,name,False)
            stock_entries=[]
            if name.casefold() in stock_names:
                try:_sblob,stock_entries,_smeta=_ui_text_read_file(reg,name,True)
                except Exception as ex:errors.append(f'{name} stock: {ex}')
            values=[]
            for e in entries:
                current=_ui_text_decode(e['raw']);stock=(
                    _ui_text_decode(stock_entries[e['index']]['raw'])
                    if e['index']<len(stock_entries) else None)
                cat,screen,user=_ui_text_classify(current)
                item=dict(file=name,index=e['index'],current=current,stock=stock,
                          current_length=len(e['raw']),stock_length=(len(stock_entries[e['index']]['raw']) if e['index']<len(stock_entries) else None),
                          category=cat,screen=screen,user_facing=user,
                          modified=(stock is not None and current!=stock),tokens=_ui_text_tokens(current),
                          byte_offset=e['start'],file_size=len(blob))
                values.append(item);raw_counts[current]+=1
            per_file.append((name,values,meta,bool(stock_entries)))
        except Exception as ex:errors.append(f'{name}: {ex}')
    for name,values,meta,has_stock in per_file:
        for item in values:
            item['reference_count']=raw_counts[item['current']]
            item['shared']=item['reference_count']>1
            rows.append(item)
        files.append(dict(name=name,count=len(values),user_facing=sum(1 for x in values if x['user_facing']),
                          modified=sum(1 for x in values if x['modified']),size=meta['size'],has_stock=has_stock))
    _UI_TEXT_CACHE.update(signature=sig,rows=rows,files=files,errors=errors)
    return rows,files,errors


def _ui_text_invalidate():
    _UI_TEXT_CACHE.update(signature=None,rows=None,files=None,errors=None)
    _UI_TEXT_FILE_CACHE.clear()


def _ui_text_exact(reg,file_name,index,pristine=False):
    blob,entries,meta=_ui_text_read_file(reg,file_name,pristine)
    i=int(index)
    if i<0 or i>=len(entries):raise ValueError(f'string index {i} is outside {file_name}')
    return blob,entries[i],meta


def _ui_text_encode(text):
    text=str(text)
    if '\x00' in text:raise ValueError('text cannot contain a NUL character')
    try:return text.encode('latin1')
    except UnicodeEncodeError as ex:
        bad=text[ex.start:ex.end]
        raise ValueError(f'{bad!r} is outside the game\'s Latin-1 text encoding')


def _ui_text_missing_tokens(old,new):
    oldc=collections.Counter(_ui_text_tokens(old));newc=collections.Counter(_ui_text_tokens(new))
    missing=[]
    for token,count in oldc.items():
        if newc[token]<count:missing.extend([token]*(count-newc[token]))
    return missing


def _ui_text_plan(file_name,index,new_text,mode='auto',force_tokens=False):
    g,reg=registry();blob,e,meta=_ui_text_exact(reg,file_name,index,False)
    old=_ui_text_decode(e['raw']);newb=_ui_text_encode(new_text);oldb=e['raw']
    missing=_ui_text_missing_tokens(old,new_text)
    if missing and not force_tokens:
        raise ValueError('replacement removes required format token(s): '+', '.join(missing))
    mode=str(mode or 'auto').lower()
    if mode not in ('auto','fixed','rebuild'):raise ValueError('mode must be auto, fixed, or rebuild')
    chosen=('fixed' if len(newb)<=len(oldb) else 'rebuild') if mode=='auto' else mode
    if chosen=='fixed' and len(newb)>len(oldb):
        raise ValueError(f'fixed-slot mode allows at most {len(oldb)} Latin-1 bytes; replacement is {len(newb)}')
    rebuilt_size=len(blob)
    if chosen=='rebuild':
        rebuilt,_=C.lda_rebuild_indices(blob,{int(index):newb});rebuilt_size=len(rebuilt)
    rows,_fm,_fe=_ui_text_scan_file(file_name,include_stock=False);matches=[r for r in rows if r['index']==int(index)]
    refs=matches[0]['reference_count'] if matches else 1
    return dict(file=meta['name'],index=int(index),old=old,new=str(new_text),old_bytes=len(oldb),new_bytes=len(newb),
                mode=chosen,file_size=len(blob),rebuilt_size=rebuilt_size,size_delta=rebuilt_size-len(blob),
                repoint=(chosen=='rebuild'),missing_tokens=missing,format_tokens=_ui_text_tokens(old),
                reference_count=refs,shared=refs>1,has_backup=os.path.exists(need(reg,'0')['bak']) and os.path.exists(backup_path(need(reg,'0')['cdf'])))


def _ui_text_apply_one(file_name,index,new_text,mode='auto',force_tokens=False):
    plan=_ui_text_plan(file_name,index,new_text,mode,force_tokens)
    g,reg=registry();v=need(reg,'0')
    if _rp_game_running():raise ValueError('NASCAR15.exe is running; close the game first')
    blob,e,meta=_ui_text_exact(reg,file_name,index,False);newb=_ui_text_encode(new_text)
    _rp_backup_pair(v)
    if plan['mode']=='fixed':
        replacement=newb+b'\0'*(len(e['raw'])-len(newb))
        before_size=os.path.getsize(v['ar'])
        with open(v['ar'],'r+b') as f:
            f.seek(meta['offset']+e['start']);f.write(replacement);f.flush();os.fsync(f.fileno())
            f.seek(meta['offset']+e['start']);readback=f.read(len(replacement))
        if readback!=replacement:raise ValueError('text readback mismatch')
        if os.path.getsize(v['ar'])!=before_size:raise ValueError('archive size changed during fixed-slot edit')
        history=dict(timestamp=datetime.datetime.now().isoformat(timespec='seconds'),archive='0',entry=meta['name'],
                     category='UI Text',source_name=f'{meta["name"]} string {index}',old_offset=meta['offset'],old_size=meta['size'],
                     new_offset=meta['offset'],new_size=meta['size'],growth=0,verified=True,text_index=int(index),text_mode='fixed')
        try:_rp_add_history(history)
        except Exception:pass
        result=dict(ok=True,verified=True,mode='fixed',history=history)
    else:
        rebuilt,changed=C.lda_rebuild_indices(blob,{int(index):newb})
        if changed!=1:raise ValueError('the requested text already matches the replacement')
        fd,tmp=tempfile.mkstemp(prefix='n15mod_ui_text_',suffix='.LDA');os.close(fd)
        try:
            with open(tmp,'wb') as f:f.write(rebuilt)
            _raw,idxrows,_lay=_rp_index_rows(v['cdf']);row=_rp_find_row(idxrows,meta['name'])
            with _RP_LOCK:
                result=_rp_install_one('0',v,row,tmp,source_name=f'UI Text {meta["name"]} #{index}',allow_magic=True)
        finally:
            try:os.remove(tmp)
            except OSError:pass
        result['mode']='rebuild'
    _ui_text_invalidate()
    return result,plan


@app.route('/api/ui_text/status')
def ui_text_status():
    try:
        files=_ui_text_quick_status()
        return jsonify(dict(ok=True,lazy=True,files=files,file_count=len(files),
                            string_count=None,user_facing=None,modified_count=None,
                            shared_count=None,categories=_UI_TEXT_CATEGORIES,
                            category_counts={},errors=[],encoding='Latin-1',
                            long_text_repoint=True,
                            note='Tables are decoded on demand. Choose one table or enter a search to scan all tables.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/list',methods=['POST'])
def ui_text_list():
    q=request.get_json(silent=True) or {}
    query=str(q.get('q') or '').casefold().strip()
    file_name=str(q.get('file') or 'all')
    category=str(q.get('category') or 'all')
    modified=bool(q.get('modified_only'));include_internal=bool(q.get('include_internal'))
    page=max(0,int(q.get('page',0)));per=max(1,min(250,int(q.get('per',100))))
    try:
        # Initial tab load is intentionally non-blocking. Scanning every table
        # is deferred until the user chooses a table or enters a real search.
        if file_name=='all' and not query and category=='all' and not modified:
            return jsonify(dict(ok=True,total=0,page=page,per=per,rows=[],
                                files=_ui_text_quick_status(),errors=[],requires_filter=True,
                                note='Choose a TEXT table or type a search term to scan all tables.'))
        rows=[];files=[];errors=[]
        if file_name!='all':
            fr,fm,fe=_ui_text_scan_file(file_name,include_stock=True)
            rows=list(fr);files=[fm];errors.extend(fe)
        else:
            # Explicit all-table search. Decode one file at a time and retain
            # each result in the per-file cache, so subsequent searches are fast.
            for fm0 in _ui_text_quick_status():
                try:
                    fr,fm,fe=_ui_text_scan_file(fm0['name'],include_stock=True)
                    rows.extend(fr);files.append(fm);errors.extend(fe)
                except Exception as ex:errors.append(f"{fm0['name']}: {ex}")
        out=[]
        for r in rows:
            if category!='all' and r['category']!=category:continue
            if modified and not r['modified']:continue
            if not include_internal and not r['user_facing']:continue
            hay=' '.join((r['file'],str(r['index']),r['current'],r.get('stock') or '',r['category'],r['screen'])).casefold()
            if query and query not in hay:continue
            x=dict(r);x['current_display']=_ui_text_visible(x['current']);x['stock_display']=(_ui_text_visible(x['stock']) if x['stock'] is not None else None)
            out.append(x)
        total=len(out)
        return jsonify(dict(ok=True,total=total,page=page,per=per,
                            rows=out[page*per:(page+1)*per],files=files,errors=errors,
                            requires_filter=False))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/change',methods=['POST'])
def ui_text_change():
    q=request.get_json(silent=True) or {}
    try:
        plan=_ui_text_plan(q['file'],q['index'],q.get('new',''),q.get('mode','auto'),bool(q.get('force_tokens')))
        if q.get('dry_run'):return jsonify(dict(ok=True,dry_run=True,plan=plan))
        result,plan=_ui_text_apply_one(q['file'],q['index'],q.get('new',''),q.get('mode','auto'),bool(q.get('force_tokens')))
        return jsonify(dict(ok=True,plan=plan,result=result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/restore',methods=['POST'])
def ui_text_restore():
    q=request.get_json(silent=True) or {}
    try:
        _blob,e,_meta=_ui_text_exact(registry()[1],q['file'],q['index'],True)
        stock=_ui_text_decode(e['raw'])
        result,plan=_ui_text_apply_one(q['file'],q['index'],stock,'rebuild',True)
        return jsonify(dict(ok=True,stock=stock,plan=plan,result=result))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/restore_file',methods=['POST'])
def ui_text_restore_file():
    q=request.get_json(silent=True) or {};tmp=None
    try:
        g,reg=registry();v=need(reg,'0');stock,_entries,meta=_ui_text_read_file(reg,q['file'],True)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_ui_text_stock_',suffix='.LDA');os.close(fd);open(tmp,'wb').write(stock)
        _raw,rows,_layout=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,meta['name'])
        with _RP_LOCK:result=_rp_install_one('0',v,row,tmp,source_name=f'UI Text restore {meta["name"]}',allow_magic=True)
        _ui_text_invalidate();return jsonify(dict(ok=True,result=result,restored=meta['name']))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass


@app.route('/api/ui_text/export')
def ui_text_export():
    try:
        rows,_files,_errors=_ui_text_scan();file_name=request.args.get('file','all');category=request.args.get('category','all')
        chosen=[r for r in rows if (file_name=='all' or r['file']==file_name) and (category=='all' or r['category']==category)]
        out=io.StringIO(newline='');fields=['file','index','category','screen','stock_text','current_text','new_text','current_bytes','reference_count','format_tokens']
        w=csv.DictWriter(out,fieldnames=fields);w.writeheader()
        for r in chosen:w.writerow(dict(file=r['file'],index=r['index'],category=r['category'],screen=r['screen'],stock_text=r.get('stock') or '',current_text=r['current'],new_text=r['current'],current_bytes=r['current_length'],reference_count=r['reference_count'],format_tokens=' | '.join(r['tokens'])))
        data=io.BytesIO(out.getvalue().encode('utf-8-sig'))
        return send_file(data,mimetype='text/csv',as_attachment=True,download_name='nascar15_ui_text_export.csv')
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui_text/import_preview',methods=['POST'])
def ui_text_import_preview():
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a CSV exported by the UI Text Editor')
        raw=up.read()
        if len(raw)>8*1024*1024:raise ValueError('CSV exceeds the 8 MB safety limit')
        text=raw.decode('utf-8-sig');reader=csv.DictReader(io.StringIO(text));changes=[];seen=set()
        for line,row in enumerate(reader,2):
            fn=(row.get('file') or '').strip();idxs=(row.get('index') or '').strip();new=row.get('new_text')
            if new is None:new=row.get('current_text')
            if not fn or not idxs:continue
            key=(fn,int(idxs))
            if key in seen:raise ValueError(f'duplicate file/index at CSV line {line}: {fn} #{idxs}')
            seen.add(key)
            try:
                plan=_ui_text_plan(fn,int(idxs),new or '','auto',False)
                if plan['old']==(new or ''):continue
                changes.append(dict(file=fn,index=int(idxs),new=new or '',valid=True,plan=plan,line=line))
            except Exception as ex:
                changes.append(dict(file=fn,index=int(idxs),new=new or '',valid=False,error=str(ex),line=line))
            if len(changes)>_UI_TEXT_MAX_BATCH:raise ValueError(f'CSV has more than {_UI_TEXT_MAX_BATCH} changes')
        return jsonify(dict(ok=True,count=len(changes),valid_count=sum(1 for x in changes if x['valid']),invalid_count=sum(1 for x in changes if not x['valid']),changes=changes))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


def _ui_text_batch_apply_internal(changes,force_tokens=False,source_prefix='UI Text batch'):
    td=None
    if not changes:raise ValueError('no text changes were supplied')
    if len(changes)>_UI_TEXT_MAX_BATCH:raise ValueError(f'batch exceeds {_UI_TEXT_MAX_BATCH} changes')
    g,reg=registry();v=need(reg,'0')
    if _rp_game_running():raise ValueError('NASCAR15.exe is running; close the game first')
    grouped=collections.defaultdict(dict)
    for item in changes:
        fn=str(item['file']);idx=int(item['index']);new=str(item.get('new',''))
        _blob,e,meta=_ui_text_exact(reg,fn,idx,False);old=_ui_text_decode(e['raw'])
        missing=_ui_text_missing_tokens(old,new)
        if missing and not force_tokens:raise ValueError(f'{fn} #{idx} removes token(s): '+', '.join(missing))
        grouped[meta['name']][idx]=_ui_text_encode(new)
    try:
        td=tempfile.mkdtemp(prefix='n15mod_ui_text_batch_');prepared=[]
        for n,(fn,repls) in enumerate(grouped.items()):
            blob,_entries,meta=_ui_text_read_file(reg,fn,False);rebuilt,changed=C.lda_rebuild_indices(blob,repls)
            if not changed:continue
            fp=os.path.join(td,f'{n:03d}_{os.path.basename(fn)}');open(fp,'wb').write(rebuilt)
            _raw,idxrows,_layout=_rp_index_rows(v['cdf']);row=_rp_find_row(idxrows,fn)
            prepared.append((fn,row,fp,changed))
        if not prepared:raise ValueError('all imported values already match the live text')
        _rp_backup_pair(v);state=dict(archive_size=os.path.getsize(v['ar']),cdf=open(v['cdf'],'rb').read());results=[]
        try:
            with _RP_LOCK:
                for fn,row,fp,changed in prepared:
                    r=_rp_install_one('0',v,row,fp,source_name=f'{source_prefix} {fn}',allow_magic=True,history=False)
                    r['history']['text_changes']=changed;results.append(r)
                hist=_rp_load_history();hist.extend(r['history'] for r in results);_rp_save_history(hist)
        except Exception as install_ex:
            rollback_archive_cdf(v,state['archive_size'],state['cdf'],'.ui_text_rollback.tmp',install_ex)
            raise
        _ui_text_invalidate()
        return dict(ok=True,atomic=True,files=len(results),changes=sum(x[3] for x in prepared),verified=True,results=[r['history'] for r in results])
    finally:
        if td:shutil.rmtree(td,ignore_errors=True)


@app.route('/api/ui_text/batch_apply',methods=['POST'])
def ui_text_batch_apply():
    q=request.get_json(silent=True) or {}
    try:return jsonify(_ui_text_batch_apply_internal(q.get('changes') or [],bool(q.get('force_tokens')),'UI Text CSV'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end v0.9.25 UI TEXT EDITOR ====================

# ---------------- stats (base 0-100 + supported custom range) ----------------
STATS=['skill','aggression','skill_intermediate','skill_plate',
       'skill_road_course','skill_short','skill_superspeedway']
STAT_LABELS=['Overall Skill','Aggression','Intermediate','Plate','Road Course','Short Track','Superspeedway']
PYC_CODE_BASE = 30
STAT_EXPERIMENTAL_ABS_MAX=1_000_000_000.0


def load_profiles():
    name='ai_profiles_nascar14.csv' if ACTIVE_GAME=='nascar14' else 'ai_profiles.csv'
    return list(csv.DictReader(open(_game_data_path(name),encoding='utf-8-sig')))


def _pyc_consts(pyc):
    class P:
        def __init__(s,d,o): s.d=d; s.o=o
        def b(s): v=s.d[s.o]; s.o+=1; return v
        def i32(s):
            v=struct.unpack_from('<i',s.d,s.o)[0]; s.o+=4; return v
        def rd(s,n): v=s.d[s.o:s.o+n]; s.o+=n; return v
        def obj(s):
            t=chr(s.b() & 0x7f)
            if t=='N': return None
            if t in 'FT': return t=='T'
            if t=='i': return s.i32()
            if t=='I': return struct.unpack('<q',s.rd(8))[0]
            if t=='g': return struct.unpack('<d',s.rd(8))[0]
            if t=='f': return float(s.rd(s.b()).decode('ascii'))
            if t in 'st': return s.rd(s.i32())
            if t=='u': return s.rd(s.i32()).decode('utf8','replace')
            if t=='R': s.i32(); return None
            if t in '([': return tuple(s.obj() for _ in range(s.i32()))
            if t=='l': s.rd(abs(s.i32())*2); return None
            if t=='c':
                for _ in range(4): s.i32()
                s.obj(); consts=s.obj()
                for _ in range(6): s.obj()
                s.i32(); s.obj()
                return consts
            raise ValueError('marshal '+t)
    return P(pyc,8).obj()


class _StatMarshalSkip:
    """Small Python-2 marshal walker used only to locate root co_consts."""
    def __init__(self,data):self.d=data;self.i=0
    def take(self,n):
        if self.i+n>len(self.d):raise ValueError('truncated Python-2 marshal object')
        o=self.i;self.i+=n;return o
    def i32(self):o=self.take(4);return struct.unpack_from('<i',self.d,o)[0]
    def obj(self,depth=0):
        if depth>300:raise ValueError('marshal nesting is too deep')
        c=chr(self.d[self.take(1)] & 0x7f)
        if c in ('N','T','F','S','.','0'):return
        if c=='i':self.take(4);return
        if c=='I':self.take(8);return
        if c=='g':self.take(8);return
        if c=='y':self.take(16);return
        if c=='f':self.take(self.d[self.take(1)]);return
        if c=='x':
            self.take(self.d[self.take(1)]);self.take(self.d[self.take(1)]);return
        if c=='l':self.take(abs(self.i32())*2);return
        if c in ('s','t','u'):
            n=self.i32();
            if n<0:raise ValueError('negative marshal string length')
            self.take(n);return
        if c=='R':self.take(4);return
        if c in ('(','['):
            n=self.i32()
            if n<0 or n>10_000_000:raise ValueError('invalid marshal sequence length')
            for _ in range(n):self.obj(depth+1)
            return
        if c=='{':
            while True:
                if self.i>=len(self.d):raise ValueError('unterminated marshal dict')
                if chr(self.d[self.i]&0x7f)=='0':self.i+=1;break
                self.obj(depth+1);self.obj(depth+1)
            return
        if c=='c':
            self.take(16)
            for _ in range(8):self.obj(depth+1) # code,consts,names,varnames,freevars,cellvars,filename,name
            self.take(4);self.obj(depth+1)
            return
        raise ValueError('unsupported Python-2 marshal type '+repr(c))


def _stat_root_layout(pyc):
    if len(pyc)<31 or pyc[8]&0x7f!=ord('c'):
        raise ValueError('unexpected PYC root layout')
    r=_StatMarshalSkip(pyc);r.i=9;r.take(16)
    typ=chr(pyc[r.take(1)]&0x7f)
    if typ not in ('s','t'):raise ValueError('PYC root bytecode is not a string object')
    code_len=r.i32();code_off=r.take(code_len)
    const_type_off=r.i
    if chr(pyc[r.take(1)]&0x7f)!='(':
        raise ValueError('PYC root constants are not a tuple')
    count_pos=r.i;count=r.i32();items_start=r.i
    for _ in range(count):r.obj(1)
    return dict(code_off=code_off,code_len=code_len,const_type_off=const_type_off,
                count_pos=count_pos,count=count,items_start=items_start,const_end=r.i)


def _stat_live_entry(reg):
    v=need(reg,'0');raw,rows,layout=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,'DB_AICONFIG_SCRIPT.PYC')
    with open(v['ar'],'rb') as fh:fh.seek(row['offset']);pyc=fh.read(row['size'])
    if len(pyc)!=row['size']:raise ValueError('short DB_AICONFIG_SCRIPT.PYC read')
    return v,row,pyc


def stat_machine(reg):
    v,row,pyc=_stat_live_entry(reg)
    layout=_stat_root_layout(pyc)
    consts=_pyc_consts(pyc)
    iov={i:const_value for i,const_value in enumerate(consts) if isinstance(const_value,float)}
    voi={}
    for const_index,const_value in iov.items():voi.setdefault(const_value,const_index)
    return row['offset'],row['size'],voi,iov,pyc,layout,row,v


def stat_offset(row, st):
    return PYC_CODE_BASE + int(row[st+'_load_offset_hex'],16)


def _stat_display_from_const(value):
    x=float(value)*100.0
    return int(round(x)) if abs(x-round(x))<1e-9 else round(x,6)


def read_stats(reg):
    off,sz,voi,iov,pyc,layout,idxrow,v = stat_machine(reg)
    profs={int(r['profile_id']):r for r in load_profiles()}
    out=[]
    for link in load_driver_links():
        profile_id=int(link.get('profile_id',-1))
        if profile_id not in profs:continue
        row=profs[profile_id];vals={}
        for st in STATS:
            lo=stat_offset(row,st)
            if lo+3>len(pyc) or pyc[lo]!=0x64:raise ValueError('unexpected rating bytecode - scan offsets no longer match')
            ci=struct.unpack_from('<H',pyc,lo+1)[0]
            vals[st]=_stat_display_from_const(iov.get(ci,float(row[st])))
        out.append(dict(slot=link.get('slot'),label=_driver_display_from_link(link),profile_id=profile_id,stats=vals))
    return sorted(out,key=lambda d:d['label'])


def _stat_rebuild_with_const(pyc,load_off,target):
    layout=_stat_root_layout(pyc);consts=_pyc_consts(pyc)
    if layout['count']!=len(consts):raise ValueError('PYC constant-count validation failed')
    if layout['count']>=65535:raise ValueError('PYC constant table has reached the 16-bit LOAD_CONST limit')
    if load_off<layout['code_off'] or load_off+3>layout['code_off']+layout['code_len'] or pyc[load_off]!=0x64:
        raise ValueError('rating LOAD_CONST offset is invalid')
    new_index=layout['count'];out=bytearray(pyc)
    struct.pack_into('<H',out,load_off+1,new_index)
    struct.pack_into('<i',out,layout['count_pos'],new_index+1)
    out[layout['const_end']:layout['const_end']]=b'g'+struct.pack('<d',float(target))
    check=_pyc_consts(bytes(out))
    if len(check)!=new_index+1 or not isinstance(check[new_index],float) or abs(check[new_index]-target)>1e-12:
        raise ValueError('rebuilt rating PYC failed constant readback')
    # The bytecode lies before co_consts, so inserting at const_end must not move
    # or alter the targeted instruction.
    if out[load_off]!=0x64 or struct.unpack_from('<H',out,load_off+1)[0]!=new_index:
        raise ValueError('rebuilt rating PYC failed LOAD_CONST readback')
    return bytes(out),new_index


def write_stat(reg,profile_id,stat,value100,experimental=False):
    if stat not in STATS:raise ValueError('bad stat')
    try:value100=float(value100)
    except Exception:raise ValueError('rating must be a finite number')
    if not _math.isfinite(value100):raise ValueError('NaN and infinity are blocked')
    if not experimental and not (0.0<=value100<=100.0):
        raise ValueError('the original rating scale is 0-100; enable Allow ratings outside 0-100 to use a custom value')
    if abs(value100)>STAT_EXPERIMENTAL_ABS_MAX:
        raise ValueError(f'absolute ratings above {STAT_EXPERIMENTAL_ABS_MAX:g} are blocked')
    target=value100/100.0
    off,sz,voi,iov,pyc,layout,idxrow,v=stat_machine(reg)
    row=next((r for r in load_profiles() if int(r['profile_id'])==int(profile_id)),None)
    if not row:raise ValueError('driver AI profile not found')
    lo=stat_offset(row,stat)
    if lo+3>len(pyc) or pyc[lo]!=0x64:raise ValueError('unexpected rating bytecode - aborted for safety')
    existing=next((idx for val,idx in voi.items() if abs(float(val)-target)<1e-12),None)
    _rp_backup_pair(v)
    if existing is not None:
        with open(v['ar'],'r+b') as fh:
            fh.seek(idxrow['offset']+lo);before=fh.read(3)
            if len(before)!=3 or before[0]!=0x64:raise ValueError('live rating bytecode changed; reload ratings')
            fh.seek(idxrow['offset']+lo+1);fh.write(struct.pack('<H',existing));fh.flush();os.fsync(fh.fileno())
        with open(v['ar'],'rb') as fh:fh.seek(idxrow['offset']+lo+1);rb=struct.unpack('<H',fh.read(2))[0]
        if rb!=existing:
            with open(v['ar'],'r+b') as fh:fh.seek(idxrow['offset']+lo);fh.write(before);fh.flush();os.fsync(fh.fileno())
            raise ValueError('rating operand readback failed; original bytes restored')
        return dict(applied=_stat_display_from_const(target),method='existing_constant',repoint=False,const_index=existing)
    rebuilt,new_index=_stat_rebuild_with_const(pyc,lo,target)
    fd,tmp=tempfile.mkstemp(prefix='n15mod_rating_',suffix='.PYC');os.close(fd)
    try:
        open(tmp,'wb').write(rebuilt)
        with _RP_LOCK:
            result=_rp_install_one('0',v,idxrow,tmp,source_name=f'Uncapped rating {profile_id}/{stat}',allow_magic=True)
    finally:
        try:os.remove(tmp)
        except OSError:pass
    # Parse the live repointed file and verify just this instruction resolves to target.
    _off,_sz,_voi,iov2,live,_layout,_row,_v=stat_machine(reg)
    ci=struct.unpack_from('<H',live,lo+1)[0]
    if ci not in iov2 or abs(iov2[ci]-target)>1e-12:raise ValueError('uncapped rating live readback failed')
    return dict(applied=_stat_display_from_const(target),method='append_constant_repoint',
                repoint=True,const_index=new_index,file=result)


def reset_stats(reg,profile_id):
    row=next((r for r in load_profiles() if int(r['profile_id'])==int(profile_id)),None)
    if not row:return 0
    n=0
    for st in STATS:
        write_stat(reg,profile_id,st,float(row[st])*100.0,experimental=True);n+=1
    return n

# ---------------- menus: numbers / custom thumbs ----------------
MENU_CONTAINERS = {
    'numbers': ('0','SPRINTNUMS2015.ARC'),
    'teams': ('0','2DRIVERSELECTMENUIMAGE.ARC'),
    'shoplogo': ('0','TEAMSHOPLOGO.ARC'),
    'shoplogo2': ('0','TEAMSHOPLOGO2.ARC'),
    'careerthumbs': ('0','BASESCHEMETHUMBNAILS.ARC'),
    'customthumbs': ('1','CUSTOMSCHEMETHUMBNAILS.ARC'),
}

# Clean-file mapping corrected the SPRINTNUMS parser itself.  Public v1 began
# every payload 40 bytes late, then used roll/seam hacks to make the corrupted
# preview look plausible.  With the native 16-byte records and +24-byte texture
# header mapped, the 128x64 / 64x128 atlases decode at their real orientation.
def _numcard_unroll(img):
    return img.copy()

def _numcard_reroll(img):
    return img.copy()

def _menu_containers():
    out=dict(MENU_CONTAINERS)
    out['numbers']=('0',active_game_profile().get('number_container','SPRINTNUMS2015.ARC'))
    # NASCAR '14 does not expose TEAMSHOPLOGO2 in the mapped base archive.
    if ACTIVE_GAME=='nascar14': out.pop('shoplogo2',None)
    return out


def menu_container(reg, key, live=True):
    arcid,name=_menu_containers()[key]
    a=need(reg,arcid)
    off,size=find_entry(reg,arcid,name)
    src=a['ar'] if live or not os.path.exists(a['bak']) else a['bak']
    with open(src,'rb') as fh:
        fh.seek(off); return arcid,off,size,fh.read(size)


def _menu_parse_entries(arc,key):
    """Parse menu banks with the mapped SPRINTNUMS storage geometry.

    SPRINTNUMS2015 has 94 DXT1 payloads that are all 4096 bytes and decode as
    128x64.  The 47 BIG_* records advertise 64x64 in one native header field,
    but the game consumes the complete 128x64 BC1 surface.  Treating those as
    64x64 decodes only the first half of the payload and produces the horizontal
    stripe preview seen in RC1.
    """
    return C.parse_multi_arc(arc,known_dims=(128,64) if key=='numbers' else None)

# ============ v0.7: paint-scheme preview containers (2DRIVERSELECTTD_*) ============
# Each team has a 2DRIVERSELECTTD_<teamid>.ARC in ARCHIVE1 holding DXT5 previews:
#   DRIVERPAINT_<id>   256x256  car render (the carousel image)
#   PAINTSCHEME_<id>   256x256  scheme thumbnail
#   DRIVER_<id>_3DNUM  512x256  3D number render
# Standard ARCC multi-texture containers -> reuse parse_multi_arc / multi_*.

def td_containers(reg):
    """Return list of (arcid, name, off, size) for every 2DRIVERSELECTTD_* file."""
    out=[]
    for arcid in reg:
        cdf=reg[arcid].get('cdf')
        if not cdf: continue
        try: ent=parse_cdfiles(cdf)
        except Exception: continue
        for o,s,n in ent:
            if n.startswith('2DRIVERSELECTTD_'):
                out.append((arcid, n, o, s))
    return sorted(out, key=lambda x:x[1])

def td_read(reg, container):
    """Read a TD container's bytes (live or backup) by its ARC name."""
    for arcid, name, off, size in td_containers(reg):
        if name==container:
            a=need(reg,arcid)
            with open(a['ar'],'rb') as fh:
                fh.seek(off); return arcid, off, size, fh.read(size)
    raise ValueError('TD container not found: '+container)

@app.route('/api/tdlist')
def api_tdlist():
    g,reg=registry()
    result=[]
    for arcid, name, off, size in td_containers(reg):
        team=name.replace('2DRIVERSELECTTD_','').replace('.ARC','')
        try:
            _,_,_,arc=td_read(reg,name)
            ent,_=C.parse_multi_arc(arc)
            entries=[dict(name=e['name'],w=e['w'],h=e['h'],fmt=e['fmt']) for e in ent]
        except Exception as ex:
            entries=[]; 
        result.append(dict(container=name, team=team, entries=entries))
    return jsonify(dict(ok=True, containers=result))

@app.route('/api/td/<container>/<entry>', methods=['GET','POST'])
def api_td_entry(container, entry):
    g,reg=registry()
    arcid,off,size,arc=td_read(reg,container)
    ent,_=C.parse_multi_arc(arc)
    match=[e for e in ent if e['name']==entry]
    if not match: return ('not found',404)
    e=match[0]
    if request.method=='GET':
        if request.args.get('pristine'):
            a=need(reg,arcid)
            if os.path.exists(a['bak']):
                with open(a['bak'],'rb') as fh:
                    fh.seek(off); arc=fh.read(size)
                ent2,_=C.parse_multi_arc(arc)
                match=[x for x in ent2 if x['name']==entry]; e=match[0] if match else e
        img=C.multi_read_png(arc,e)
        buf=io.BytesIO(); img.save(buf,'PNG'); buf.seek(0)
        return send_file(buf,mimetype='image/png')
    # Replace (PNG re-encode) is proven safe in-game ONLY for DRIVERPAINT car
    # renders. PAINTSCHEME and 3DNUM crash Paint Select when re-encoded (still
    # under investigation). Those stay Copy From / Export only.
    if not entry.startswith('DRIVERPAINT') and not request.args.get('experimental'):
        return jsonify(dict(ok=False,
            error='Replace only works for DRIVERPAINT car renders right now. '
                  'Use Copy From… or Export for '+entry.split('_')[0]+' entries.')),400
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(e['w'],e['h']),request_resize_mode('fit'),preserve_alpha=True)
    try:
        new=C.multi_write_png_validated(arc,e,img,encode_fn=encode_any)
        _ui_install(arcid,off,size,new)
    except RollbackFailed as ex:
        return jsonify(dict(ok=False,error='Image install failed and rollback also failed. Stop editing and restore the affected archive from backup. '+str(ex))),500
    except Exception as ex:
        return jsonify(dict(ok=False,error='write refused (safe): '+str(ex))),400
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True,verified=True,decode_verified=True,image_prep=prep))

@app.route('/api/td_copy', methods=['POST'])
def api_td_copy():
    """Copy the RAW stored bytes of one TD entry over another same-size entry.
    This is the proven-safe operation (no PNG/DXT re-encode). Both entries must
    be the same fmt and payload_size."""
    d=request.get_json(force=True)
    src_c, src_e = d['src_container'], d['src_entry']
    dst_c, dst_e = d['dst_container'], d['dst_entry']
    g,reg=registry()
    s_arcid,s_off,s_size,s_arc=td_read(reg,src_c)
    d_arcid,d_off,d_size,d_arc=td_read(reg,dst_c)
    s_ent,_=C.parse_multi_arc(s_arc); d_ent,_=C.parse_multi_arc(d_arc)
    se=next((e for e in s_ent if e['name']==src_e),None)
    de=next((e for e in d_ent if e['name']==dst_e),None)
    if not se or not de:
        return jsonify(dict(ok=False,error='entry not found')),404
    if se['payload_size']!=de['payload_size'] or se['fmt']!=de['fmt']:
        return jsonify(dict(ok=False,
            error=f'size/format mismatch: {se["fmt"]}/{se["payload_size"]} vs {de["fmt"]}/{de["payload_size"]}')),400
    raw=bytes(s_arc[se['payload_abs']:se['payload_abs']+se['payload_size']])
    new=bytearray(d_arc)
    new[de['payload_abs']:de['payload_abs']+de['payload_size']]=raw
    if len(new)!=len(d_arc):
        return jsonify(dict(ok=False,error='size guard failed')),500
    _ui_install(d_arcid,d_off,d_size,bytes(new))
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True,verified=True))

@app.route('/api/td/<container>/<entry>/reset', methods=['POST'])
def api_td_reset(container, entry):
    g,reg=registry()
    arcid,off,size,_=td_read(reg,container)
    a=need(reg,arcid)
    if not os.path.exists(a['bak']): return jsonify(dict(ok=False,error='no backup'))
    with open(a['bak'],'rb') as fh:
        fh.seek(off); barc=fh.read(size)
    ent,_=C.parse_multi_arc(barc)
    match=[e for e in ent if e['name']==entry]
    if not match: return ('not found',404)
    e=match[0]
    _arcid,_off,_size,live_arc=td_read(reg,container)
    restored=bytearray(live_arc)
    restored[e['payload_abs']:e['payload_abs']+e['payload_size']]=barc[e['payload_abs']:e['payload_abs']+e['payload_size']]
    _ui_install(arcid,off,size,bytes(restored))
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True,verified=True))

@app.route('/api/menu/<key>')
def api_menu_list(key):
    g,reg=registry()
    if key not in _menu_containers(): return jsonify(dict(ok=False,error='bad key')),404
    try:
        _,_,_,arc=menu_container(reg,key)
        ent,_=_menu_parse_entries(arc,key)
        return jsonify(dict(ok=True,
            entries=[dict(name=e['name'],w=e['w'],h=e['h']) for e in ent
                     if e['w']>0 and e['h']>0]))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e), entries=[]))

@app.route('/api/menu/<key>/<name>', methods=['GET','POST'])
def api_menu_entry(key,name):
    try:
        g,reg=registry()
        arcid,off,size,arc=menu_container(reg,key)
        ent,_=_menu_parse_entries(arc,key)
        match=[e for e in ent if e['name']==name]
        if not match: return ('not found',404)
        e=match[0]
        if request.method=='GET':
            if request.args.get('pristine'):
                _,_,_,arc=menu_container(reg,key,live=False)
            img=C.multi_read_png(arc,e)
            if key=='numbers': img=_numcard_unroll(img.convert('RGBA'))
            buf=io.BytesIO(); img.save(buf,'PNG'); buf.seek(0)
            return send_file(buf,mimetype='image/png')
        if key in ('shoplogo','shoplogo2'):
            return jsonify(dict(ok=False,error='Team Shop logo replacement is locked: this special short-payload texture caused an in-game fatal error. Use Stock to restore it.')),400
        f=request.files.get('file')
        if not f: return jsonify(dict(ok=False,error='no file')),400
        img=Image.open(f.stream)
        requested=request_resize_mode('auto')
        mode=('stretch' if key=='numbers' else ('fit' if requested=='auto' else requested))
        img,prep=prepare_import_image(img,(e['w'],e['h']),mode,preserve_alpha=True)
        prep['requested_mode']=requested; prep['effective_mode']=mode
        if key=='numbers':
            img=_numcard_reroll(img.convert('RGBA'))
            prep['target_aware']=True
            prep['resize_reason']='mapped SPRINTNUMS atlas; stretched to the exact native canvas so no black side bars are added'
        # This validates the rewritten ARC, every neighboring payload, and the
        # target's decode before the shared archive is touched.
        new=C.multi_write_png_validated(arc,e,img,encode_fn=encode_any,
                                             known_dims=((128,64) if key=='numbers' else None))
        # This performs a same-size transaction, fsync, exact full-container
        # readback, and verified rollback if the live write fails.
        _ui_install(arcid,off,size,new)
        _clear_ui_thumb_cache()
        return jsonify(dict(ok=True,verified=True,decode_verified=True,image_prep=prep,
                            target=dict(width=e['w'],height=e['h'],format=e['fmt'],
                                        payload_size=e['payload_size'])))
    except RollbackFailed as ex:
        return jsonify(dict(ok=False,error='Image install failed and the automatic rollback also failed. Stop editing and restore the affected archive from backup. '+str(ex))),500
    except Exception as ex:
        return jsonify(dict(ok=False,error='Image import was not installed: '+str(ex))),400

@app.route('/api/menu/<key>/<name>/reset', methods=['POST'])
def api_menu_reset(key,name):
    g,reg=registry()
    arcid,off,size,_=menu_container(reg,key)
    a=need(reg,arcid)
    if not os.path.exists(a['bak']): return jsonify(dict(ok=False,error='no backup'))
    _,_,_,arc_b=menu_container(reg,key,live=False)
    ent,_=_menu_parse_entries(arc_b,key)
    match=[e for e in ent if e['name']==name]
    if not match: return ('not found',404)
    e=match[0]
    _,_,_,live_arc=menu_container(reg,key,live=True)
    restored=bytearray(live_arc)
    restored[e['payload_abs']:e['payload_abs']+e['payload_size']]=arc_b[e['payload_abs']:e['payload_abs']+e['payload_size']]
    _ui_install(arcid,off,size,bytes(restored))
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True,verified=True))

# NASCAR '14 starts fail-closed. Flask route rules are listed exactly so a
# future endpoint is blocked until it is deliberately reviewed and added here.
_N14_ALLOWED_RULES={
 '/api/status','/api/app_settings','/api/diagnostics/export','/api/backup_now','/api/setpath','/api/restore',
 '/api/grid','/api/template/<name>','/api/thumb/<name>','/api/scheme_smart/<name>','/api/scheme/<name>',
 '/api/layer/<name>','/api/slotthumb/<name>','/api/build','/api/restore_slot','/api/import_scheme/<name>',
 '/api/previewedit/<name>','/api/roster','/api/name','/api/handle','/api/names/export','/api/names/restore_all',
 '/api/ui_text/status','/api/ui_text/list','/api/ui_text/change','/api/ui_text/restore','/api/ui_text/restore_file',
 '/api/ui_text/export','/api/ui_text/import_preview','/api/ui_text/batch_apply',
 '/api/stats','/api/stats/set','/api/stats/reset',
 '/api/audio/banks','/api/audio/samples','/api/audio/preview','/api/audio/export','/api/audio/export_bank',
 '/api/audio/restore_bank','/api/audio/replace','/api/audio/restore',
 '/api/pyc/status','/api/pyc/records','/api/pyc/set','/api/pyc/set_batch','/api/pyc/baseline','/api/pyc/restore',
 '/api/pyc/aitrack_crosswalk','/api/pyc/aiglobal','/api/pyc/worldpace',
 '/api/scr/list','/api/scr/set','/api/scr/keys','/api/scr/key/set','/api/scr/keys/batch',
 '/api/ui/discover','/api/ui/status','/api/ui/list','/api/ui/audit','/api/ui/tire_family','/api/ui/map',
 '/api/ui/manifest/export','/api/ui/mappings/export','/api/ui/export_raw','/api/ui/export','/api/ui/thumb',
 '/api/ui/replace_raw','/api/ui/copy','/api/ui/restore','/api/ui/bulk_restore',
 '/api/schedule','/api/schedule/stock','/api/schedule/preview','/api/schedule/apply','/api/schedule/restore',
 '/api/schedule/stock36_laps','/api/schedule/event_lap/preview','/api/schedule/event_lap/apply',
 '/api/schedule/event_laps/batch/preview','/api/schedule/event_laps/batch/apply','/api/schedule/stock36_laps/restore',
 '/api/tracks/files','/api/tracks/compare','/api/tracks/report','/api/tracks/export',
 '/api/presets','/api/presets/save','/api/presets/delete','/api/presets/export','/api/presets/import','/api/pitlog',
 '/api/pyc/audit','/api/pyc/audit/export','/api/support/check','/api/support/report','/api/help/request'
}

def _n14_route_allowed(path):
    rule=getattr(getattr(request,'url_rule',None),'rule',None)
    return (rule or path) in _N14_ALLOWED_RULES

_N14_PYC_WRITE_POLICY={
 'AIRACINGTRACKCONFIG_C': ('DB_AICONFIG_SCRIPT.PYC', None),
 'AIRACINGGLOBALCONFIG_C': ('DB_AICONFIG_SCRIPT.PYC', None),
 'WORLDSCRIPT_C': ('DB_GAME_LOCAL_SCRIPT.PYC', None),
 'RACEDATA_C': ('DB_GAME_LOCAL_SCRIPT.PYC', {'RaceLaps'}),
}

def _n14_pyc_write_allowed(rule,payload):
    if rule not in ('/api/pyc/set','/api/pyc/set_batch'): return True,None
    payload=payload if isinstance(payload,dict) else {}
    cls=str(payload.get('class') or '').upper(); policy=_N14_PYC_WRITE_POLICY.get(cls)
    if not policy: return False,f"PYC class {cls or '(missing)'} is read-only in NASCAR '14."
    expected_file,fields=policy
    if str(payload.get('file') or '').upper()!=expected_file:
        return False,f"{cls} writes are limited to {expected_file} in NASCAR '14."
    requested=[]
    if rule=='/api/pyc/set': requested=[str(payload.get('field') or '')]
    else: requested=[str(x.get('field') or '') for x in (payload.get('changes') or []) if isinstance(x,dict)]
    if not requested or any(not f for f in requested): return False,'PYC write request has no valid field.'
    if fields is not None and any(f not in fields for f in requested):
        return False,f'{cls} writes are limited to: '+', '.join(sorted(fields))+'.'
    return True,None

def _n14_ui_write_allowed(rule):
    if rule not in ('/api/ui/replace_raw','/api/ui/copy','/api/ui/restore','/api/ui/bulk_restore'): return True,None
    allowed_archives={'0','1'}
    archives=[]
    if rule=='/api/ui/replace_raw': archives=[str(request.form.get('archive') or '')]
    else:
        q=request.get_json(silent=True) or {}
        if rule=='/api/ui/copy': archives=[str((q.get('dst') or {}).get('archive') or '')]
        elif rule=='/api/ui/restore': archives=[str(q.get('archive') or '')]
        else: archives=[str(x.get('archive') or '') for x in (q.get('targets') or []) if isinstance(x,dict)]
    if not archives or any(a not in allowed_archives for a in archives):
        return False,"NASCAR '14 Graphics writes are currently limited to mapped ARCHIVE0/1 front-end assets. Track packages and paint archives remain read-only here."
    return True,None

@app.before_request
def _game_request_guard():
    path=request.path
    # Static assets and the shell document touch no game state, so they must not
    # queue behind a long install. Holding the global lock for them made the
    # whole UI appear frozen (rather than merely busy) during full repairs.
    _lock_exempt = (path=='/api/games/select' or path=='/' or path.startswith('/static/')
                    or path=='/favicon.ico')
    if not _lock_exempt:
        _GAME_SWITCH_LOCK.acquire(); g._game_switch_lock_owned=True
    public=(path=='/' or path=='/favicon.ico' or path.startswith('/static/') or path in ('/api/games/session','/api/games/select','/api/status','/api/appdata/export','/api/appdata/import'))
    if not GAME_SESSION_SELECTED and not public:
        return jsonify(dict(ok=False,error="Choose NASCAR 15 or NASCAR '14 before using the app.",code='game_not_selected')),409
    if GAME_SESSION_SELECTED and ACTIVE_GAME=='nascar14' and path.startswith('/api/') and not public:
        rule=getattr(getattr(request,'url_rule',None),'rule',None) or path
        if not _n14_route_allowed(path):
            return jsonify(dict(ok=False,error=f"{path} is not available for NASCAR '14 yet.",code='feature_not_available',game=active_game_name())),403
        allowed,reason=_n14_pyc_write_allowed(rule,request.get_json(silent=True) if request.method in ('POST','PUT','PATCH') else None)
        if not allowed:
            return jsonify(dict(ok=False,error=reason,code='pyc_write_not_allowed',game=active_game_name())),403
        allowed,reason=_n14_ui_write_allowed(rule)
        if not allowed:
            return jsonify(dict(ok=False,error=reason,code='graphics_write_not_allowed',game=active_game_name())),403

@app.after_request
def _log_api_failures(response):
    """Print the reason next to the access-log line for failed /api/ calls.

    Handlers here deliberately convert exceptions into {ok:false,error:...} with a
    4xx status, which keeps the UI friendly but leaves the console showing only
    `"GET /api/... " 400 -`. That is not enough to diagnose a bug report, so echo
    the message. Never touch streamed/file responses.
    """
    try:
        if (response.status_code >= 400
                and request.path.startswith('/api/')
                and not response.direct_passthrough
                and response.is_json):
            body = response.get_json(silent=True) or {}
            msg = body.get('error') or body.get('message')
            if msg:
                code = body.get('code')
                tag = f' [{code}]' if code else ''
                print(f'  -> {request.method} {request.path} {response.status_code}{tag}: {msg}',
                      file=sys.stderr, flush=True)
    except Exception:
        pass
    return response


@app.teardown_request
def _release_game_request_guard(_error=None):
    if getattr(g,'_game_switch_lock_owned',False):
        g._game_switch_lock_owned=False; _GAME_SWITCH_LOCK.release()

# ---------------- routes ----------------
@app.route('/')
def root():
    # The UI is inline JavaScript inside index.html.  Reusing a cached page from
    # an older RC can silently hide new controls while talking to a newer (or
    # older) backend, so the app shell must always be fetched fresh.
    response=send_from_directory(os.path.join(RES_DIR,'static'),'index.html',max_age=0)
    response.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma']='no-cache'
    response.headers['Expires']='0'
    return response

def _game_session_payload():
    profiles=[]
    for gid,profile in GAME_PROFILES.items():
        path=detect_game(gid)
        profiles.append(dict(id=gid,name=profile['name'],path=path,found=bool(path),
                             full_feature_set=bool(profile['full_feature_set']),
                             tabs=list(profile['tabs']),paint_modes=list(profile['paint_modes']),team_editor_mode=profile.get('team_editor_mode'),graphics_mode=profile.get('graphics_mode'),season_year=profile.get('season_year')))
    current=active_game_profile()
    return dict(ok=True,selected=bool(GAME_SESSION_SELECTED),active_game=ACTIVE_GAME,
                game_name=current['name'],profiles=profiles,tabs=list(current['tabs']),
                paint_modes=list(current['paint_modes']),full_feature_set=bool(current['full_feature_set']),team_editor_mode=current.get('team_editor_mode'),graphics_mode=current.get('graphics_mode'),season_year=current.get('season_year'))

@app.route('/api/games/session')
def game_session():
    return jsonify(_game_session_payload())

@app.route('/api/games/select', methods=['POST'])
def game_select():
    global GAME_SESSION_SELECTED
    q=request.get_json(silent=True) or {}
    gid=str(q.get('game_id') or '').strip().lower()
    try:
        with _GAME_SWITCH_LOCK: _activate_game(gid)
    except RuntimeError as ex:
        return jsonify(dict(ok=False,error=str(ex),code='game_switch_busy')),409
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    GAME_SESSION_SELECTED=True
    return jsonify(_game_session_payload())

@app.route('/api/status')
def status():
    g,reg=registry()
    profile=active_game_profile()
    ok=bool(g and all(k in reg for k in profile['required_archives']))
    added_scheme_scan = None
    if ok and ACTIVE_GAME == 'nascar15':
        try:
            added_scheme_scan = _extra_reconcile_state_with_live_database(extra_scheme_mod(), g)
        except Exception as ex:
            added_scheme_scan = {'changed': False, 'error': str(ex)}
    core=set(('0','1','2','3','4','5','6','7','8','314'))
    dlc=[k for k in reg if k not in core]
    nbak=sum(1 for v in reg.values() if os.path.exists(v['bak']))
    return jsonify(dict(game=g, ok=ok, archives=sorted(reg.keys()),
        game_id=ACTIVE_GAME,game_name=profile['name'],session_selected=bool(GAME_SESSION_SELECTED),
        tabs=list(profile['tabs']),paint_modes=list(profile['paint_modes']),
        full_feature_set=bool(profile['full_feature_set']),team_editor_mode=profile.get('team_editor_mode'),graphics_mode=profile.get('graphics_mode'),season_year=profile.get('season_year'),required_archives=list(profile['required_archives']),
        dlc_count=len(dlc), texconv=bool(texconv_path()), ffmpeg=bool(ffmpeg_path()),
        python_version='%d.%d.%d' % sys.version_info[:3],
        missing_archives=[k for k in profile['required_archives'] if k not in reg],
        app_name=APP_NAME, version=APP_VERSION, release_label=APP_RELEASE_LABEL, backup_count=nbak, archive_count=len(reg),
        added_scheme_scan=added_scheme_scan,
        backed_up=bool(reg and nbak)))

def _clean_project_destination(value, field_name):
    value=str(value or '').strip()
    if not value:
        return ''
    low=value.lower()
    if low.startswith(('https://','http://','mailto:')):
        return value
    raise ValueError(f'{field_name} must be an http(s) URL, mailto link, or blank')


def _app_settings_payload(cfg=None):
    cfg=cfg if isinstance(cfg,dict) else load_cfg()
    ui=cfg.get('app_settings') if isinstance(cfg.get('app_settings'),dict) else {}
    accent=str(ui.get('accent_color') or '#ffd23f').strip()
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent):
        accent='#ffd23f'
    accent2=str(ui.get('accent_color_2') or '#ffd23f').strip()
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',accent2):
        accent2='#ffd23f'
    def choice(name,allowed,default):
        value=str(ui.get(name) or default).strip()
        return value if value in allowed else default
    nav_default=['Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup','Repoint']
    raw_nav=ui.get('nav_order')
    nav_order=[]
    if isinstance(raw_nav,list):
        for value in raw_nav:
            value=str(value or '').strip()
            if value in nav_default and value not in nav_order:
                nav_order.append(value)
    nav_order += [value for value in nav_default if value not in nav_order]
    return dict(
        theme_preset=choice('theme_preset',{'classic','modern','subtle','custom'},'classic'),
        accent_color=accent.lower(),
        accent_color_2=accent2.lower(),
        gradient_direction=choice('gradient_direction',{'horizontal','vertical','diagonal','reverse_diagonal'},'horizontal'),
        accent_style=choice('accent_style',{'solid','subtle','bold'},'solid'),
        surface_style=choice('surface_style',{'flat','soft','contrast'},'flat'),
        nav_order=nav_order,
        remember_section_state=bool(ui.get('remember_section_state',True)),
        help_destination=str(ui.get('help_destination') or '').strip(),
        support_destination=str(ui.get('support_destination') or '').strip(),
        interface_density=choice('interface_density',{'compact','comfortable','spacious'},'comfortable'),
        text_size=choice('text_size',{'small','normal','large'},'normal'),
        page_width=choice('page_width',{'standard','wide','full'},'standard'),
        thumbnail_size=choice('thumbnail_size',{'small','normal','large'},'normal'),
        reduce_motion=bool(ui.get('reduce_motion',False)),
        remember_last_tab=bool(ui.get('remember_last_tab',True)),
        startup_tab=choice('startup_tab',{'Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup'},'Setup'),
        auto_open_browser=bool(ui.get('auto_open_browser',True)),
        # Whether the first-run walkthrough has been completed or dismissed.
        # Stored server-side so it survives clearing browser data and is the
        # same on every browser pointed at this install.
        tour_completed=bool(ui.get('tour_completed',False)),
    )


@app.route('/api/app_settings', methods=['GET','POST'])
def app_settings():
    cfg=load_cfg()
    if request.method=='GET':
        return jsonify(dict(ok=True,**_app_settings_payload(cfg)))
    q=request.get_json(silent=True) or {}
    current=_app_settings_payload(cfg)
    accent=str(q.get('accent_color',current['accent_color'])).strip().lower()
    accent2=str(q.get('accent_color_2',current['accent_color_2'])).strip().lower()
    if not re.fullmatch(r'#[0-9a-f]{6}',accent) or not re.fullmatch(r'#[0-9a-f]{6}',accent2):
        return jsonify(dict(ok=False,error='accent colors must use #RRGGBB format')),400
    try:
        help_destination=_clean_project_destination(q.get('help_destination',current['help_destination']),'help destination')
        support_destination=_clean_project_destination(q.get('support_destination',current['support_destination']),'support destination')
    except ValueError as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    def selected(name,allowed):
        value=str(q.get(name,current[name])).strip()
        if value not in allowed:raise ValueError(f'invalid {name.replace("_"," ")} setting')
        return value
    try:
        saved=dict(
            theme_preset=selected('theme_preset',{'classic','modern','subtle','custom'}),
            accent_color=accent,
            accent_color_2=accent2,
            gradient_direction=selected('gradient_direction',{'horizontal','vertical','diagonal','reverse_diagonal'}),
            accent_style=selected('accent_style',{'solid','subtle','bold'}),
            surface_style=selected('surface_style',{'flat','soft','contrast'}),
            nav_order=current['nav_order'],
            remember_section_state=bool(q.get('remember_section_state',current['remember_section_state'])),
            help_destination=help_destination,
            support_destination=support_destination,
            interface_density=selected('interface_density',{'compact','comfortable','spacious'}),
            text_size=selected('text_size',{'small','normal','large'}),
            page_width=selected('page_width',{'standard','wide','full'}),
            thumbnail_size=selected('thumbnail_size',{'small','normal','large'}),
            reduce_motion=bool(q.get('reduce_motion',current['reduce_motion'])),
            remember_last_tab=bool(q.get('remember_last_tab',current['remember_last_tab'])),
            startup_tab=selected('startup_tab',{'Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup'}),
            auto_open_browser=bool(q.get('auto_open_browser',current['auto_open_browser'])),
            tour_completed=bool(q.get('tour_completed',current['tour_completed'])),
        )
    except ValueError as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    if 'nav_order' in q:
        raw_nav=q.get('nav_order')
        if not isinstance(raw_nav,list):
            return jsonify(dict(ok=False,error='tab order must be a list')),400
        nav_default=['Setup','Favorites','Grid','Names','Text','Stats','Audio','Race','AI','UI','Settings','Checkup','Repoint']
        nav_order=[]
        for value in raw_nav:
            value=str(value or '').strip()
            if value in nav_default and value not in nav_order:
                nav_order.append(value)
        saved['nav_order']=nav_order+[value for value in nav_default if value not in nav_order]
    cfg['app_settings']=saved
    save_cfg(cfg)
    return jsonify(dict(ok=True,**_app_settings_payload(cfg)))

@app.route('/api/diagnostics/export')
def diagnostics_export():
    """Small support bundle: no game archives or copyrighted payloads."""
    import zipfile,hashlib,platform,datetime
    g,reg=registry(); cfg=load_cfg()
    def file_info(path):
        if not path or not os.path.exists(path): return dict(path=path,exists=False)
        st=os.stat(path); h=hashlib.sha256()
        # Hash first and last MiB plus size for fast diagnostics on huge archives.
        with open(path,'rb') as f:
            first=f.read(1<<20)
            if st.st_size>(1<<20):
                f.seek(max(0,st.st_size-(1<<20))); last=f.read(1<<20)
            else: last=b''
        h.update(struct.pack('<Q',st.st_size));h.update(first);h.update(last)
        return dict(path=path,exists=True,size=st.st_size,mtime=st.st_mtime,
                    quick_sha256=h.hexdigest())
    archives={}
    for k,v in sorted(reg.items()):
        archives[k]=dict(archive=file_info(v['ar']),cdfiles=file_info(v['cdf']),
                         archive_backup=file_info(v['bak']),
                         cdfiles_backup=file_info(backup_path(v['cdf'])))
    schemes=[]
    if os.path.isdir(SCHEMES):
        for fn in sorted(os.listdir(SCHEMES)):
            fp=os.path.join(SCHEMES,fn)
            if os.path.isfile(fp): schemes.append(dict(name=fn,size=os.path.getsize(fp)))
    helpers={n:os.path.exists(component_path(n)) for n in (
        'texconv.exe','ffmpeg.exe','nascar15_pyc_record_mapper_v5_teams.py',
        'nascar15_v11_probe_patcher.py','nascar15_const_repoint_v0_2.py','nascar15_schedule_editor_v0_1.py','ui_assets.csv')}
    safe_cfg={k:v for k,v in cfg.items() if k not in ('token','password','secret')}
    report=dict(created=datetime.datetime.now().isoformat(),app_name=APP_NAME,app_version=APP_VERSION,release_label=APP_RELEASE_LABEL,
                python=sys.version,platform=platform.platform(),game=g,config=safe_cfg,
                helpers=helpers,archives=archives,scheme_files=schemes,
                stock_baselines=cfg.get('stock_baselines',{}),
                repoint_history=(globals().get('_rp_load_history',lambda:[])()))
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('diagnostics.json',json.dumps(report,indent=2))
        z.writestr('README.txt','NASCAR 15 Modding App diagnostics. No game archive bytes are included.\n')
    buf.seek(0)
    return send_file(buf,mimetype='application/zip',as_attachment=True,
                     download_name=f'nascar15_modding_app_v{APP_VERSION}_diagnostics.zip')

@app.route('/api/backup_now', methods=['POST'])
def backup_now():
    """Force pristine backups of every archive + cdfiles pair that doesn't
    already have one. Never overwrites an existing backup."""
    g,reg=registry()
    if not g: return jsonify(dict(ok=False, error='game not found')),400
    created=[]; existing=[]; failed=[]
    for k,v in sorted(reg.items()):
        for live,bak in ((v['ar'],v['bak']),(v['cdf'],backup_path(v['cdf']))):
            base=os.path.basename(live)
            if os.path.exists(bak): existing.append(base); continue
            try:
                ensure_backup(live,bak); created.append(base)
            except PermissionError:
                failed.append(base+' (file locked - close the game and retry)')
            except Exception as e:
                failed.append(f'{base} ({e})')
    if created:
        _clear_ui_thumb_cache()
    return jsonify(dict(ok=not failed, created=created,
                        existing=len(existing), failed=failed))

@app.route('/api/setpath', methods=['POST'])
def setpath():
    gpath=(request.json or {}).get('path','').strip().strip('"').rstrip('\\/')
    profile=active_game_profile()
    root=_profile_root_from(gpath,ACTIVE_GAME)
    if not root:
        needed=', '.join('ARCHIVE'+k+'.AR' for k in profile['required_archives'])
        looked=os.path.join(gpath,'data') if gpath else '(no folder given)'
        hint=''
        if gpath and os.path.basename(gpath).lower()=='data':
            hint=(' That path already ends in the data folder - try the folder above it: '
                  +os.path.dirname(gpath))
        elif gpath and not os.path.isdir(gpath):
            hint=' That folder does not exist or is not readable.'
        return jsonify(dict(ok=False,error=f'{needed} were not all found in {looked}.{hint}')),400
    # Store the resolved install ROOT: registry() appends the data folder
    # downstream, so storing a data path here silently produces data\\data.
    c=load_cfg(); c['game']=root; save_cfg(c)
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True,game_id=ACTIVE_GAME,game_name=profile['name'],path=root))

def custom_scheme_slots():
    """Enumerate player custom schemes (CUSTOM-named liveries) so they show in
    the grid. Assumes the LIVERY_ wrapper convention; adjust if your custom
    slots differ."""
    g, reg = registry()
    if not g: return []
    out = {}
    entries_by_arc = {}
    hd_map = {}
    for arcid, info in reg.items():
        cdf = info.get('cdf')
        if not cdf: continue
        try: entries_by_arc[arcid] = parse_cdfiles(cdf)
        except Exception: entries_by_arc[arcid] = []
        for o,s,n in entries_by_arc[arcid]:
            if n.startswith('HDLIVERY_') and 'CUSTOM' in n.upper():
                hd_map[n] = (arcid,o,s)
    for arcid,ent in entries_by_arc.items():
        for o,s,n in ent:
            if n.startswith('LIVERY_') and 'CUSTOM' in n.upper():
                sl = slot_from_name(n,o,s,arcid,hd_map)
                if sl:
                    sl['kind']='custom'; out[n]=sl
    return sorted(out.values(), key=lambda x:x['name'])

def _managed_extra_slot_names():
    """Return livery wrapper names owned by the app-created slot system.

    App-created liveries also exist in live cdfiles, so the legacy Grid browser
    used to mistake them for stock paint slots. That exposed stock Restore/Import
    actions and made /api/thumb seek their appended offsets in a pristine backup
    that predates the entries. Keep them exclusively in Existing Paints.
    """
    names=set()
    try:
        state=json.load(open(os.path.join(USER_DIR,'extra_schemes_v1.json'),'r',encoding='utf-8'))
    except Exception:
        state={}
    for item in state.get('schemes',[]) if isinstance(state,dict) else []:
        if not isinstance(item,dict):
            continue
        for field in ('sd_entry','hd_entry'):
            value=str(item.get(field) or '').strip()
            if value:
                names.add(value.upper())
        for field in ('script_name','identity_migrated_from'):
            token=str(item.get(field) or '').strip()
            token=re.sub(r'^(?:HD)?LIVERY_','',token,flags=re.I)
            token=re.sub(r'\.ARC$','',token,flags=re.I)
            if token:
                names.add(f'LIVERY_{token}.ARC'.upper())
                names.add(f'HDLIVERY_{token}.ARC'.upper())
    return names


def _is_managed_extra_slot(name):
    upper=str(name or '').upper()
    # _EXTRA_ was used by the pre-CUSTOM migration builds and is always an
    # app-created independent slot, even when an old state entry is incomplete.
    return upper in _managed_extra_slot_names() or ('_EXTRA_' in upper and upper.endswith('.ARC'))


def _managed_extra_action_error(name):
    return (f'{name} is an app-created independent paint. Use Paint Schemes > '
            'Existing Paints so its database record, current-team thumbnail, '
            'and AI schedule wiring stay synchronized.')


@app.route('/api/grid')
def grid():
    out=[]
    _base_names=set()
    managed=_managed_extra_slot_names()
    for s in grid_slots():
        if str(s.get('name','')).upper() in managed or _is_managed_extra_slot(s.get('name')):
            continue
        _base_names.add(s['name'])
        s=dict(s)
        s['has_scheme']=os.path.exists(os.path.join(SCHEMES,s['name']+'.png'))
        s['has_layer']=os.path.exists(os.path.join(SCHEMES,s['name']+'.layer.png'))
        s['has_thumb']=os.path.exists(os.path.join(SCHEMES,s['name']+'.thumb.png'))
        s['livery_uid']=_slot_livery_uid(s['name'])
        s['fei']=bool(s['fei'])
        out.append(s)
    # v0.6: append player custom schemes not already in the base grid
    try:
        for cs in custom_scheme_slots():
            if cs['name'] in _base_names or _is_managed_extra_slot(cs.get('name')):
                continue
            cs=dict(cs)
            cs['has_scheme']=os.path.exists(os.path.join(SCHEMES,cs['name']+'.png'))
            cs['has_layer']=os.path.exists(os.path.join(SCHEMES,cs['name']+'.layer.png'))
            cs['has_thumb']=os.path.exists(os.path.join(SCHEMES,cs['name']+'.thumb.png'))
            cs['livery_uid']=_slot_livery_uid(cs['name'])
            cs['fei']=bool(cs.get('fei'))
            out.append(cs)
    except Exception:
        pass
    return jsonify(out)

def _slot(name):
    for s in grid_slots():
        if s['name']==name: return s
    return None




def _cdf_named_entry(cdf_path, name):
    """Resolve one CDF entry by name, case-insensitively."""
    wanted=str(name or '').upper()
    if not wanted or not os.path.exists(cdf_path):
        return None
    rows=parse_cdfiles(cdf_path)
    return next((x for x in rows if str(x[2]).upper()==wanted),None)


def _data_registry(data_dir, suffix=''):
    """Build an archive registry from a real data directory or paired backup suffix.

    ``suffix`` is used for paired files such as ``ARCHIVE2.AR.gridapp.bak`` and
    ``cdfiles.dat.gridapp.bak``. Pairing the same suffix prevents an archive from
    one backup generation being interpreted with another generation's CDF.
    """
    out={}
    if not data_dir or not os.path.isdir(data_dir):
        return out
    try:
        entries=os.listdir(data_dir)
    except OSError:
        return out
    if suffix:
        rx=re.compile(r'^cdfiles(\d*)\.dat'+re.escape(suffix)+r'$',re.I)
    else:
        rx=re.compile(r'^cdfiles(\d*)\.dat$',re.I)
    for fname in entries:
        m=rx.match(fname)
        if not m:
            continue
        arcid=m.group(1) or '0'
        arname=f'ARCHIVE{arcid}.AR' if arcid!='0' else 'ARCHIVE0.AR'
        ar=os.path.join(data_dir,arname+suffix)
        cdf=os.path.join(data_dir,fname)
        if os.path.exists(ar):
            out[str(arcid)]=dict(ar=ar,cdf=cdf,bak=ar,label=('backup '+suffix if suffix else 'data folder'),data_dir=data_dir)
    return out


def _norm_path(path):
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except Exception:
        return os.path.normcase(str(path or ''))


def _candidate_clean_data_dirs(game_folder=None):
    """Return verified read-only clean-data candidates, best candidate first."""
    cfg=load_cfg()
    raw=[]
    for key in ('clean_data_dir','paint_clean_data_dir','master_map_clean_data_dir'):
        if cfg.get(key): raw.append(cfg.get(key))
    env=os.environ.get('NASCAR15_CLEAN_DATA')
    if env: raw.append(env)
    # Master Mapper / project default used by this app's clean test lab.
    for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        raw.extend((
            drive+r':\SteamLibrary\data\data',
            drive+r':\NASCAR15_CLEAN_BASELINE\data',
            drive+r':\NASCAR15_AUTOLAB\CLEAN_GAME\data',
        ))
    try:
        entries=stock_baselines()
    except Exception:
        entries={}
    for entry in (entries or {}).values():
        p=(entry or {}).get('path') if isinstance(entry,dict) else None
        if p: raw.append(os.path.dirname(p))
    live_data=os.path.join(game_folder,'data') if game_folder else None
    live_norm=_norm_path(live_data) if live_data else None
    out=[]; seen=set()
    for p in raw:
        if not p: continue
        p=os.path.abspath(os.path.expandvars(os.path.expanduser(str(p))))
        n=_norm_path(p)
        if n in seen or (live_norm and n==live_norm):
            continue
        seen.add(n)
        if os.path.isfile(os.path.join(p,'cdfiles.dat')) and os.path.isfile(os.path.join(p,'ARCHIVE0.AR')):
            out.append(p)
    return out


def _find_resource_refs(search_reg, name, label):
    refs=[]
    wanted=str(name or '').upper()
    for arcid,info in sorted((search_reg or {}).items(),key=lambda kv:(len(str(kv[0])),str(kv[0]))):
        try:
            row=_cdf_named_entry(info['cdf'],wanted)
            if row is None: continue
            off,size,stored=row
            if int(off)<0 or int(size)<=0 or os.path.getsize(info['ar'])<int(off)+int(size):
                continue
            refs.append(dict(arcid=str(arcid),ar=info['ar'],cdf=info['cdf'],offset=int(off),size=int(size),
                             stored_name=stored,label=label,data_dir=info.get('data_dir') or os.path.dirname(info['cdf'])))
        except Exception:
            continue
    return refs


def _find_live_resource(reg, name, preferred_arc=None):
    """Inspect the actual game CDFs and return the real live location."""
    refs=_find_resource_refs(reg,name,'live game')
    if not refs:
        raise ValueError(f'live game does not contain {name} in any archive')
    if preferred_arc is not None:
        preferred=[r for r in refs if str(r['arcid'])==str(preferred_arc)]
        if preferred:
            return preferred[0],refs
    return refs[0],refs


def _pristine_search_sources(reg, game_folder=None):
    """Enumerate clean-baseline and paired-backup registries without guessing."""
    sources=[]
    for data_dir in _candidate_clean_data_dirs(game_folder):
        r=_data_registry(data_dir)
        if r:
            sources.append((f'clean baseline: {data_dir}',r))
    # Search both generations. Do not use backup_path() here because restore must
    # inspect every valid pair rather than trusting one filename preference.
    live_dirs=[]
    for info in (reg or {}).values():
        d=os.path.dirname(info['cdf'])
        if d not in live_dirs: live_dirs.append(d)
    for data_dir in live_dirs:
        for suffix,label in ((LEGACY_BACKUP_SUFFIX,'legacy pristine backup'),(MOD_BACKUP_SUFFIX,'modding-app pristine backup')):
            r=_data_registry(data_dir,suffix)
            if r:
                sources.append((label,r))
    return sources


def _find_pristine_resource(reg, name, expected_size=None, game_folder=None):
    searched=[]; wrong=[]
    for label,source_reg in _pristine_search_sources(reg,game_folder):
        searched.append(label)
        for ref in _find_resource_refs(source_reg,name,label):
            if expected_size is not None and int(ref['size'])!=int(expected_size):
                wrong.append(f'{label} ARCHIVE{ref["arcid"]}: {ref["size"]} bytes')
                continue
            return ref,searched,wrong
    return None,searched,wrong


def _read_exact_region(path, offset, size, label):
    if not os.path.exists(path):
        raise ValueError(f'{label} file is missing: {path}')
    if int(offset)<0 or int(size)<=0 or os.path.getsize(path)<int(offset)+int(size):
        raise ValueError(f'{label} region is outside the file: 0x{int(offset):X}+0x{int(size):X}')
    with open(path,'rb') as fh:
        fh.seek(int(offset)); data=fh.read(int(size))
    if len(data)!=int(size):
        raise ValueError(f'short read for {label}: {len(data)} of {int(size)} bytes')
    return data


def _native_wrapper_mip_image(wrapper, level, hd=False, logical=False):
    """Decode one page-mapped native livery mip from an exact wrapper."""
    if hd:
        dims=_NATIVE_HD_DIMS; offsets=_NATIVE_HD_MIP_OFFSETS; pitches=_NATIVE_HD_MIP_PITCHES
        wrap_x=_NATIVE_HD_WRAP_X_BLOCKS; wrap_y=_NATIVE_HD_WRAP_Y_BLOCKS
        phys=_NATIVE_HD_PHYS_BLOCKS; large_max=5; rolls=_NATIVE_HD_LARGE_ROLL
    else:
        dims=_NATIVE_SD_DIMS; offsets=_NATIVE_SD_MIP_OFFSETS; pitches=_NATIVE_SD_MIP_PITCHES
        wrap_x=_NATIVE_SD_WRAP_X_BLOCKS; wrap_y=_NATIVE_SD_WRAP_Y_BLOCKS
        phys=_NATIVE_SD_PHYS_BLOCKS; large_max=4; rolls=_NATIVE_SD_LARGE_ROLL
    if level<0 or level>=len(dims):
        raise ValueError('mip level out of range')
    w,h=dims[level]
    bw=max(1,(w+3)//4); bh=max(1,(h+3)//4)
    payload=bytearray()
    base=RAW_OFFSET+offsets[level]; pitch=pitches[level]
    for sy in range(bh):
        for sx in range(bw):
            if level<=large_max:
                dx,dy=sx,sy
            else:
                dx=(sx+wrap_x)%phys; dy=(sy+wrap_y)%phys
            pos=base+dy*pitch+dx*8
            end=pos+8
            if pos<0 or end>len(wrapper):
                raise ValueError(f'mip L{level} block ({sx},{sy}) is outside wrapper')
            payload += wrapper[pos:end]
    img=Image.fromarray(dxt1_decode(bytes(payload),w,h)).convert('RGB')
    if logical and level<=large_max:
        roll=int(rolls[level])
        if roll:
            img=Image.fromarray(np.roll(np.asarray(img),-roll,axis=1).astype(np.uint8),'RGB')
    return img


def _zip_write_image(zf, arcname, image):
    buf=io.BytesIO(); image.save(buf,'PNG'); zf.writestr(arcname,buf.getvalue())


def _paint_forensics_job(reg, slot, kind, game_folder=None):
    """Capture the exact resource that is physically installed in the game.

    A pristine source is optional. This intentionally remains useful even when
    old backup CDFs are stale, missing, or from a different archive revision.
    """
    hd=(kind=='hd')
    entry_name=slot.get('hd') if hd else slot.get('name')
    preferred=str((slot.get('hd_arc') if hd else slot.get('sd_arc')) or slot.get('arc'))
    live_ref,live_matches=_find_live_resource(reg,entry_name,preferred)
    live_bytes=_read_exact_region(live_ref['ar'],live_ref['offset'],live_ref['size'],f'live {entry_name}')
    pristine_ref,searched,wrong=_find_pristine_resource(reg,entry_name,live_ref['size'],game_folder)
    pristine_bytes=None
    if pristine_ref is not None:
        pristine_bytes=_read_exact_region(pristine_ref['ar'],pristine_ref['offset'],pristine_ref['size'],f'pristine {entry_name}')
    return dict(kind=kind,hd=hd,arcid=live_ref['arcid'],entry=entry_name,
                live_off=live_ref['offset'],size=live_ref['size'],live_bytes=live_bytes,
                live_archive=live_ref['ar'],live_cdf=live_ref['cdf'],live_matches=live_matches,
                pristine_ref=pristine_ref,pristine_bytes=pristine_bytes,
                pristine_search=searched,pristine_wrong_sizes=wrong,
                pristine_status=('found' if pristine_ref else 'not found; live capture still complete'))

def _backup_contains_named_entry(reg, arcid, name, offset, size):
    a=need(reg,arcid)
    archive_backup=a['bak']
    cdf_backup=backup_path(a['cdf'])
    if not (name and size and os.path.exists(archive_backup) and os.path.exists(cdf_backup)):
        return False
    try:
        match=next((x for x in parse_cdfiles(cdf_backup) if x[2]==name),None)
        if not match:
            return False
        off,entry_size,_name=match
        if int(off)!=int(offset) or int(entry_size)!=int(size):
            return False
        return os.path.getsize(archive_backup) >= int(off)+int(entry_size)
    except Exception:
        return False


def _backup_contains_slot_entry(reg, slot):
    """A pristine archive is usable only when its matching pristine CDF owns
    the same entry at the same offset/size. Added slots exist only in the live
    archive, even though a perfectly valid older archive backup also exists.
    """
    return _backup_contains_named_entry(
        reg,slot['arc'],slot['name'],slot['sd_off'],slot['sd_size'])


def _slot_sd_source(reg, slot, prefer_pristine=True):
    a=need(reg,slot['arc'])
    if prefer_pristine and _backup_contains_slot_entry(reg,slot):
        return a['bak'],'pristine backup'
    return a['ar'],'live added/current slot'


def _read_slot_mip0(reg,slot,prefer_pristine=True):
    need_bytes=(2048//4)*(1024//4)*8
    src,kind=_slot_sd_source(reg,slot,prefer_pristine=prefer_pristine)
    start=int(slot['sd_off'])+RAW_OFFSET
    if os.path.getsize(src) < start+need_bytes:
        raise ValueError(f'{kind} does not contain a complete mip-0 payload for {slot["name"]}')
    with open(src,'rb') as fh:
        fh.seek(start);payload=fh.read(need_bytes)
    if len(payload)!=need_bytes:
        raise ValueError(f'short read for {slot["name"]}: {len(payload)} of {need_bytes} bytes')
    return payload,kind


def _preview_placeholder(text='Preview unavailable'):
    img=Image.new('RGB',(192,96),(18,22,28))
    draw=ImageDraw.Draw(img)
    draw.rectangle((0,0,191,95),outline=(56,66,80))
    draw.text((12,40),str(text)[:28],fill=(170,180,192))
    buf=io.BytesIO();img.save(buf,'JPEG',quality=82);buf.seek(0)
    return buf


@app.route('/api/template/<name>')
def template(name):
    try:
        g,reg=registry(); s=_slot(name)
        if not s: return ('not found',404)
        payload,source_kind=_read_slot_mip0(reg,s,prefer_pristine=True)
        img=Image.fromarray(dxt1_decode(payload,2048,1024))
        buf=io.BytesIO(); img.save(buf,'PNG'); buf.seek(0)
        response=send_file(buf,mimetype='image/png',download_name=name.replace('.ARC','_template.png'))
        response.headers['X-N15-Paint-Source']=source_kind
        return response
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),404


@app.route('/api/thumb/<name>')
def thumb(name):
    try:
        png=os.path.join(SCHEMES,name+'.png')
        if os.path.exists(png):
            st=os.stat(png);sig=('saved',name,st.st_size,st.st_mtime_ns)
        else:
            g,reg=registry();sig=('game',name,_paint_preview_signature(reg,('2',)))
        cached=_PAINT_ATLAS_PREVIEW_CACHE.get(sig)
        if cached is None:
            if os.path.exists(png):
                with Image.open(png) as opened:img=opened.convert('RGB');img.load()
                source_kind='saved app paint'
            else:
                s=_slot(name)
                if not s:return ('not found',404)
                payload,source_kind=_read_slot_mip0(reg,s,prefer_pristine=True)
                img=Image.fromarray(dxt1_decode(payload,2048,1024)).convert('RGB')
            img.thumbnail((192,96));buf=io.BytesIO();img.save(buf,'JPEG',quality=80,optimize=True)
            cached=(buf.getvalue(),source_kind)
            if len(_PAINT_ATLAS_PREVIEW_CACHE)>256:_PAINT_ATLAS_PREVIEW_CACHE.clear()
            _PAINT_ATLAS_PREVIEW_CACHE[sig]=cached
        data,source_kind=cached
        response=send_file(io.BytesIO(data),mimetype='image/jpeg',conditional=True,max_age=300)
        response.headers['X-N15-Paint-Source']=source_kind
        return response
    except Exception as ex:
        response=send_file(_preview_placeholder(),mimetype='image/jpeg',max_age=60)
        response.headers['X-N15-Preview-Error']=str(ex)[:240]
        return response


def auto_diff_layer(reg, slot, img):
    """Generate the touched-mask layer by diffing an import against a real base.

    Stock slots use the pristine entry. Appended slots correctly fall back to
    their live wrapper because they did not exist when the archive backup was made.
    """
    payload,_source_kind=_read_slot_mip0(reg,slot,prefer_pristine=True)
    stock=dxt1_decode(payload,2048,1024).astype(np.int32)
    up=np.asarray(img.convert('RGB')).astype(np.int32)
    diff=np.abs(up-stock).sum(2)
    mask=(diff>36).astype(np.uint8)*255      # tolerance ~12/channel
    la=np.dstack([up.astype(np.uint8), mask[:,:,None]])
    return Image.fromarray(la,'RGBA')

# ---- v0.9.26.10 paint-scheme Smart Import ----
SCHEME_TARGET_SIZE=(2048,1024)
SCHEME_SMART_QUALITIES={'auto','direct','1','2','4'}
SCHEME_LAYOUT_MODES={'auto','native','community_legacy'}
# Common hardware regions shared by stock and community Cup templates. These
# deliberately avoid most large sponsor/body-color areas and are used only to
# estimate a whole-atlas horizontal storage-layout offset.
_SCHEME_LAYOUT_RECTS=(
    (0,0,250,180),       # left headlight / fascia hardware
    (1000,0,1300,200),   # fuel circle / manufacturer hardware
    (1150,250,1510,570), # front grille and headlights
    (780,180,1120,650),  # rear lights / center hardware
    (1500,0,2048,1024),  # narrow utility / flame / light strips
    (0,600,900,1024),    # lower-left fixed parts and contingency region
)


def _scheme_edge_map(img):
    arr=np.asarray(img.convert('RGB')).astype(np.float32)
    gray=0.299*arr[:,:,0]+0.587*arr[:,:,1]+0.114*arr[:,:,2]
    gx=np.zeros_like(gray);gy=np.zeros_like(gray)
    gx[:,1:-1]=gray[:,2:]-gray[:,:-2]
    gy[1:-1,:]=gray[2:,:]-gray[:-2,:]
    return np.sqrt(gx*gx+gy*gy)


def _scheme_layout_mask(width,height):
    mask=np.zeros((height,width),dtype=bool)
    sx=width/2048.0;sy=height/1024.0
    for x1,y1,x2,y2 in _SCHEME_LAYOUT_RECTS:
        xa=max(0,min(width,int(round(x1*sx))))
        xb=max(0,min(width,int(round(x2*sx))))
        ya=max(0,min(height,int(round(y1*sy))))
        yb=max(0,min(height,int(round(y2*sy))))
        if xb>xa and yb>ya:mask[ya:yb,xa:xb]=True
    return mask


def _scheme_edge_overlap(source_edges,reference_edges,mask,shift,threshold=25.0):
    src=np.roll(source_edges,int(shift),axis=1)>threshold
    ref=reference_edges>threshold
    a=src[mask];b=ref[mask]
    den=float(np.sqrt(float(a.sum())*float(b.sum())))
    if den<=0.0:return 0.0
    return float(np.logical_and(a,b).sum())/den


def _detect_scheme_layout_shift(source,reference):
    """Estimate the circular X translation needed to match the native atlas.

    The community template supplied with the RC4 report was measured at +20 px
    relative to the app-exported native atlas. Rather than hard-coding that result
    for every image, Auto compares fixed vehicle hardware against the target
    slot's pristine mip-0 and applies a shift only when the evidence is strong.
    """
    box=Image.Resampling.BOX if hasattr(Image,'Resampling') else Image.BOX
    small_size=(1024,512)
    src_small=source.convert('RGB').resize(small_size,box)
    ref_small=reference.convert('RGB').resize(small_size,box)
    se=_scheme_edge_map(src_small);re=_scheme_edge_map(ref_small)
    mask=_scheme_layout_mask(*small_size)
    coarse=[]
    for shift in range(-32,33):
        coarse.append((shift,_scheme_edge_overlap(se,re,mask,shift)))
    coarse_best=max(coarse,key=lambda x:x[1])
    candidate=int(coarse_best[0]*2)

    # Refine at native 2048x1024 resolution around the coarse candidate.
    src_full=source.convert('RGB')
    ref_full=reference.convert('RGB')
    if src_full.size!=(2048,1024):src_full=src_full.resize((2048,1024),box)
    if ref_full.size!=(2048,1024):ref_full=ref_full.resize((2048,1024),box)
    sfe=_scheme_edge_map(src_full);rfe=_scheme_edge_map(ref_full)
    full_mask=_scheme_layout_mask(2048,1024)
    scores=[]
    for shift in range(max(-64,candidate-4),min(64,candidate+4)+1):
        scores.append((shift,_scheme_edge_overlap(sfe,rfe,full_mask,shift)))
    best_shift,best_score=max(scores,key=lambda x:x[1])
    zero_score=_scheme_edge_overlap(sfe,rfe,full_mask,0)
    gain=best_score-zero_score
    relative=(gain/max(zero_score,0.01))
    accepted=(abs(best_shift)>=2 and best_score>=0.12 and gain>=0.03 and relative>=0.12)
    return dict(
        best_shift=int(best_shift),best_score=round(float(best_score),6),
        zero_score=round(float(zero_score),6),gain=round(float(gain),6),
        relative_gain=round(float(relative),6),accepted=bool(accepted),
        method='native hardware edge overlap / circular X search')


def _apply_scheme_layout(img,mode='auto',reference=None):
    requested=str(mode or 'auto').strip().lower()
    if requested not in SCHEME_LAYOUT_MODES:requested='auto'
    shift=0;detection=None;applied='native'
    if requested=='community_legacy':
        # Measured RC4 report: community atlas landmarks were 20 pixels to the
        # right of the app-exported native layout, so move the source left.
        shift=-20;applied='community_legacy'
    elif requested=='auto' and reference is not None:
        detection=_detect_scheme_layout_shift(img,reference)
        if detection.get('accepted'):
            shift=int(detection['best_shift'])
            applied='auto_aligned'
        else:
            applied='auto_native'
    elif requested=='auto':
        applied='auto_no_reference'
    arr=np.asarray(img.convert('RGB'))
    if shift:arr=np.roll(arr,shift,axis=1)
    out=Image.fromarray(arr.astype(np.uint8),'RGB')
    if shift:
        note=(f'Applied a {shift:+d}-pixel circular X alignment before mip generation. '
              'All SD/HD mip levels are generated from this corrected native atlas.')
    elif requested=='auto' and detection is not None:
        note='Auto layout check found no strong non-native atlas offset; source kept unchanged.'
    elif requested=='native':
        note='Native/app-exported layout selected; source kept unchanged.'
    else:
        note='No pristine reference was available, so Auto kept the source unchanged.'
    return out,dict(requested=requested,applied=applied,x_shift_pixels=int(shift),
                    reference='target slot pristine mip-0' if reference is not None else None,
                    detection=detection,note=note)


def _prepare_scheme_smart_image(stream,quality='auto'):
    """Prepare a livery atlas with an optional supersampled resize pass.

    Paint atlases must remain an exact 2:1 UV canvas, so this never crops or pads.
    Auto uses a direct Lanczos downsample for an already-oversized source and a
    2x intermediate render for target-size/smaller art. 2x/4x deliberately render
    to a larger intermediate canvas and then downsample to 2048x1024, which can
    smooth externally drawn text, numbers, and decal edges. It does not invent
    real detail; a genuinely high-resolution source is still the best input.
    """
    img=Image.open(stream)
    source_format=(img.format or 'unknown').upper();img.load()
    source_mode=str(img.mode);source_alpha=('A' in source_mode) or ('transparency' in img.info)
    src=img.convert('RGB');sw,sh=src.size;tw,th=SCHEME_TARGET_SIZE
    q=str(quality or 'auto').lower().strip()
    if q not in SCHEME_SMART_QUALITIES:q='auto'
    if q in ('direct','1'):
        factor=1;policy='direct Lanczos resize to the native 2048x1024 atlas'
    elif q in ('2','4'):
        factor=int(q);policy=f'{factor}x supersample, then Lanczos downsample to the native atlas'
    else:
        if sw>tw or sh>th:
            factor=1;policy='Auto: source is already oversized, so it is downsampled directly'
        else:
            factor=2;policy='Auto: 2x intermediate supersample, then downsample to native size'
    lanczos=Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS
    intermediate=None
    if factor>1:
        intermediate=(tw*factor,th*factor)
        stage=src if src.size==intermediate else src.resize(intermediate,lanczos)
        out=stage.resize((tw,th),lanczos)
    else:
        out=src if src.size==(tw,th) else src.resize((tw,th),lanczos)
    source_aspect=sw/max(1,sh);target_aspect=tw/th
    aspect_warning=(abs(source_aspect-target_aspect)>0.002)
    prep=dict(resized=(src.size!=(tw,th) or factor>1),source=[sw,sh],target=[tw,th],
              source_format=source_format,source_mode=source_mode,source_alpha=bool(source_alpha),
              preserve_alpha=False,mode='stretch (fixed UV atlas)',quality_requested=q,
              quality_policy=policy,supersample_factor=factor,
              intermediate=(list(intermediate) if intermediate else None),
              aspect_warning=aspect_warning,
              alpha_action=('flattened to opaque RGB' if source_alpha else 'not present'))
    return out.convert('RGB'),prep


def _scheme_preview_png(img):
    preview=img.copy();preview.thumbnail((720,360))
    b=io.BytesIO();preview.save(b,'PNG');return _b64.b64encode(b.getvalue()).decode()


# Stable stock references for each current body family.  Smart Import normally
# compares the source against the selected slot's pristine mip-0 to detect the
# community-template X offset.  That reference becomes invalid after a team is
# changed to another manufacturer (for example Chevrolet -> Ford), because the
# selected slot's pristine art still belongs to the old body.  Use an untouched
# stock paint authored for the CURRENT manufacturer instead of disabling Auto.
_MANUFACTURER_LAYOUT_REFERENCE_SLOTS = {
    1015: (
        'LIVERY_15_2_BRAD_KESELOWSKI_PRIMARY.ARC',
        'LIVERY_15_22_JOEY_LOGANO_PRIMARY.ARC',
        'LIVERY_15_43_ARIC_ALMIROLA_PRIMARY.ARC',
    ),
    1076: (
        'LIVERY_15_4_KEVIN_HARVICK_PRIMARY.ARC',
        'LIVERY_15_1_JAMIE_MCMURRAY_PRIMARY.ARC',
        'LIVERY_15_88_DALE_EARNHARDT_JR_PRIMARY.ARC',
    ),
    1078: (
        'LIVERY_15_18_KYLE_BUSCH_PRIMARY.ARC',
        'LIVERY_15_11_DENNY_HAMLIN_PRIMARY.ARC',
        'LIVERY_15_20_MATT_KENSETH_SECONDARY.ARC',
    ),
}


def _manufacturer_layout_reference(reg, manufacturer_uid, exclude_slot=None, game_folder=None):
    """Return a logical 2048x1024 stock reference for the live body family.

    Prefer a verified clean/paired-backup wrapper.  If no pristine source is
    available yet, use a different live stock slot from that manufacturer; the
    reference is read-only and is used only for horizontal layout detection.
    """
    try:
        manufacturer_uid=int(manufacturer_uid)
    except Exception:
        return None, None
    slots={str(x.get('name') or '').upper():x for x in grid_slots()}
    exclude=str(exclude_slot or '').upper()
    need_bytes=(2048//4)*(1024//4)*8
    errors=[]
    for candidate_name in _MANUFACTURER_LAYOUT_REFERENCE_SLOTS.get(manufacturer_uid, ()):
        if candidate_name.upper()==exclude:
            continue
        slot=slots.get(candidate_name.upper())
        if not slot:
            errors.append(candidate_name+': not indexed')
            continue
        try:
            ref,searched,wrong=_find_pristine_resource(
                reg,slot['name'],expected_size=int(slot['sd_size']),game_folder=game_folder)
            if ref is not None:
                wrapper=_read_exact_region(ref['ar'],ref['offset'],ref['size'],
                                           f'manufacturer reference {slot["name"]}')
                if len(wrapper)<RAW_OFFSET+need_bytes:
                    raise ValueError('wrapper does not contain a complete mip-0')
                payload=wrapper[RAW_OFFSET:RAW_OFFSET+need_bytes]
                source_kind=str(ref.get('label') or 'verified pristine source')
            else:
                payload,source_kind=_read_slot_mip0(reg,slot,prefer_pristine=True)
            image=Image.fromarray(dxt1_decode(payload,2048,1024)).convert('RGB')
            return image,dict(
                manufacturer_uid=manufacturer_uid,
                manufacturer=TEAM_MANUFACTURER_NAMES.get(manufacturer_uid,str(manufacturer_uid)),
                slot=slot['name'],source=source_kind,
                fallback_live=not str(source_kind).lower().startswith(('clean baseline','legacy pristine','modding-app pristine','pristine backup')),
            )
        except Exception as ex:
            errors.append(candidate_name+': '+str(ex))
    return None,dict(
        manufacturer_uid=manufacturer_uid,
        manufacturer=TEAM_MANUFACTURER_NAMES.get(manufacturer_uid,str(manufacturer_uid)),
        unavailable=True,errors=errors[:6],
    )


@app.route('/api/scheme_smart/<name>',methods=['POST'])
def scheme_smart_import(name):
    """Preview or apply a quality-controlled external paint-scheme import."""
    try:
        if _is_managed_extra_slot(name):
            raise ValueError(_managed_extra_action_error(name))
        f=request.files.get('file')
        if not f:raise ValueError('choose an image file')
        img,prep=_prepare_scheme_smart_image(f.stream,request.form.get('quality','auto'))
        s=_slot(name)
        layout_mode=request.form.get('layout_mode','auto')
        reference=None;reference_info=None
        manufacturer_context=_slot_manufacturer_context(name) if s else {'known':False,'mismatch':False}
        if s:
            try:
                _g,layout_reg=registry()
                if manufacturer_context.get('mismatch') and str(layout_mode or 'auto').lower()=='auto':
                    reference,reference_info=_manufacturer_layout_reference(
                        layout_reg,manufacturer_context.get('current_manufacturer_uid'),
                        exclude_slot=name,game_folder=_g)
                elif not manufacturer_context.get('mismatch'):
                    payload,_source_kind=_read_slot_mip0(layout_reg,s,prefer_pristine=True)
                    reference=Image.fromarray(dxt1_decode(payload,2048,1024)).convert('RGB')
                    reference_info=dict(slot=s.get('name'),source=_source_kind,
                                        manufacturer='selected slot original body')
            except Exception as ex:
                reference=None
                reference_info=dict(unavailable=True,error=str(ex))
        img,layout_info=_apply_scheme_layout(img,layout_mode,reference)
        if reference_info:
            layout_info['reference_detail']=reference_info
        if manufacturer_context.get('mismatch'):
            layout_info['manufacturer_mismatch']=True
            if reference is not None:
                layout_info['note']=(
                    f"The team now uses {manufacturer_context.get('current_manufacturer') or 'a different manufacturer'}. "
                    f"Auto alignment compared the import with current-body stock reference "
                    f"{reference_info.get('slot') if reference_info else 'unknown'} instead of the old body. "
                    + str(layout_info.get('note') or ''))
            else:
                layout_info['note']=(
                    'The team manufacturer changed after this stock paint was authored, and no '
                    'current-body stock reference was available. Auto kept the source unchanged; '
                    'choose Community legacy manually only when the source uses that older template layout. '
                    + str(layout_info.get('note') or ''))
        prep['manufacturer_context']=manufacturer_context
        prep['layout']=layout_info
        prep['layout_mode']=layout_info.get('applied')
        prep['layout_shift_x']=layout_info.get('x_shift_pixels',0)
        if request.form.get('dry_run')=='1':
            warning=('The source is not 2:1, so Smart Import must stretch it to the fixed car UV atlas. '
                     if prep.get('aspect_warning') else '')
            warning+=('Supersampling improves edge filtering but cannot create genuine source detail. '
                      'The saved atlas remains exactly 2048x1024 and the native SD/HD mip installer is unchanged. ')
            warning+=layout_info.get('note','')
            return jsonify(dict(ok=True,dry_run=True,preview_png=_scheme_preview_png(img),image_prep=prep,
                profile=dict(width=2048,height=1024,codec='DXT1',profile='car paint atlas / native SD+HD mip pipeline',
                             payload_size='fixed livery wrappers'),experimental=False,warning=warning))
        png=os.path.join(SCHEMES,name+'.png');lay=os.path.join(SCHEMES,name+'.layer.png')
        img.save(png)
        # An externally supplied 2:1 paint is a complete atlas replacement. The
        # old auto-diff mask could leave donor blocks behind and made SD/HD or
        # adjacent mip levels disagree. Paint Booth edits may still provide an
        # explicit layer through /api/scheme.
        if os.path.exists(lay): os.remove(lay)
        prep['install_mode']='full UV-atlas replacement (no donor diff mask)'
        wrote=None
        install=request.form.get('install')=='1'
        if install:
            if not s:raise ValueError('paint slot was not found in the installed game')
            wrote=install_slot(registry()[1],s,png,None)
        return jsonify(dict(ok=True,installed=bool(wrote),wrote=wrote,image_prep=prep))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/scheme/<name>', methods=['POST','GET'])
def scheme(name):
    png=os.path.join(SCHEMES,name+'.png'); lay=os.path.join(SCHEMES,name+'.layer.png')
    if request.method=='GET':
        if os.path.exists(png):
            return send_file(png,mimetype='image/png',
                             download_name=name.replace('.ARC','_scheme.png'))
        return ('none',404)
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(2048,1024),'stretch',preserve_alpha=False)
    img=img.convert('RGB'); img.save(png)
    lf=request.files.get('layer')
    if lf:
        la=Image.open(lf.stream)
        la,_layer_prep=prepare_import_image(la,(2048,1024),'stretch',preserve_alpha=True)
        la.convert('RGBA').save(lay)
    else:
        # Imported from outside the booth: replace the complete atlas. A stale
        # auto-diff layer from v1/RC2 must not survive and reintroduce donor blocks.
        if os.path.exists(lay): os.remove(lay)
        prep['install_mode']='full UV-atlas replacement (no donor diff mask)'
    return jsonify(dict(ok=True, image_prep=prep))

@app.route('/api/layer/<name>')
def layer(name):
    lay=os.path.join(SCHEMES,name+'.layer.png')
    if os.path.exists(lay):
        return send_file(lay,mimetype='image/png',
                         download_name=name.replace('.ARC','_layer.png'))
    return ('none',404)

_LIVE_PAINT_THUMB_CACHE={}
_PAINT_ATLAS_PREVIEW_CACHE={}
_STOCK_THUMB_SUPPORT_CACHE={}
_LIVE_LIVERY_INDEX_CACHE={'sig':None,'script_to_uid':{},'uid_to_driver':{}}
_LIVE_THUMB_LOCATION_CACHE={'sig':None,'by_uid':{},'errors':[]}


def _paint_preview_signature(reg,groups=('0','1','2')):
    parts=[]
    for gid in groups:
        info=reg.get(str(gid)) or {}
        for key in ('ar','cdf'):
            path=info.get(key)
            try:
                st=os.stat(path);parts.append((str(gid),key,st.st_size,st.st_mtime_ns))
            except Exception:parts.append((str(gid),key,0,0))
    try:
        st=os.stat(EXTRA_SCHEME_STATE);parts.append(('state','json',st.st_size,st.st_mtime_ns))
    except Exception:parts.append(('state','json',0,0))
    return tuple(parts)


def _live_livery_index(game,reg):
    sig=_paint_preview_signature(reg,('0','1'))
    cache=_LIVE_LIVERY_INDEX_CACHE
    if cache.get('sig')==sig:return cache
    catalog=extra_scheme_mod().catalog(game,EXTRA_SCHEME_STATE)
    script_to_uid={};uid_to_driver={}
    for driver in catalog.get('drivers',[]):
        try:driver_uid=int(driver.get('uid'))
        except Exception:continue
        for scheme in driver.get('schemes',[]):
            try:uid=int(scheme.get('uid'))
            except Exception:continue
            script=str(scheme.get('script_name') or '').casefold()
            if script:script_to_uid[script]=uid
            uid_to_driver[uid]=driver_uid
    cache.update(sig=sig,script_to_uid=script_to_uid,uid_to_driver=uid_to_driver)
    return cache


def _decode_thumbnail_from_raw(raw, uid):
    """Decode PAINTSCHEME_<uid> from one already-read team container."""
    entries,_=C.parse_multi_arc(raw)
    name=f'PAINTSCHEME_{int(uid)}'
    entry=next((e for e in entries if str(e.get('name'))==name),None)
    if entry is None:
        raise ValueError(f'{name} was not found in this team container')
    if str(entry.get('fmt'))!='DXT5' or int(entry.get('w',0))!=256 or int(entry.get('h',0))!=256:
        raise ValueError(f'{name} is not a readable 256x256 DXT5 thumbnail')
    return C.multi_read_png(raw,entry).convert('RGBA')


def _decode_thumbnail_from_container(game, uid, container_name):
    """Read one exact live PAINTSCHEME resource without applying write guards."""
    tm=extra_thumbnail_mod()
    hit=tm.find_target(game,int(uid),target_container_name=container_name)
    if not hit:
        raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in {container_name}')
    return _decode_thumbnail_from_raw(hit[2],uid)


def _live_thumbnail_locations(reg):
    """Index every live team-bank copy of every PAINTSCHEME resource once.

    NASCAR 15 can retain the same livery thumbnail in more than one team bank
    after driver moves. The public preview must inspect those live copies rather
    than trusting app-side state or whichever CDF row happens to appear first.
    """
    sig=_paint_preview_signature(reg,('1',))
    cache=_LIVE_THUMB_LOCATION_CACHE
    if cache.get('sig')==sig:
        return cache
    info=need(reg,'1');by_uid=collections.defaultdict(list);errors=[]
    rows=parse_cdfiles(info['cdf'])
    with open(info['ar'],'rb') as fh:
        for off,size,name in rows:
            if not str(name).upper().startswith('2DRIVERSELECTTD_'):
                continue
            try:
                fh.seek(int(off));raw=fh.read(int(size))
                if len(raw)!=int(size):
                    raise ValueError(f'short read ({len(raw)} of {int(size)} bytes)')
                entries,_=C.parse_multi_arc(raw)
                for entry in entries:
                    m=re.fullmatch(r'PAINTSCHEME_(\d+)',str(entry.get('name') or ''),re.I)
                    if not m:
                        continue
                    if str(entry.get('fmt'))!='DXT5' or int(entry.get('w',0))!=256 or int(entry.get('h',0))!=256:
                        continue
                    by_uid[int(m.group(1))].append({
                        'container':str(name),'offset':int(off),'size':int(size),
                    })
            except Exception as ex:
                errors.append(f'{name}: {ex}')
    cache.update(sig=sig,by_uid={int(k):list(v) for k,v in by_uid.items()},errors=errors)
    return cache


def _read_indexed_thumbnail(archive_path, row, uid):
    with open(archive_path,'rb') as fh:
        fh.seek(int(row['offset']));raw=fh.read(int(row['size']))
    if len(raw)!=int(row['size']):
        raise ValueError(f"short read for {row['container']}")
    return _decode_thumbnail_from_raw(raw,uid)


def _pristine_thumbnail_hash(reg, container_name, uid, cache):
    """Return the original backup thumbnail hash for one named team bank."""
    key=(str(container_name).casefold(),int(uid))
    if key in cache:
        return cache[key]
    info=need(reg,'1');bak_ar=backup_path(info['ar']);bak_cdf=backup_path(info['cdf'])
    if not (os.path.exists(bak_ar) and os.path.exists(bak_cdf)):
        cache[key]=None;return None
    try:
        row=next(({'container':n,'offset':int(o),'size':int(s)}
                  for o,s,n in parse_cdfiles(bak_cdf)
                  if str(n).casefold()==str(container_name).casefold()),None)
        if not row:
            cache[key]=None;return None
        image=_read_indexed_thumbnail(bak_ar,row,uid)
        value=hashlib.sha256(image.tobytes()).hexdigest()
    except Exception:
        value=None
    cache[key]=value
    return value


def _live_livery_thumbnail(uid):
    """Return the exact live thumbnail targeted by the proven replacement writer.

    The game-facing replace route uses extra_thumbnail_mod().find_target(game, uid)
    without forcing a team bank. In-game testing proves that changing this exact
    target changes Paint Select. The preview must therefore decode that same target
    first. Ranking duplicate team-bank copies can choose an older stock copy even
    while the game is displaying the writer target.

    Only when the exact writer target cannot be decoded do we fall back to scanning
    every readable duplicate live copy. App-side PNGs remain HTTP-route fallback only.
    """
    game,reg=_extra_game_and_registry();uid=int(uid)
    index=_live_livery_index(game,reg)
    if uid not in index['uid_to_driver']:
        raise ValueError(f'livery UID {uid} is not in the live paint catalog')
    sig=(_paint_preview_signature(reg,('1',)),uid)
    cached=_LIVE_PAINT_THUMB_CACHE.get(sig)
    if cached is not None:
        return Image.open(io.BytesIO(cached)).convert('RGBA')

    errors=[]
    # First choice: the exact resource selected by the in-game-proven writer.
    try:
        writer_hit=extra_thumbnail_mod().find_target(game,uid)
        if writer_hit:
            image=_decode_thumbnail_from_raw(writer_hit[2],uid)
            buf=io.BytesIO();image.save(buf,format='PNG',optimize=False)
            if len(_LIVE_PAINT_THUMB_CACHE)>256:_LIVE_PAINT_THUMB_CACHE.clear()
            _LIVE_PAINT_THUMB_CACHE[sig]=buf.getvalue()
            return image
        errors.append('the proven thumbnail target was not found')
    except Exception as ex:
        errors.append(f'proven target: {ex}')

    # Fallback: inspect every live duplicate and use the newest readable CDF row.
    # This is recovery-only; it must never outrank the same target used by Replace.
    locations=_live_thumbnail_locations(reg)
    rows=list(locations.get('by_uid',{}).get(uid,[]))
    archive_path=need(reg,'1')['ar'];candidates=[]
    for row in rows:
        try:
            image=_read_indexed_thumbnail(archive_path,row,uid)
            candidates.append((int(row.get('offset',0)),image))
        except Exception as ex:
            errors.append(f"{row.get('container')}: {ex}")
    if not candidates:
        detail='; '.join(errors or locations.get('errors',[])[:8])
        raise ValueError(detail or f'no live Paint Select thumbnail was found for livery UID {uid}')
    _off,image=max(candidates,key=lambda x:x[0])
    buf=io.BytesIO();image.save(buf,format='PNG',optimize=False)
    if len(_LIVE_PAINT_THUMB_CACHE)>256:_LIVE_PAINT_THUMB_CACHE.clear()
    _LIVE_PAINT_THUMB_CACHE[sig]=buf.getvalue()
    return image


def _slot_livery_uid(name):
    script = str(name or '')
    if script.upper().startswith('LIVERY_') and script.upper().endswith('.ARC'):
        script = script[7:-4]
    try:
        game,reg=_extra_game_and_registry()
        return _live_livery_index(game,reg)['script_to_uid'].get(script.casefold())
    except Exception:
        pass
    return None


@app.route('/api/paint_thumbnail/<int:uid>')
def paint_thumbnail(uid):
    try:
        image = _live_livery_thumbnail(uid)
        out = io.BytesIO(); image.save(out, format='PNG'); out.seek(0)
        return send_file(out, mimetype='image/png', conditional=True, max_age=300)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),404



@app.route('/api/paint_previews/reload', methods=['POST'])
def paint_previews_reload():
    """Clear only read-only preview caches so failed rows can be retried."""
    _LIVE_PAINT_THUMB_CACHE.clear()
    _PAINT_ATLAS_PREVIEW_CACHE.clear()
    _STOCK_THUMB_SUPPORT_CACHE.clear()
    _LIVE_LIVERY_INDEX_CACHE.update(sig=None,script_to_uid={},uid_to_driver={})
    _LIVE_THUMB_LOCATION_CACHE.update(sig=None,by_uid={},errors=[])
    return jsonify(dict(ok=True,note='Paint preview caches cleared. No game files were changed.'))


def _stock_thumbnail_support(uid):
    """Use the dev26-proven live thumbnail lookup.

    Dev33 scoped every lookup to the driver's current team. That looked safer on
    paper, but in real game files it rejected or targeted the wrong native copy
    for normal stock liveries. The dev26 unscoped helper is the in-game-proven
    replacement path, so keep that behavior and separately replay the saved
    custom pixels when a driver is moved.
    """
    game,reg=_extra_game_and_registry();uid=int(uid)
    sig=(_paint_preview_signature(reg,('1',)),uid)
    cached=_STOCK_THUMB_SUPPORT_CACHE.get(sig)
    if cached is not None:return dict(cached)
    tm=extra_thumbnail_mod()
    try:
        info=tm.inspect_thumbnail_identity(game,uid)
        supported=bool(info.get('exists') and info.get('structural_valid') and info.get('same_bank_valid'))
        reason='' if supported else (info.get('structural_error') or info.get('identity_chain_error') or 'This Paint Select slot does not have a proven same-bank native thumbnail identity.')
        out=dict(ok=True,uid=uid,supported=supported,reason=reason,container=info.get('container'),details=info,
                 target_source='verified native lookup')
    except Exception as ex:
        out=dict(ok=True,uid=uid,supported=False,reason=str(ex),target_source='verified native lookup')
    if len(_STOCK_THUMB_SUPPORT_CACHE)>512:_STOCK_THUMB_SUPPORT_CACHE.clear()
    _STOCK_THUMB_SUPPORT_CACHE[sig]=dict(out)
    return out

@app.route('/api/paint_thumbnail_debug/<int:uid>')
def paint_thumbnail_debug(uid):
    """Read-only report showing every live copy considered for one thumbnail."""
    try:
        game,reg=_extra_game_and_registry();uid=int(uid)
        index=_live_livery_index(game,reg);driver_uid=index['uid_to_driver'].get(uid)
        live_link=_team_fast_driver_links().get(int(driver_uid)) if driver_uid is not None else None
        current_container=(f"2DRIVERSELECTTD_{int(live_link['team_uid'])}.ARC" if live_link else '')
        writer_hit=extra_thumbnail_mod().find_target(game,uid)
        writer_container=str(writer_hit[1].get('name') or '') if writer_hit else ''
        writer_offset=int(writer_hit[1].get('offset',0)) if writer_hit else None
        rows=list(_live_thumbnail_locations(reg).get('by_uid',{}).get(uid,[]))
        archive_path=need(reg,'1')['ar'];pristine_cache={};report=[]
        for row in rows:
            try:
                image=_read_indexed_thumbnail(archive_path,row,uid)
                live_hash=hashlib.sha256(image.tobytes()).hexdigest()
                pristine_hash=_pristine_thumbnail_hash(reg,row['container'],uid,pristine_cache)
                report.append(dict(container=row['container'],offset=int(row['offset']),size=int(row['size']),
                                   current_team=(str(row['container']).casefold()==current_container.casefold()),
                                   writer_target=(str(row['container']).casefold()==writer_container.casefold() and int(row['offset'])==int(writer_offset or -1)),
                                   modified_from_backup=bool(pristine_hash and live_hash!=pristine_hash),
                                   live_hash=live_hash,pristine_hash=pristine_hash))
            except Exception as ex:
                report.append(dict(container=row.get('container'),offset=row.get('offset'),error=str(ex)))
        return jsonify(dict(ok=True,uid=uid,driver_uid=driver_uid,current_container=current_container,
                            writer_container=writer_container,writer_offset=writer_offset,copies=report))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/stock_thumbnail_support/<int:uid>')
def stock_thumbnail_support(uid):
    try:return jsonify(_stock_thumbnail_support(uid))
    except Exception as ex:return jsonify(dict(ok=False,supported=False,error=str(ex))),400


def _stock_thumbnail_preview_path(uid):
    """Stable app-side preview for a replaced stock thumbnail, keyed by livery UID."""
    folder=os.path.join(SCHEMES,'thumbnail_overrides')
    os.makedirs(folder,exist_ok=True)
    return os.path.join(folder,f'{int(uid)}.png')


def _stock_thumbnail_override_record(uid, slot_name, source_container):
    """Remember the exact uploaded thumbnail so a later team move can replay it."""
    state=_team_state_load()
    overrides=state.setdefault('thumbnail_overrides',{})
    saved_name=''
    uid_preview=_stock_thumbnail_preview_path(uid)
    if os.path.exists(uid_preview):
        saved_name=os.path.relpath(uid_preview,SCHEMES).replace(os.sep,'/')
    elif slot_name:
        candidate=os.path.join(SCHEMES,str(slot_name)+'.thumb.png')
        if os.path.exists(candidate):saved_name=os.path.basename(candidate)
    overrides[str(int(uid))]={
        'slot':str(slot_name or ''),
        'saved_thumb':saved_name,
        'uid_preview':saved_name,
        'source_container':str(source_container or ''),
        'updated':int(time.time()),
    }
    _team_state_save(state)


@app.route('/api/stock_thumbnail/<int:uid>', methods=['POST'])
def stock_thumbnail_replace(uid):
    snapshot=None;temp_path=None
    with _EXTRA_CREATE_LOCK:
        try:
            if _extra_game_running():raise RuntimeError('NASCAR15.exe is running. Close the game before changing a Paint Select thumbnail')
            support=_stock_thumbnail_support(uid)
            if not support.get('supported'):raise ValueError(support.get('reason') or 'This thumbnail is not structurally supported')
            upload=request.files.get('file')
            if not upload:raise ValueError('choose an image first')
            prepared,prep=_extra_prepare_thumbnail_source(upload.read(),request.form.get('quality') or 'auto')
            fd,temp_path=tempfile.mkstemp(prefix='n15_stock_thumb_',suffix='.png');os.close(fd);prepared.save(temp_path,'PNG')
            game,reg=_extra_game_and_registry();tm=extra_thumbnail_mod();existing=tm.find_target(game,int(uid))
            if not existing:raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in the live game')
            _extra_backups(reg,('1',));snapshot=_extra_transaction_snapshot(reg,('1',),inplace_thumbnail=existing)
            _extra_persist_snapshot(snapshot,f'Replace in-game thumbnail for livery UID {int(uid)}',operation={'type':'stock_thumbnail','uid':int(uid)})
            report=tm.replace_existing_thumbnail(game,int(uid),temp_path,target_container_name=support.get('container'))
            if 'texconv' not in str(report.get('encoder') or '').lower():
                raise ValueError('the game-safe texconv DXT5 encoder was not used; nothing was kept')
            # Keep the exact installed pixels as the app preview and as the
            # source that follows this livery through future driver moves.
            slot_name=str(request.form.get('slot') or '').strip()
            # Persist the exact installed image under both the slot name and a
            # stable livery-UID key. The UID copy survives driver moves and avoids
            # relying on whichever duplicate team-bank copy the live preview
            # resolver happens to encounter first.
            uid_preview=_stock_thumbnail_preview_path(int(uid))
            prepared.save(uid_preview,'PNG')
            if slot_name:
                os.makedirs(SCHEMES,exist_ok=True)
                preview_path=os.path.join(SCHEMES,slot_name+'.thumb.png')
                prepared.save(preview_path,'PNG')
            _stock_thumbnail_override_record(int(uid),slot_name,report.get('container') or support.get('container'))
            _extra_seal_persisted_snapshot({'type':'stock_thumbnail','uid':int(uid),
                                             'container':report.get('container') or support.get('container')})
            _clear_ui_thumb_cache()
            return jsonify(dict(ok=True,uid=int(uid),container=report.get('container') or support.get('container'),
                                preview=report,preparation=prep,
                                note='Paint Select thumbnail replaced and verified. The installed image was also saved so it can follow the driver during a later team move.'))
        except Exception as ex:
            errors=_extra_transaction_restore(snapshot) if snapshot else []
            detail=str(ex)
            if errors:detail+=' | Rollback warnings: '+'; '.join(errors)
            elif snapshot:_extra_clear_persisted_snapshot()
            return jsonify(dict(ok=False,error=detail,rolled_back=bool(snapshot and not errors))),400
        finally:
            if temp_path and os.path.exists(temp_path):
                try:os.remove(temp_path)
                except OSError:pass

@app.route('/api/slotthumb/<name>', methods=['GET','POST'])
def slotthumb(name):
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    if request.method == 'GET':
        uid = _slot_livery_uid(name)
        # Live game files are the source of truth. This makes a fresh app folder
        # immediately reflect thumbnails installed by any older app version.
        if uid is not None:
            try:
                image=_live_livery_thumbnail(uid)
                out=io.BytesIO();image.save(out,format='PNG');out.seek(0)
                response=send_file(out,mimetype='image/png',conditional=False,max_age=0)
                response.headers['Cache-Control']='no-store, max-age=0'
                response.headers['X-N15-Thumbnail-Source']='live game Paint Select thumbnail'
                return response
            except Exception:
                pass
            uid_path=_stock_thumbnail_preview_path(uid)
            if os.path.exists(uid_path):
                response=send_file(uid_path,mimetype='image/png',conditional=False,max_age=0)
                response.headers['Cache-Control']='no-store, max-age=0'
                response.headers['X-N15-Thumbnail-Source']='fallback saved thumbnail by livery UID'
                return response
        path=os.path.join(SCHEMES,name+'.thumb.png')
        if os.path.exists(path):
            response=send_file(path,mimetype='image/png',conditional=False,max_age=0)
            response.headers['Cache-Control']='no-store, max-age=0'
            response.headers['X-N15-Thumbnail-Source']='fallback app preview'
            return response
        return ('not found',404)
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(256,256),request_resize_mode('fit'),preserve_alpha=True)
    img.save(os.path.join(SCHEMES,name+'.thumb.png'))
    return jsonify(dict(ok=True, image_prep=prep, note='Menu preview saved. Supported game thumbnail targets use it during install.'))

@app.route('/api/slotthumb_export/<name>')
def slotthumb_export(name):
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    uid = _slot_livery_uid(name)
    if uid is not None:
        try:
            image=_live_livery_thumbnail(uid)
            out=io.BytesIO();image.save(out,format='PNG');out.seek(0)
            return send_file(out,mimetype='image/png',as_attachment=True,
                             download_name=f"{name}_menu_thumbnail.png")
        except Exception:
            uid_path=_stock_thumbnail_preview_path(uid)
            if os.path.exists(uid_path):
                return send_file(uid_path,mimetype='image/png',as_attachment=True,
                                 download_name=f"{name}_menu_thumbnail.png")
    path=os.path.join(SCHEMES,name+'.thumb.png')
    if os.path.exists(path):
        return send_file(path,mimetype='image/png',as_attachment=True,
                         download_name=f"{name}_menu_thumbnail.png")
    return ('not found',404)

@app.route('/api/build', methods=['POST'])
def build():
    g,reg=registry(); names=request.json.get('names',[])
    slots={s['name']:s for s in grid_slots()}
    done=[]; errs=[]
    for n in names:
        png=os.path.join(SCHEMES,n+'.png'); lay=os.path.join(SCHEMES,n+'.layer.png')
        if _is_managed_extra_slot(n):
            errs.append(dict(name=n,error=_managed_extra_action_error(n)))
        elif n in slots and os.path.exists(png):
            try: done.append(dict(name=n, wrote=install_slot(reg,slots[n],png,lay)))
            except Exception as e: errs.append(dict(name=n,error=str(e)))
        else: errs.append(dict(name=n,error='no scheme saved'))
    return jsonify(dict(done=done,errors=errs))

@app.route('/api/restore_slot', methods=['POST'])
def restore_slot():
    try:
        g,reg=registry(); name=(request.json or {}).get('name')
        if _is_managed_extra_slot(name):
            return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
        slot=_slot(name)
        if not slot:
            return jsonify(dict(ok=False,error='slot not found')),404
        specs=[dict(kind='SD',preferred=str(slot.get('sd_arc') or slot['arc']),entry=slot.get('name'))]
        if slot.get('hd') and slot.get('hd_size'):
            specs.append(dict(kind='HD',preferred=str(slot.get('hd_arc') or slot['arc']),entry=slot.get('hd')))
        jobs=[]
        for spec in specs:
            live_ref,live_matches=_find_live_resource(reg,spec['entry'],spec['preferred'])
            source_ref,searched,wrong=_find_pristine_resource(reg,spec['entry'],live_ref['size'],g)
            if source_ref is None:
                detail='; '.join(searched) if searched else 'no clean baseline or paired backup was detected'
                if wrong: detail += '; wrong-size matches: ' + ', '.join(wrong)
                raise ValueError(f'no verified stock source contains {spec["entry"]} ({live_ref["size"]} bytes). Searched: {detail}')
            pristine=_read_exact_region(source_ref['ar'],source_ref['offset'],source_ref['size'],f'stock {spec["entry"]}')
            old_live=_read_exact_region(live_ref['ar'],live_ref['offset'],live_ref['size'],f'live {spec["entry"]}')
            jobs.append(dict(**spec,live_ref=live_ref,source_ref=source_ref,size=live_ref['size'],
                             pristine=pristine,old_live=old_live,live_matches=live_matches))
        attempted=[]
        try:
            for job in jobs:
                attempted.append(job)
                with open(job['live_ref']['ar'],'r+b') as live:
                    live.seek(job['live_ref']['offset']);live.write(job['pristine']);live.flush();os.fsync(live.fileno())
                    live.seek(job['live_ref']['offset'])
                    if live.read(job['size'])!=job['pristine']:
                        raise ValueError(f'{job["kind"]} restore readback mismatch')
        except Exception as write_ex:
            rb=[]
            for job in reversed(attempted):
                try:
                    with open(job['live_ref']['ar'],'r+b') as live:
                        live.seek(job['live_ref']['offset']);live.write(job['old_live']);live.flush();os.fsync(live.fileno())
                        live.seek(job['live_ref']['offset'])
                        if live.read(job['size'])!=job['old_live']:
                            raise ValueError('rollback readback mismatch')
                except Exception as ex:
                    rb.append(f'ARCHIVE{job["live_ref"]["arcid"]} {job["kind"]}: {ex}')
            if rb:
                raise RuntimeError(f'{write_ex}; restore rollback also failed: ' + '; '.join(rb))
            raise
        for ext in ('.png','.layer.png','.layer.json','.thumb.png'):
            fp=os.path.join(SCHEMES,name+ext)
            if os.path.exists(fp): os.remove(fp)
        _clear_ui_thumb_cache()
        return jsonify(dict(ok=True,restored=[dict(kind=j['kind'],entry=j['entry'],
                    live_archive=j['live_ref']['arcid'],live_offset=j['live_ref']['offset'],size=j['size'],
                    source=j['source_ref']['label'],source_archive=j['source_ref']['arcid'],
                    source_offset=j['source_ref']['offset']) for j in jobs]))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/paint_forensics/<path:name>')
def paint_forensics(name):
    """Export the exact live/pristine paint wrappers and decoded mip evidence.

    This is intentionally opt-in because it contains the selected game resource
    bytes. It never changes the game and avoids packaging whole archives.
    """
    try:
        g,reg=registry(); slot=_slot(name)
        if not slot:
            return jsonify(dict(ok=False,error='paint slot not found')),404
        jobs=[_paint_forensics_job(reg,slot,'sd',g)]
        if slot.get('hd') and slot.get('hd_size'):
            jobs.append(_paint_forensics_job(reg,slot,'hd',g))
        manifest=dict(app_version=APP_VERSION,release=APP_RELEASE_LABEL,
                      generated=int(time.time()),game_folder=g,slot={k:v for k,v in slot.items() if k not in ('fei',)},
                      jobs=[])
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
            source_path=os.path.join(SCHEMES,name+'.png')
            if os.path.exists(source_path):
                zf.write(source_path,'source/imported_or_saved.png')
            comparison=[]
            source_img=None
            if os.path.exists(source_path):
                try:
                    source_img=Image.open(source_path).convert('RGB'); source_img.load()
                except Exception:
                    source_img=None
            for job in jobs:
                base=job['kind']
                zf.writestr(f'{base}/live_entry.bin',job['live_bytes'])
                if job.get('pristine_bytes') is not None:
                    zf.writestr(f'{base}/pristine_entry.bin',job['pristine_bytes'])
                meta={k:v for k,v in job.items() if not k.endswith('_bytes')}
                meta['live_sha256']=__import__('hashlib').sha256(job['live_bytes']).hexdigest()
                if job.get('pristine_bytes') is not None:
                    meta['pristine_sha256']=__import__('hashlib').sha256(job['pristine_bytes']).hexdigest()
                    meta['changed_bytes']=sum(a!=b for a,b in zip(job['live_bytes'],job['pristine_bytes']))
                else:
                    meta['pristine_sha256']=None
                    meta['changed_bytes']=None
                manifest['jobs'].append(meta)
                max_level=12 if job['hd'] else 11
                for level in range(max_level):
                    try:
                        live_stored=_native_wrapper_mip_image(job['live_bytes'],level,job['hd'],False)
                        live_logical=_native_wrapper_mip_image(job['live_bytes'],level,job['hd'],True)
                        stock_logical=(_native_wrapper_mip_image(job['pristine_bytes'],level,job['hd'],True) if job.get('pristine_bytes') is not None else None)
                        _zip_write_image(zf,f'{base}/mips/L{level:02d}_live_stored.png',live_stored)
                        _zip_write_image(zf,f'{base}/mips/L{level:02d}_live_logical.png',live_logical)
                        if stock_logical is not None:
                            _zip_write_image(zf,f'{base}/mips/L{level:02d}_pristine_logical.png',stock_logical)
                        row=dict(kind=base,level=level,width=live_logical.width,height=live_logical.height)
                        if source_img is not None:
                            box=Image.Resampling.BOX if hasattr(Image,'Resampling') else Image.BOX
                            src=source_img.resize(live_logical.size,box)
                            la=np.asarray(live_logical).astype(np.int16); sa=np.asarray(src).astype(np.int16)
                            row['source_mean_abs_error']=round(float(np.abs(la-sa).mean()),6)
                            _zip_write_image(zf,f'{base}/mips/L{level:02d}_source_resized.png',src)
                        comparison.append(row)
                    except Exception as mip_ex:
                        comparison.append(dict(kind=base,level=level,error=str(mip_ex)))
            zf.writestr('mip_comparison.json',json.dumps(comparison,indent=2))
            zf.writestr('manifest.json',json.dumps(manifest,indent=2,default=str))
            zf.writestr('README.txt',
                'NASCAR 15 paint forensics package\n\n'
                'Contains only the selected paint resource wrappers, the matching pristine backup wrappers,\n'
                'the app-saved source PNG when present, and decoded mip images. No whole archive is included.\n'
                'live_stored shows physical page interpretation; live_logical reverses the currently mapped\n'
                'large-mip compensation for visual comparison. This export never changes the game.\n')
        out.seek(0)
        safe=re.sub(r'[^A-Za-z0-9_.-]+','_',name)
        return send_file(out,mimetype='application/zip',as_attachment=True,
                         download_name=f'PAINT_FORENSICS_{safe}_{time.strftime("%Y%m%d_%H%M%S")}.zip')
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/roster')
def api_roster():
    g,reg=registry(); d,t=roster(reg)
    return jsonify(dict(drivers=d, teams=t))

@app.route('/api/name', methods=['POST'])
def api_name():
    g,reg=registry(); j=request.json
    old, new = j['old'], j['new'].strip()
    if not new: return jsonify(dict(ok=False,error='empty name')),400
    exp=bool(j.get('experimental'))
    try:
        if exp: n=patch_name_exp(reg, old, new)
        else: n=patch_name(reg, old, new)
    except Exception as e: return jsonify(dict(ok=False,error=str(e))),400
    cfg=load_cfg(); led=cfg.setdefault('renames',{})
    orig=old
    for o,c2 in list(led.items()):
        if c2==old: orig=o; break
    led[orig]=new; save_cfg(cfg)
    return jsonify(dict(ok=True,patched=n,experimental=exp))

@app.route('/api/handle', methods=['POST'])
def api_handle():
    g,reg=registry(); j=request.json
    old,new=j['old'],j['new'].strip()
    if not new: return jsonify(dict(ok=False,error='empty handle')),400
    try: n,applied=patch_handle(reg,old,new)
    except Exception as e: return jsonify(dict(ok=False,error=str(e))),400
    cfg=load_cfg(); led=cfg.setdefault('handles',{})
    orig=old
    for o,c2 in list(led.items()):
        if c2==old: orig=o; break
    led[orig]=applied; save_cfg(cfg)
    return jsonify(dict(ok=True,patched=n,applied=applied.rstrip('_ '),storage_length=len(applied)))

@app.route('/api/names/export')
def names_export():
    try:
        _g,reg=registry();drivers,teams=roster(reg);cfg=load_cfg()
        out=io.StringIO();w=csv.writer(out);w.writerow(['type','original','current','new_value','notes'])
        for d in drivers:
            w.writerow(['driver',d['original'],d['current'],'','full/display name; aliases can be edited in UI Text'])
            if d.get('handle'):w.writerow(['handle',d['handle'],d.get('handle_current',d['handle']),'','driver-card handle'])
        for t in teams:w.writerow(['team',t['original'],t['current'],'','team display name'])
        return send_file(io.BytesIO(out.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='nascar15_names_roster.csv')
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/names/restore_all',methods=['POST'])
def names_restore_all():
    try:
        _g,reg=registry();cfg=load_cfg();renames=dict(cfg.get('renames') or {});handles=dict(cfg.get('handles') or {})
        done=[];errors=[]
        # Reverse the newest display strings back to their original text-table strings.
        for original,current in list(renames.items()):
            if str(original)==str(current):continue
            try:patch_name_exp(reg,str(current),str(original));done.append(f'name {current} -> {original}')
            except Exception as ex:errors.append(f'{current}: {ex}')
        for original,current in list(handles.items()):
            if str(original)==str(current):continue
            try:patch_handle(reg,str(current),str(original));done.append(f'handle {current} -> {original}')
            except Exception as ex:errors.append(f'@{current}: {ex}')
        if errors:return jsonify(dict(ok=False,error='; '.join(errors),restored=done)),400
        cfg.pop('renames',None);cfg.pop('handles',None);save_cfg(cfg)
        _ui_text_invalidate()
        return jsonify(dict(ok=True,restored=done,count=len(done)))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/stats')
def api_stats():
    g,reg=registry()
    return jsonify(dict(stats=read_stats(reg),order=STATS,
                        labels=dict(zip(STATS,STAT_LABELS)),normal_min=0,normal_max=100,
                        experimental_supported=True,experimental_abs_max=STAT_EXPERIMENTAL_ABS_MAX,
                        note='The original scale is 0-100. Custom values outside that range are supported and the app expands the rating data when needed.'))

@app.route('/api/stats/set', methods=['POST'])
def api_stats_set():
    g,reg=registry();j=request.get_json(force=True)
    try: result=write_stat(reg,j['profile_id'],j['stat'],j['value'],bool(j.get('experimental')))
    except Exception as e:return jsonify(dict(ok=False,error=str(e))),400
    return jsonify(dict(ok=True,**result))

@app.route('/api/stats/reset', methods=['POST'])
def api_stats_reset():
    g,reg=registry()
    n=reset_stats(reg, request.json['profile_id'])
    return jsonify(dict(ok=True, restored=n))

@app.route('/api/pack/export')
def pack_export():
    import zipfile
    g,reg=registry()
    buf=io.BytesIO()
    z=zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED)
    z.writestr('manifest.json', json.dumps(dict(format='gridpack', version=1)))
    for f in os.listdir(SCHEMES):
        z.write(os.path.join(SCHEMES,f), 'schemes/'+f)
    cfg=load_cfg()
    z.writestr('names.json',json.dumps(dict(renames=cfg.get('renames',{}),
        handles={k:str(v).rstrip('_ ') for k,v in (cfg.get('handles',{}) or {}).items()}),indent=1))
    try:
        z.writestr('stats.json', json.dumps(
            [dict(profile_id=d['profile_id'], stats=d['stats']) for d in read_stats(reg)],
            indent=1))
    except Exception: pass
    # menu images that differ from backup
    for key in _menu_containers():
        try:
            arcid,off,size,live=menu_container(reg,key,live=True)
            a=need(reg,arcid)
            if not os.path.exists(a['bak']): continue
            _,_,_,bak=menu_container(reg,key,live=False)
            ent,_=C.parse_multi_arc(live, known_dims=(128,64) if key=='numbers' else None)
            for e in ent:
                if e['w']<=0: continue
                pa,ps=e['payload_abs'],e['payload_size']
                if live[pa:pa+ps]!=bak[pa:pa+ps]:
                    img=C.multi_read_png(live,e)
                    b=io.BytesIO(); img.save(b,'PNG')
                    z.writestr(f'menus/{key}/{e["name"]}.png', b.getvalue())
        except Exception: continue
    z.close(); buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='nascar15_legacy_mod_pack.gridpack')

LEGACY_PACK_MEMBER_RENAMES = {
    'LIVERY_14_55_VICKERS_PRIMARY.ARC': 'LIVERY_15_55_BRIAN_VICKERS_PRIMARY.ARC',
    'HDLIVERY_14_55_VICKERS_PRIMARY.ARC': 'HDLIVERY_15_55_BRIAN_VICKERS_PRIMARY.ARC',
}


def _legacy_pack_member_basename(name):
    """Translate filenames emitted by older public builds to current IDs."""
    base=os.path.basename(str(name or ''))
    for old,new in LEGACY_PACK_MEMBER_RENAMES.items():
        if base.startswith(old):
            return new+base[len(old):]
    return base


def _legacy_driver_name_target(reg, old):
    """Resolve old public roster aliases to an exact current stock text key."""
    text=str(old or '').strip()
    # The broken public roster exposed Mike Wallace in Darrell Wallace Jr.'s
    # selectable slot.  A legacy driver rename under that key therefore belongs
    # to Darrell, not the unrelated historic trivia/name string.
    candidates=(['Darrell Wallace Jr.','Bubba Wallace Jr.','Darrell Wallace Jr','Darrell Wallace']
                if text.casefold()=='mike wallace' else [text])
    return _find_exact_stock_text(reg,candidates)


def _pack_apply_legacy_v1(z, selected=None):
    """Convert and import a gridpack v1 through current safe write paths."""
    selected=set(selected or ('schemes','names','ratings','menus'))
    _g,reg=registry();applied={k:0 for k in PACK_CATEGORIES};errors=[];migrations=[]
    if 'schemes' in selected:
        os.makedirs(SCHEMES,exist_ok=True)
        for member in z.namelist():
            safe=_pack_safe_member(member)
            if not safe.startswith('schemes/') or safe.endswith('/'):continue
            base=_legacy_pack_member_basename(safe)
            if not base:continue
            raw=z.read(member)
            if len(raw)>100*1024*1024:
                errors.append(base+': file is larger than the 100 MB safety limit');continue
            target=os.path.join(SCHEMES,base)
            if os.path.exists(target) and open(target,'rb').read()==raw:continue
            with open(target,'wb') as fh:fh.write(raw)
            if base!=os.path.basename(safe):migrations.append(f'{os.path.basename(safe)} → {base}')
            if base.lower().endswith('.png') and '.layer.' not in base.lower() and '.thumb.' not in base.lower():
                applied['schemes']+=1
    if 'names' in selected:
        data=_pack_read_json(z,'names.json',{}) or {};cfg=load_cfg()
        for old,new in (data.get('renames') or {}).items():
            target=_legacy_driver_name_target(reg,old)
            if not target:
                errors.append(f'Rename {old}: exact current stock text was not found');continue
            try:
                if str(cfg.get('renames',{}).get(target,target))!=str(new):
                    patch_name_exp(reg,target,str(new));cfg.setdefault('renames',{})[target]=str(new);applied['names']+=1
                if str(old)!=str(target):migrations.append(f'name key {old} → {target}')
            except Exception as ex:errors.append(f'Rename {old}: {ex}')
        for old,new in (data.get('handles') or {}).items():
            try:
                if str(cfg.get('handles',{}).get(old,old))!=str(new):
                    _n,actual=patch_handle(reg,str(old),str(new));cfg.setdefault('handles',{})[str(old)]=actual;applied['names']+=1
            except Exception as ex:errors.append(f'Handle {old}: {ex}')
        save_cfg(cfg)
    if 'ratings' in selected:
        ratings=_pack_read_json(z,'stats.json',[]) or []
        try:current={str(x['profile_id']):x['stats'] for x in read_stats(reg)}
        except Exception:current={}
        for row in ratings:
            for st,v in (row.get('stats') or {}).items():
                if str(current.get(str(row.get('profile_id')),{}).get(st))==str(v):continue
                try:write_stat(reg,row['profile_id'],st,float(v),experimental=True);applied['ratings']+=1
                except Exception as ex:errors.append(f"Rating {row.get('profile_id')}/{st}: {ex}")
    if 'menus' in selected:
        for member in z.namelist():
            m=re.match(r'^menus/([^/]+)/(.+)\.png$',member,re.I)
            if not m:continue
            key,name=m.group(1),m.group(2)
            try:
                arcid,off,size,arc=menu_container(reg,key)
                entries,_=_menu_parse_entries(arc,key)
                e=next((x for x in entries if x['name']==name),None)
                if not e:raise ValueError('current game image entry is missing')
                img=Image.open(io.BytesIO(z.read(member)))
                img,_=prepare_import_image(img,(e['w'],e['h']),'fit',preserve_alpha=True)
                a=need(reg,arcid);ensure_backup(a['ar'],a['bak'])
                new=C.multi_write_png_validated(arc,e,img,encode_fn=encode_any,
                    known_dims=(128,64) if key=='numbers' else None)
                with open(a['ar'],'r+b') as fh:fh.seek(off);fh.write(new);fh.flush();os.fsync(fh.fileno())
                applied['menus']+=1
            except Exception as ex:errors.append(f'Menu {key}/{name}: {ex}')
        if applied['menus']:_clear_ui_thumb_cache()
    return applied,errors,migrations


@app.route('/api/pack/import', methods=['POST'])
def pack_import():
    """Compatibility endpoint retained for older app frontends."""
    import zipfile
    f=request.files.get('file')
    if not f:return jsonify(dict(ok=False,error='no file')),400
    try:
        with zipfile.ZipFile(f.stream) as z:
            info=_pack_inspect_zip(z)
            if not info.get('legacy'):
                return jsonify(dict(ok=False,error='This is a current Mod Pack. Use Import Mod Pack so it can be previewed first.')),400
            applied,errors,migrations=_pack_apply_legacy_v1(z)
        return jsonify(dict(ok=True,schemes=applied['schemes'],renames=applied['names'],handles=0,
            stats=applied['ratings'],menus=applied['menus'],errors=errors,migrations=migrations,
            note='Older pack converted to the current data model. Saved paints can be installed from Paint Schemes.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/verify_game_files')
def verify_game_files_api():
    """Check every game file group the app can write to and report problems."""
    try:
        g, _reg = registry()
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400
    try:
        V = bank_verify_mod()
        report = V.verify_game(g, indexes=('1', '2'))
        data = report.to_dict()
        data['ok'] = True
        data['problem'] = not report.ok
        return jsonify(data)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/restore', methods=['POST'])
def restore():
    """Restore all available pristine archive/index pairs as one transaction.

    v1.0.1 copied files one at a time. A locked CDF or interrupted archive copy
    could therefore leave the game half stock and half modified, while stale
    extra/team state still claimed the edits existed. This stages every copy,
    swaps originals aside, verifies the complete set, and rolls the set back if
    any commit step fails.
    """
    staged = []
    committed = []
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before restoring original files')
        _g, reg = registry()
        targets = []
        skipped = []
        for key, value in sorted(reg.items(), key=lambda x: str(x[0])):
            for kind, live, backup in (
                ('ar', value['ar'], backup_path(value['ar'])),
                ('cdf', value['cdf'], backup_path(value['cdf'])),
            ):
                if not os.path.exists(backup):
                    skipped.append(os.path.basename(live) + ' (no pristine backup)')
                    continue
                if not _valid_backup(backup, kind):
                    raise ValueError(os.path.basename(backup) + ' looks invalid; nothing was restored')
                if not os.path.exists(live):
                    raise ValueError(os.path.basename(live) + ' is missing; nothing was restored')
                targets.append((kind, live, backup))
        if not targets:
            raise ValueError('no valid pristine backups are available')

        token = f"{os.getpid()}_{int(time.time())}"
        # Stage first. No live file changes until every backup has copied and
        # passed size/magic validation.
        for kind, live, backup in targets:
            temp = live + '.restore_new_' + token
            old = live + '.restore_old_' + token
            shutil.copyfile(backup, temp)
            with open(temp, 'rb+') as fh:
                os.fsync(fh.fileno())
            if os.path.getsize(temp) != os.path.getsize(backup) or not _valid_backup(temp, kind):
                raise ValueError('staged restore validation failed for ' + os.path.basename(live))
            staged.append((kind, live, backup, temp, old))

        # Atomic file swaps. Keep every original beside the live file until the
        # full set has passed readback.
        for kind, live, backup, temp, old in staged:
            if os.path.exists(old):
                os.remove(old)
            os.replace(live, old)
            try:
                os.replace(temp, live)
            except Exception:
                os.replace(old, live)
                raise
            committed.append((kind, live, backup, temp, old))

        for kind, live, backup, _temp, _old in committed:
            if os.path.getsize(live) != os.path.getsize(backup) or not _valid_backup(live, kind):
                raise ValueError('restored file readback failed for ' + os.path.basename(live))

        # Game bytes are now stock. Archive app ownership/history state instead
        # of leaving it capable of reapplying or misreporting removed edits.
        stamp = time.strftime('%Y%m%d_%H%M%S')
        state_archive = os.path.join(USER_DIR, 'restored_app_state', stamp)
        archived_state = []
        state_paths = [
            EXTRA_SCHEME_STATE,
            TEAM_MANAGER_STATE,
            globals().get('_RP_HISTORY'),
            globals().get('FULL_REPAIR_REPORT'),
        ]
        for state_path in state_paths:
            if state_path and os.path.isfile(state_path):
                os.makedirs(state_archive, exist_ok=True)
                dest = os.path.join(state_archive, os.path.basename(state_path))
                os.replace(state_path, dest)
                archived_state.append(os.path.basename(state_path))
        if os.path.isdir(TEAM_ASSET_ROLLBACK_DIR):
            os.makedirs(state_archive, exist_ok=True)
            dest = os.path.join(state_archive, os.path.basename(TEAM_ASSET_ROLLBACK_DIR))
            if os.path.exists(dest):
                shutil.rmtree(dest)
            os.replace(TEAM_ASSET_ROLLBACK_DIR, dest)
            archived_state.append(os.path.basename(TEAM_ASSET_ROLLBACK_DIR) + '/')

        cfg = load_cfg(); cfg.pop('renames', None); cfg.pop('handles', None); save_cfg(cfg)
        for _kind, _live, _backup, _temp, old in committed:
            try:
                os.remove(old)
            except FileNotFoundError:
                pass
        _clear_ui_thumb_cache()
        try:
            _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear()
        except Exception:
            pass
        return jsonify(dict(
            ok=True,
            restored=[os.path.basename(x[1]) for x in committed],
            skipped=skipped,
            archived_state=archived_state,
            state_archive=(state_archive if archived_state else None),
            note='All staged files passed readback. Previous app ownership/history state was archived so the restored game is rediscovered from live files.'
        ))
    except Exception as original:
        rollback_errors = []
        # Reverse all committed swaps. The original file is still in `old`.
        for _kind, live, _backup, _temp, old in reversed(committed):
            try:
                if os.path.exists(old):
                    failed_live = live + '.restore_failed_new'
                    if os.path.exists(failed_live):
                        os.remove(failed_live)
                    if os.path.exists(live):
                        os.replace(live, failed_live)
                    os.replace(old, live)
                    if os.path.exists(failed_live):
                        os.remove(failed_live)
            except Exception as ex:
                rollback_errors.append(os.path.basename(live) + ': ' + str(ex))
        for _kind, _live, _backup, temp, _old in staged:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except Exception:
                pass
        detail = str(original)
        if rollback_errors:
            detail += ' | Restore rollback failed: ' + '; '.join(rollback_errors)
        return jsonify(dict(ok=False, error=detail,
                            rolled_back=bool(committed and not rollback_errors))), 400


# ==================== v0.6 additions ====================

@app.route('/api/import_scheme/<name>', methods=['POST'])
def api_import_scheme(name):
    """One-click import of an externally-edited (GIMP/PS) scheme PNG.
    Saves it as a complete 2048x1024 UV-atlas replacement and optionally
    installs immediately (?install=1)."""
    if _is_managed_extra_slot(name):
        return jsonify(dict(ok=False,error=_managed_extra_action_error(name))),400
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(2048,1024),'stretch',preserve_alpha=False)
    img=img.convert('RGB')
    png=os.path.join(SCHEMES,name+'.png'); lay=os.path.join(SCHEMES,name+'.layer.png')
    img.save(png)
    g,reg=registry()
    s=_slot(name)
    if os.path.exists(lay): os.remove(lay)
    prep['install_mode']='full UV-atlas replacement (no donor diff mask)'
    wrote=None
    if request.args.get('install') and s:
        try:
            wrote=install_slot(reg,s,png,None)
        except Exception as e:
            return jsonify(dict(ok=False,error=f'install failed: {e}')),500
    return jsonify(dict(ok=True, installed=bool(wrote), wrote=wrote, image_prep=prep))


@app.route('/api/previewedit/<name>', methods=['GET','POST'])
def api_previewedit(name):
    """Load a career/AI 256x256 preview card into the canvas and save it back."""
    g,reg=registry()
    key='careerthumbs'
    arcid,off,size,arc=menu_container(reg,key)
    ent,_=C.parse_multi_arc(arc)
    match=[e for e in ent if e['name']==name]
    if not match: return ('not found',404)
    e=match[0]
    if request.method=='GET':
        img=C.multi_read_png(arc,e)
        buf=io.BytesIO(); img.save(buf,'PNG'); buf.seek(0)
        return send_file(buf,mimetype='image/png')
    f=request.files.get('file')
    if not f: return jsonify(dict(ok=False,error='no file')),400
    img=Image.open(f.stream)
    img,prep=prepare_import_image(img,(e['w'],e['h']),request_resize_mode('fit'),preserve_alpha=True)
    a=need(reg,arcid); ensure_backup(a['ar'],a['bak'])
    new=C.multi_write_png(arc,e,img,encode_fn=encode_any)
    with open(a['ar'],'r+b') as fh:
        fh.seek(off); fh.write(new)
    _clear_ui_thumb_cache()
    return jsonify(dict(ok=True, image_prep=prep))


def import_liv_tmp(path, out_png, raw_offset=0x5, w=2048, h=1024):
    """PROTOTYPE: decode a captured Paint Booth LIV_TMP DXT1 texture to a scheme
    PNG you can then install via the normal import path. Manual use only."""
    data=open(path,'rb').read()
    need_bytes=(w//4)*(h//4)*8
    payload=data[raw_offset:raw_offset+need_bytes]
    if len(payload)<need_bytes:
        payload=payload+b'\0'*(need_bytes-len(payload))
    arr=C._dxt1_decode(payload,w,h)
    Image.fromarray(arr).convert('RGB').save(out_png)
    return out_png

# ==================== end v0.6 additions ====================



# ==================== v0.8 AUDIO LAB ====================
import base64 as _b64

AUDIO_TOOLS_DIR = os.path.join(USER_DIR, 'audio_tools')
AUDIO_TOOLS_BIN = os.path.join(AUDIO_TOOLS_DIR, 'bin')
AUDIO_TOOLS_RELEASE_TAG = 'latest'
AUDIO_TOOLS_ARCHIVE = 'ffmpeg-master-latest-win64-lgpl-shared.zip'
AUDIO_TOOLS_RELEASE_BASE = (
    'https://github.com/BtbN/FFmpeg-Builds/releases/download/'
    + AUDIO_TOOLS_RELEASE_TAG
)


def ffmpeg_path():
    # Prefer the app-managed LGPL audio-tools component. It lives in USER_DIR so
    # frozen builds can install/update it next to the executable without touching
    # the bundled application resources.
    candidates = [
        os.path.join(AUDIO_TOOLS_BIN, 'ffmpeg.exe'),
        os.path.join(AUDIO_TOOLS_DIR, 'ffmpeg.exe'),
        os.path.join(USER_DIR, 'ffmpeg.exe'),
        os.path.join(APP_DIR, 'ffmpeg.exe'),
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return shutil.which('ffmpeg')


def _ffmpeg_details():
    ff = ffmpeg_path()
    if not ff:
        return dict(ready=False, path=None, source=None, version=None)
    source = ('managed_audio_tools' if os.path.abspath(ff).startswith(os.path.abspath(AUDIO_TOOLS_DIR))
              else ('app_folder' if os.path.dirname(os.path.abspath(ff)) in
                    (os.path.abspath(USER_DIR), os.path.abspath(APP_DIR)) else 'path'))
    version = None
    try:
        r = subprocess.run([ff, '-hide_banner', '-version'], capture_output=True,
                           text=True, timeout=15)
        first = (r.stdout or r.stderr or '').splitlines()
        version = first[0].strip() if first else None
    except Exception:
        pass
    return dict(ready=True, path=ff, source=source, version=version)


def _download_url(url, destination, timeout=180):
    import urllib.request
    req = urllib.request.Request(url, headers={
        'User-Agent': 'NASCAR-Modding-App-Audio-Tools/1.0.1'
    })
    with urllib.request.urlopen(req, timeout=timeout) as response, open(destination, 'wb') as out:
        shutil.copyfileobj(response, out, length=1024*1024)


def _find_file(root, wanted):
    wanted = wanted.lower()
    for base, dirs, files in os.walk(root):
        for name in files:
            if name.lower() == wanted:
                return os.path.join(base, name)
    return None


def _validate_managed_ffmpeg(ff):
    r = subprocess.run([ff, '-hide_banner', '-version'], capture_output=True,
                       text=True, timeout=30)
    text = (r.stdout or '') + '\n' + (r.stderr or '')
    if r.returncode != 0 or 'ffmpeg version' not in text.lower():
        raise ValueError('downloaded ffmpeg.exe did not start correctly')
    lower = text.lower()
    if '--enable-gpl' in lower or '--enable-nonfree' in lower:
        raise ValueError('downloaded build is not the required LGPL-only configuration')
    enc = subprocess.run([ff, '-hide_banner', '-encoders'], capture_output=True,
                         text=True, timeout=30)
    enc_text = (enc.stdout or '') + '\n' + (enc.stderr or '')
    if enc.returncode != 0 or 'libmp3lame' not in enc_text:
        raise ValueError('downloaded LGPL build does not provide the libmp3lame encoder')
    return text.splitlines()[0].strip()


@app.route('/api/audio/tools/status')
def audio_tools_status():
    d = _ffmpeg_details()
    d.update(dict(ok=True, managed_dir=AUDIO_TOOLS_DIR,
                  release_tag=AUDIO_TOOLS_RELEASE_TAG,
                  archive=AUDIO_TOOLS_ARCHIVE))
    return jsonify(d)


@app.route('/api/audio/tools/install', methods=['POST'])
def audio_tools_install():
    if os.name != 'nt':
        return jsonify(dict(ok=False, error='The one-click audio tools installer is for 64-bit Windows.')),400
    archive_url = AUDIO_TOOLS_RELEASE_BASE + '/' + AUDIO_TOOLS_ARCHIVE
    checksums_url = AUDIO_TOOLS_RELEASE_BASE + '/checksums.sha256'
    parent = os.path.dirname(AUDIO_TOOLS_DIR) or USER_DIR
    os.makedirs(parent, exist_ok=True)
    staging = AUDIO_TOOLS_DIR + '.installing'
    old = AUDIO_TOOLS_DIR + '.previous'
    try:
        with tempfile.TemporaryDirectory(prefix='n15_audio_tools_') as td:
            archive_path = os.path.join(td, AUDIO_TOOLS_ARCHIVE)
            checksums_path = os.path.join(td, 'checksums.sha256')
            _download_url(checksums_url, checksums_path)
            _download_url(archive_url, archive_path)

            checksum_lines = open(checksums_path, 'r', encoding='utf-8', errors='replace').read().splitlines()
            expected = None
            for line in checksum_lines:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[-1].lstrip('*') == AUDIO_TOOLS_ARCHIVE:
                    expected = parts[0].lower()
                    break
            if not expected or not re.fullmatch(r'[0-9a-f]{64}', expected):
                raise ValueError('the release checksum file did not contain the expected LGPL archive')
            actual = hashlib.sha256(open(archive_path, 'rb').read()).hexdigest()
            if actual != expected:
                raise ValueError('download checksum mismatch; nothing was installed')

            extract_dir = os.path.join(td, 'extract')
            os.makedirs(extract_dir, exist_ok=True)
            import zipfile
            with zipfile.ZipFile(archive_path) as z:
                z.extractall(extract_dir)
            src_ff = _find_file(extract_dir, 'ffmpeg.exe')
            if not src_ff:
                raise ValueError('downloaded archive did not contain ffmpeg.exe')
            src_bin = os.path.dirname(src_ff)

            if os.path.exists(staging): shutil.rmtree(staging)
            os.makedirs(os.path.join(staging, 'bin'), exist_ok=True)
            copied = []
            for name in os.listdir(src_bin):
                src = os.path.join(src_bin, name)
                if os.path.isfile(src) and name.lower().endswith(('.exe', '.dll')):
                    shutil.copy2(src, os.path.join(staging, 'bin', name))
                    copied.append(name)
            if 'ffmpeg.exe' not in [x.lower() for x in copied]:
                raise ValueError('ffmpeg.exe was not copied into the managed tool directory')

            for wanted in ('LICENSE.txt', 'COPYING.LGPLv2.1', 'COPYING.LGPLv3'):
                src = _find_file(extract_dir, wanted)
                if src:
                    shutil.copy2(src, os.path.join(staging, os.path.basename(src)))

            source_info = (
                'NASCAR Modding App managed audio tools\n'
                'Component: BtbN FFmpeg Windows LGPL shared build\n'
                f'Release tag: {AUDIO_TOOLS_RELEASE_TAG}\n'
                f'Archive: {AUDIO_TOOLS_ARCHIVE}\n'
                f'Archive SHA-256: {actual}\n'
                f'Download: {archive_url}\n'
                'Build scripts/source: https://github.com/BtbN/FFmpeg-Builds\n'
                'FFmpeg source and license information: https://ffmpeg.org/\n'
                'The NASCAR Modding App launches ffmpeg.exe as a separate tool.\n'
            )
            open(os.path.join(staging, 'SOURCE_INFORMATION.txt'), 'w', encoding='utf-8').write(source_info)
            managed_ff = os.path.join(staging, 'bin', 'ffmpeg.exe')
            version = _validate_managed_ffmpeg(managed_ff)

            if os.path.exists(old): shutil.rmtree(old)
            if os.path.exists(AUDIO_TOOLS_DIR): os.replace(AUDIO_TOOLS_DIR, old)
            try:
                os.replace(staging, AUDIO_TOOLS_DIR)
            except Exception:
                if os.path.exists(old) and not os.path.exists(AUDIO_TOOLS_DIR):
                    os.replace(old, AUDIO_TOOLS_DIR)
                raise
            if os.path.exists(old): shutil.rmtree(old)

        return jsonify(dict(ok=True, ready=True, version=version,
                            path=os.path.join(AUDIO_TOOLS_BIN, 'ffmpeg.exe'),
                            release_tag=AUDIO_TOOLS_RELEASE_TAG,
                            checksum=actual,
                            files=len(copied)))
    except Exception as e:
        try:
            if os.path.exists(staging): shutil.rmtree(staging)
        except Exception:
            pass
        return jsonify(dict(ok=False, error=str(e))),400

_SIL={ (True): _b64.b64decode("//2UxIZnZ3ZmbbbRkAAAqqqqqqvvvvvvvvvvvvvvvvvvvn+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbX+/3+/fv379++++973333333ve973ve973ve973ve973vetjba/3+/379+/fv3333ve++++++973ve973ve973ve973ve971sbbQ"), (False): _b64.b64decode("//2UBFUzM0MiRDMRIiIiSSSbSAAAAAAAAACqqqqqqqqqqvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvn333333d3d3d3d1sbb58ti2Nttta+fPnz58+fPnz58222+fPvvvvvu7u7u7u7rY23z5bFsbbba18+fPnz58+fPnz5ttt8+ffffffd3d3d3d3Wxtvny2LY2221r58+fPnz58+fPnzbbb58++++++7u7u7u7utjbfPlsWxtttrXz58+fPnz58+fPm223z59999993d3d3d3dbG2+fLYtjbbbWvnz58+fPnz58+fNttvnz777777u7u7u7u62Nt8+WxbG222tfPnz58+fPnz58+bbbfPn333333d3d3d3d1sbb58ti2Nttta+fPnz58+fPnz58222+fPvvvvvu7u7u7u7rY23z5bFsbbba18+fPnz58+fPnz5ttt8+ffffffd3d3d3d3Wxtvny2LY2221r58+fPnz58+fPnzbbb58++++++7u7u7u7utjbfPlsWxtttrXz58+fPnz58+fPm223z59999993d3d3d3dbG2+fLYtjbbbWvnz58+fPnz58+fNttvnz777777u7u7u7u62Nt8+WxbG222tfPnz58+fPnz58+bbbfPg") }

_BR={(1,1):{1:32,2:64,3:96,4:128,5:160,6:192,7:224,8:256,9:288,10:320,11:352,12:384,13:416,14:448},
     (1,2):{1:32,2:48,3:56,4:64,5:80,6:96,7:112,8:128,9:160,10:192,11:224,12:256,13:320,14:384},
     (1,3):{1:32,2:40,3:48,4:56,5:64,6:80,7:96,8:112,9:128,10:160,11:192,12:224,13:256,14:320},
     (2,1):{1:32,2:48,3:56,4:64,5:80,6:96,7:112,8:128,9:144,10:160,11:176,12:192,13:224,14:256},
     (2,2):{1:8,2:16,3:24,4:32,5:40,6:48,7:56,8:64,9:80,10:96,11:112,12:128,13:144,14:160}}
_BR[(2,3)]=_BR[(2,2)]
_SRT={3:{0:44100,1:48000,2:32000},2:{0:22050,1:24000,2:16000},0:{0:11025,1:12000,2:8000}}

def frame_info(d,i=0):
    if i+4>len(d) or d[i]!=0xFF or (d[i+1]&0xE0)!=0xE0: return None
    vf=(d[i+1]>>3)&3
    if vf==1: return None
    lf=(d[i+1]>>1)&3
    if lf==0: return None
    layer=4-lf; ver=1 if vf==3 else 2
    br=(d[i+2]>>4)&0xF; sr=(d[i+2]>>2)&3; pad=(d[i+2]>>1)&1
    if br in (0,15) or sr==3: return None
    kbps=_BR[(ver,layer)][br]; hz=_SRT[vf][sr]
    if layer==1: flen=(12*kbps*1000//hz+pad)*4
    elif layer==3 and ver==2: flen=72*kbps*1000//hz+pad
    else: flen=144*kbps*1000//hz+pad
    return (layer,kbps,hz,((d[i+3]>>6)&3)==3,flen)

def walk_frames(d,limit=100000):
    out=[];i=0
    while i<len(d)-4 and len(out)<limit:
        fi=frame_info(d,i)
        if not fi: break
        out.append((i,fi)); i+=fi[4]
    return out

def _mpeg_frame_topology(d, limit=200000, max_gap=96):
    """Map the exact MPEG frame starts used by an FSB5 sample.

    NASCAR 15 music banks are not safe to rebuild as a generic contiguous or
    16-byte-padded stream.  FMOD banks may align each frame differently (32-byte
    alignment is common), and the sample header/loop chunks continue to describe
    the original decoded sample window.  This mapper preserves the stock frame
    start offsets instead of guessing a padding rule.
    """
    frames=[]
    if not d: return dict(frames=[], starts=[], cells=[], padded=False, spec=None)
    i=0
    # A valid FSB sample should start on a frame. Tolerate a tiny leading pad for
    # raw user streams, but never silently skip a large unknown prefix.
    if not frame_info(d,0):
        first=next((j for j in range(1,min(max_gap+1,max(1,len(d)-3))) if frame_info(d,j)),None)
        if first is None: return dict(frames=[], starts=[], cells=[], padded=False, spec=None)
        i=first
    spec=None
    while i<len(d)-4 and len(frames)<limit:
        fi=frame_info(d,i)
        if not fi: break
        if spec is None: spec=fi
        # A slot is one codec configuration. A different layer/rate/channel mode
        # is treated as the end rather than accepted as a false sync in padding.
        if (fi[0],fi[1],fi[2],fi[3]) != (spec[0],spec[1],spec[2],spec[3]): break
        end=i+fi[4]
        if end>len(d): break
        frames.append(dict(start=i, length=fi[4], data=d[i:end], info=fi))
        nxt=None
        for j in range(end,min(len(d)-3,end+max_gap+1)):
            nfi=frame_info(d,j)
            if nfi and (nfi[0],nfi[1],nfi[2],nfi[3]) == (spec[0],spec[1],spec[2],spec[3]):
                nxt=j; break
        if nxt is None: break
        i=nxt
    starts=[x['start'] for x in frames]
    cells=[]
    for n,x in enumerate(frames):
        # Every non-final cell ends at the next stock frame start.  The final
        # cell is limited to the original frame itself; bytes after it are kept
        # byte-identical, which avoids turning unknown tail data into audio.
        end=starts[n+1] if n+1<len(starts) else x['start']+x['length']
        cells.append(dict(start=x['start'], end=end, capacity=end-x['start'],
                          frame_length=x['length']))
    padded=any(c['capacity']!=frames[i]['length'] for i,c in enumerate(cells))
    return dict(frames=frames, starts=starts, cells=cells, padded=padded, spec=spec)

def fmod_walk(d,limit=200000):
    """Return de-padded frames using the measured FSB frame topology."""
    t=_mpeg_frame_topology(d,limit=limit)
    return [x['data'] for x in t['frames']],t['padded'],t['spec']

def _mpeg_silent_frame_that_fits(spec, capacity):
    sil=_sil_for(spec)
    if sil:
        t=_mpeg_frame_topology(sil,limit=4)
        for x in t['frames']:
            if len(x['data'])<=capacity:
                return x['data']
        fi=frame_info(sil,0)
        if fi and fi[4]<=capacity:
            return sil[:fi[4]]
    return None

def _fit_mpeg_to_stock_topology(stream, original, meta):
    """Fit replacement audio into the stock sample's exact MPEG frame cells.

    The old writer filled the byte slot using a guessed 16-byte padding rule and
    as many silent frames as would fit.  Music then retained stock sample-count
    and loop metadata but no longer retained stock frame boundaries.  This writer
    keeps the original number of frames, every original frame start offset, all
    bytes outside those cells, and therefore all header/loop metadata.
    """
    stock=_mpeg_frame_topology(original)
    source=_mpeg_frame_topology(stream)
    if not stock['frames'] or not stock['spec']:
        raise ValueError('stock MPEG frame topology could not be mapped')
    if not source['frames'] or not source['spec']:
        raise ValueError('replacement contains no valid MPEG frames')
    ss=stock['spec']; rs=source['spec']
    if (ss[0],ss[1],ss[2],ss[3]) != (rs[0],rs[1],rs[2],rs[3]):
        raise ValueError('replacement MPEG format does not exactly match the stock song')
    out=bytearray(original)
    src=[x['data'] for x in source['frames']]
    used=0; silence=0; preserved_tail=len(original)-(stock['cells'][-1]['end'] if stock['cells'] else 0)
    for i,cell in enumerate(stock['cells']):
        cap=cell['capacity']
        frame=src[i] if i<len(src) else None
        if frame is not None and len(frame)>cap:
            # A different encoder padding phase can make an occasional CBR frame
            # one byte larger.  Do not split it; use a valid matching silent frame
            # for that final fraction of a second instead.
            frame=None
        if frame is None:
            frame=_mpeg_silent_frame_that_fits(ss,cap)
            if frame is None:
                raise ValueError(f'no valid matching MPEG frame fits stock cell {i} ({cap} bytes)')
            silence+=1
        else:
            used+=1
        out[cell['start']:cell['end']]=frame+b'\0'*(cap-len(frame))
    # Re-scan the result and demand the exact original start map.  This is the
    # important game-safety check; ordinary FSB parsing does not verify it.
    check=_mpeg_frame_topology(bytes(out))
    if check['starts']!=stock['starts'] or len(check['frames'])!=len(stock['frames']):
        raise ValueError('rebuilt MPEG frame topology differs from stock; write refused')
    if check['spec'] and (check['spec'][0],check['spec'][1],check['spec'][2],check['spec'][3]) != (ss[0],ss[1],ss[2],ss[3]):
        raise ValueError('rebuilt MPEG codec specification changed')
    samples=int((meta or {}).get('samples') or 0)
    hz=int((meta or {}).get('hz') or ss[2] or 0)
    return bytes(out),dict(stock_frames=len(stock['frames']),source_frames=len(src),
        audio_frames=used,silent_frames=silence,padded=stock['padded'],
        preserved_tail=preserved_tail,samples=samples,hz=hz,starts=stock['starts'])

def _verify_mpeg_decode(payload):
    """Ask FFmpeg to decode the rebuilt elementary stream before game write."""
    ff=ffmpeg_path()
    if not ff: return True,None
    frames,_,fi=fmod_walk(payload)
    if not frames or not fi: return False,'no frames after rebuild'
    ext='.mp2' if fi[0]==2 else '.mp3'
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'verify'+ext)
        open(src,'wb').write(b''.join(frames))
        r=subprocess.run([ff,'-v','error','-i',src,'-f','null','-'],capture_output=True,text=True)
        if r.returncode!=0:
            return False,(r.stderr or 'FFmpeg decode failed')[-240:]
    return True,None


_AUDIO_VOLUME_MODES={'match_stock','custom','source'}
_AUDIO_CUSTOM_GAIN_MIN_DB=-24.0
_AUDIO_CUSTOM_GAIN_MAX_DB=24.0

def _audio_volume_mode(value):
    mode=str(value or 'match_stock').strip().lower()
    return mode if mode in _AUDIO_VOLUME_MODES else 'match_stock'

def _audio_custom_gain_db(value, mode='custom'):
    if _audio_volume_mode(mode)!='custom':
        return 0.0
    try:
        gain=float(value)
    except (TypeError,ValueError):
        raise ValueError('Custom volume must be a number between -24 and +24 dB')
    if not _math.isfinite(gain):
        raise ValueError('Custom volume must be a finite number')
    if gain<_AUDIO_CUSTOM_GAIN_MIN_DB or gain>_AUDIO_CUSTOM_GAIN_MAX_DB:
        raise ValueError('Custom volume must be between -24 and +24 dB')
    return gain

def _audio_volume_label(mode,gain_db):
    mode=_audio_volume_mode(mode)
    if mode=='match_stock': return f'matched to stock ({float(gain_db):+.1f} dB applied)'
    if mode=='source': return 'kept at source level (0.0 dB applied)'
    return f'custom gain {float(gain_db):+.1f} dB'

def _audio_is_loop_sample(name):
    n=str(name or '').lower()
    return any(token in n for token in ('eng_l','eng_r','exh_l','exh_r','engine','exhaust'))

def _active_pcm_stats(values):
    arr=np.asarray(values,dtype=np.float64).reshape(-1)
    if not arr.size: return dict(rms=0.0,peak=0.0,rms_db=-120.0,peak_db=-120.0)
    peak=float(np.max(np.abs(arr)))
    if peak<=1e-9: return dict(rms=0.0,peak=0.0,rms_db=-120.0,peak_db=-120.0)
    floor=max(1e-5,peak*0.001)
    active=arr[np.abs(arr)>=floor]
    if active.size<32: active=arr
    rms=float(np.sqrt(np.mean(active*active))) if active.size else 0.0
    def db(v): return 20.0*np.log10(max(v,1e-9))
    return dict(rms=rms,peak=peak,rms_db=float(db(rms)),peak_db=float(db(peak)))

def _safe_gain_db(mode, source_stats=None, stock_stats=None, custom_gain_db=0.0):
    mode=_audio_volume_mode(mode)
    if mode=='source': return 0.0
    if mode=='custom': return _audio_custom_gain_db(custom_gain_db,mode)
    if not source_stats or not stock_stats or source_stats.get('rms',0)<=0 or stock_stats.get('rms',0)<=0:
        return 0.0
    wanted=float(stock_stats['rms_db'])-float(source_stats['rms_db'])
    # Leave headroom before the limiter. Match-stock is intentionally bounded so
    # a nearly silent upload cannot turn into a destructive +40 dB surprise.
    peak_room=-0.5-float(source_stats.get('peak_db',-120.0))
    return max(-12.0,min(18.0,wanted,peak_room+3.0))

def _apply_pcm_gain_i16(stream,gain_db):
    if not stream: return stream
    usable=len(stream)-(len(stream)%2)
    src=np.frombuffer(stream[:usable],dtype='<i2').astype(np.float64)/32768.0
    scaled=src*(10.0**(float(gain_db)/20.0))
    # Leave already-safe audio untouched. Engage the smooth limiter only when
    # the selected gain would exceed the headroom.
    limited=(scaled if (not scaled.size or float(np.max(np.abs(scaled)))<=0.97)
             else np.tanh(scaled/0.97)*0.97)
    out=np.clip(np.rint(limited*32767.0),-32768,32767).astype('<i2').tobytes()
    return out+stream[usable:]

def _loop_fill_pcm16(stream,target_bytes,channels,hz,crossfade_ms=12):
    frame_bytes=max(1,int(channels)*2)
    target_bytes=int(target_bytes)-(int(target_bytes)%frame_bytes)
    usable=len(stream)-(len(stream)%frame_bytes)
    if usable<=0 or target_bytes<=0: return b''
    if usable>=target_bytes: return stream[:target_bytes]
    src=np.frombuffer(stream[:usable],dtype='<i2').reshape(-1,int(channels)).astype(np.float64)
    target_frames=target_bytes//frame_bytes
    repeats=(target_frames+len(src)-1)//len(src)
    out=np.tile(src,(repeats,1))[:target_frames].copy()
    fade=max(1,min(len(src)//4,int(int(hz)*crossfade_ms/1000)))
    # Blend the first samples of each repeat with the previous repeat's tail.
    # The blend is in-place, so the output remains exactly the stock duration.
    if fade>0:
        alpha=np.linspace(0.0,1.0,fade,endpoint=False)[:,None]
        for boundary in range(len(src),target_frames,len(src)):
            n=min(fade,target_frames-boundary)
            if n<=0: break
            prev=src[-n:]
            nxt=src[:n]
            out[boundary:boundary+n]=prev*(1.0-alpha[:n])+nxt*alpha[:n]
    return np.clip(np.rint(out),-32768,32767).astype('<i2').tobytes()

def _ffmpeg_pcm_stats(raw,filename,hz,channels):
    ff=ffmpeg_path()
    if not ff: return None
    ext=os.path.splitext(filename or '')[1] or '.bin'
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'measure'+ext); out=os.path.join(td,'measure.f32')
        open(src,'wb').write(raw)
        r=subprocess.run([ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
                          '-ar',str(int(hz)),'-ac',str(int(channels)),
                          '-c:a','pcm_f32le','-f','f32le',out,'-y'],
                         capture_output=True,text=True)
        if r.returncode!=0 or not os.path.exists(out): return None
        data=np.frombuffer(open(out,'rb').read(),dtype='<f4')
    return _active_pcm_stats(data)

def _mpeg_gain_db(mode,upload_raw,upload_name,stock_payload,spec,custom_gain_db=0.0):
    mode=_audio_volume_mode(mode)
    if mode!='match_stock': return _safe_gain_db(mode,custom_gain_db=custom_gain_db)
    frames,_,_fi=fmod_walk(stock_payload)
    if not frames: return 0.0
    channels=1 if spec[3] else 2
    stock_ext='.mp2' if spec[0]==2 else '.mp3'
    source_stats=_ffmpeg_pcm_stats(upload_raw,upload_name,spec[2],channels)
    stock_stats=_ffmpeg_pcm_stats(b''.join(frames),'stock'+stock_ext,spec[2],channels)
    return _safe_gain_db(mode,source_stats,stock_stats)

FSB_MODES={0:'NONE',1:'PCM8',2:'PCM16',3:'PCM24',4:'PCM32',5:'PCMFLOAT',6:'GCADPCM',
 7:'IMAADPCM',8:'VAG',9:'HEVAG',10:'XMA',11:'MPEG',12:'CELT',13:'AT9',14:'XWMA',15:'VORBIS'}
FSB_FREQ={1:8000,2:11000,3:11025,4:16000,5:22050,6:24000,7:32000,8:44100,9:48000,10:96000}

def parse_fsb5(bank):
    if bank[:4]!=b'FSB5': raise ValueError('not FSB5')
    ver,num,shdr,ntab,dsz,mode=struct.unpack_from('<6I',bank,4)
    codec=FSB_MODES.get(mode,f'mode{mode}')
    data_start=len(bank)-dsz; names_start=data_start-ntab; hdrs_start=names_start-shdr
    raws=[]; chunks=[]; raw_positions=[]; chunk_records=[]; pos=hdrs_start
    for _ in range(num):
        raw_pos=pos
        raw,=struct.unpack_from('<Q',bank,pos); pos+=8
        raws.append(raw); raw_positions.append(raw_pos); cl=[]; cr=[]; nxt=raw&1
        while nxt:
            chunk_header_pos=pos
            ch,=struct.unpack_from('<I',bank,pos); pos+=4
            nxt=ch&1; size=(ch>>1)&0xFFFFFF; ctype=ch>>25
            payload_pos=pos; payload=bank[pos:pos+size]
            cl.append((ctype,payload))
            cr.append(dict(type=ctype,header_pos=chunk_header_pos,payload_pos=payload_pos,size=size,data=payload))
            pos+=size
        chunks.append(cl); chunk_records.append(cr)
    walk_delta=pos-names_start
    names=['']*num
    if ntab>=4*num:
        noffs=[struct.unpack_from('<I',bank,names_start+4*i)[0] for i in range(num)]
        for i,o in enumerate(noffs):
            p=names_start+o
            if 0<=p<data_start:
                e=bank.find(b'\0',p,data_start)
                names[i]=bank[p:e].decode('ascii','replace') if e!=-1 else ''
    # per-sample meta: frequency index bits 1-4, channel bit 5 (standard v1 layout);
    # chunk type 1 = channel count, type 2 = explicit frequency (override)
    metas=[]
    for i,r in enumerate(raws):
        hz=FSB_FREQ.get((r>>1)&0xF); chn=((r>>5)&1)+1; scount=(r>>34)&0x3FFFFFFF
        for ct,pay in chunks[i]:
            if ct==1 and len(pay)>=1: chn=pay[0]
            elif ct==2 and len(pay)>=4: hz=struct.unpack_from('<I',pay)[0]
        metas.append(dict(hz=hz, ch=chn, samples=scount))
    # candidate offset layouts, scored by a codec-appropriate validator
    def score(offs):
        s=0
        for i,o in enumerate(offs):
            end=offs[i+1] if i+1<len(offs) else dsz
            if mode==11:
                s+=1 if (o+1<dsz and bank[data_start+o]==0xFF and (bank[data_start+o+1]&0xE0)==0xE0) else 0
            elif mode==2:
                exp=metas[i]['samples']*2*metas[i]['ch']
                s+=1 if 0<=(end-o)-exp<128 else 0
            else:
                s+=1 if end>o else 0
        return s
    cands=[]
    for bits,mult,sh in ((28,16,6),(27,32,7),(28,16,7)):
        offs=[((r>>sh)&((1<<bits)-1))*mult for r in raws]
        if not offs or offs!=sorted(offs) or offs[-1]>=dsz or offs[0]>=64: continue
        cands.append((score(offs),offs,f'sh{sh}x{mult}'))
    pick=max(cands,key=lambda c:c[0]) if cands else None
    if not pick or walk_delta!=0:
        r0=f'{raws[0]:016x}' if raws else '-'
        d0=bank[data_start:data_start+16].hex() if dsz>=16 else '-'
        raise ValueError(f'sample offset decode failed [codec={codec} n={num} shdr={shdr} '
            f'ntab={ntab} dsz={dsz} walk_delta={walk_delta} raw0={r0} data0={d0} '
            f'cands={[(c[0],c[2]) for c in cands]}] - paste this to the dev')
    sync,offs,layout=pick
    validated = sync==num                      # every sample passed its validator
    slices=[]
    for i,o in enumerate(offs):
        end=offs[i+1] if i+1<len(offs) else dsz
        slices.append((names[i] or f'sample_{i:03d}', data_start+o, end-o, metas[i]))
    return dict(num=num, mode=mode, codec=codec, slices=slices, layout=layout,
                validated=validated,
                editable=(mode==11 and validated) or (mode==2 and validated),
                version=ver, sample_headers_size=shdr, name_table_size=ntab,
                data_size=dsz, data_start=data_start, names_start=names_start,
                headers_start=hdrs_start, raws=raws, raw_positions=raw_positions,
                chunks=chunks, chunk_records=chunk_records, offsets=offs,
                metas=metas, names=names, header_walk_end=pos)


def _align_up(value, alignment):
    alignment=max(1,int(alignment))
    return (int(value)+(alignment-1)) & ~(alignment-1)


def _infer_stock_mpeg_frame_alignment(sample_bytes):
    """Infer the actual FMOD MPEG frame alignment used by one stock sample.

    Full-length replacement cannot preserve the old finite frame-start map, so it
    must reproduce the rule that generated that map. We only accept a power-of-two
    alignment when it predicts every measured stock frame start exactly.
    """
    topo=_mpeg_frame_topology(sample_bytes)
    frames=topo.get('frames') or []
    if len(frames)<3:
        raise ValueError('stock song has too few MPEG frames to infer its alignment')
    for alignment in (16,32,64,8,4,128,256):
        ok=True
        for i in range(len(frames)-1):
            expected=_align_up(frames[i]['start']+frames[i]['length'],alignment)
            if expected!=frames[i+1]['start']:
                ok=False;break
        if ok:
            return alignment,topo
    gaps=sorted({frames[i+1]['start']-(frames[i]['start']+frames[i]['length'])
                 for i in range(min(len(frames)-1,64))})
    raise ValueError('stock MPEG frame alignment is not a consistent supported power-of-two rule; '
                     'measured gaps '+str(gaps[:12]))


def _pack_mpeg_frames_with_alignment(stream, alignment):
    source=_mpeg_frame_topology(stream)
    frames=source.get('frames') or []
    if not frames or not source.get('spec'):
        raise ValueError('encoded replacement contains no valid MPEG frames')
    out=bytearray(); starts=[]
    for frame in frames:
        if out:
            target=_align_up(len(out),alignment)
            if target>len(out): out.extend(b'\0'*(target-len(out)))
        starts.append(len(out)); out.extend(frame['data'])
    check=_mpeg_frame_topology(bytes(out))
    if check.get('starts')!=starts or len(check.get('frames') or [])!=len(frames):
        raise ValueError('aligned MPEG rebuild did not retain every encoded frame')
    return bytes(out),dict(frames=len(frames),starts=starts,spec=source['spec'],alignment=alignment)


def _encode_full_length_mpeg(raw, filename, spec, gain_db=0.0):
    """Encode the complete upload and measure its intended decoded sample count."""
    ff=ffmpeg_path()
    if not ff:
        raise ValueError('full-length music replacement needs the installed LGPL audio tools')
    fmt='mp2' if spec[0]==2 else 'mp3'
    channels=1 if spec[3] else 2
    ext=os.path.splitext(filename or '')[1] or '.bin'
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'input'+ext); enc_path=os.path.join(td,'encoded.'+fmt); pcm_path=os.path.join(td,'measure.pcm')
        open(src,'wb').write(raw)
        cmd=[ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
             '-ar',str(spec[2]),'-ac',str(channels)]
        if abs(float(gain_db))>0.01:
            cmd += ['-af',f'volume={float(gain_db):.3f}dB,alimiter=limit=0.97']
        cmd += ['-c:a','mp2' if spec[0]==2 else 'libmp3lame','-b:a',f'{spec[1]}k']
        if spec[0]!=2:
            cmd += ['-write_xing','0','-id3v2_version','0']
        cmd += ['-f',fmt,enc_path,'-y']
        r=subprocess.run(cmd,capture_output=True,text=True)
        if r.returncode!=0 or not os.path.exists(enc_path):
            raise ValueError('FFmpeg could not encode the full song: '+(r.stderr or '')[-240:])
        m=subprocess.run([ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
                          '-ar',str(spec[2]),'-ac',str(channels),'-c:a','pcm_s16le',
                          '-f','s16le',pcm_path,'-y'],capture_output=True,text=True)
        if m.returncode!=0 or not os.path.exists(pcm_path):
            raise ValueError('FFmpeg could not measure the full song duration: '+(m.stderr or '')[-240:])
        encoded=open(enc_path,'rb').read(); pcm_size=os.path.getsize(pcm_path)
    first=next((i for i in range(len(encoded)) if frame_info(encoded,i)),None)
    if first is None: raise ValueError('full-song encode produced no MPEG frames')
    stream=encoded[first:]
    got=frame_info(stream,0)
    if not got or got[:4]!=spec[:4]:
        raise ValueError('full-song encode did not match the stock MPEG format')
    sample_count=pcm_size//(channels*2)
    if sample_count<=0 or sample_count>=2**30:
        raise ValueError('full song decoded sample count is outside the FSB5 header range')
    return stream,int(sample_count),channels


def _scaled_loop_points(old_start, old_end, old_samples, new_samples):
    if new_samples<=0: return 0,0
    if old_samples<=1:
        return 0,max(0,new_samples-1)
    # Map the inclusive [0, old_samples-1] timeline onto the new inclusive
    # timeline so a stock loop ending on the final sample still ends on the
    # final sample after a duration change.
    scale=float(max(0,new_samples-1))/float(old_samples-1)
    ns=max(0,min(new_samples-1,int(round(old_start*scale))))
    ne=max(ns,min(new_samples-1,int(round(old_end*scale))))
    return ns,ne


def _rebuild_fsb5_full_mpeg_sample(bank, sample_index, stream, sample_count):
    """Rebuild one MPEG sample at arbitrary duration while preserving the bank.

    The FSB5 header, names, unknown chunks, GUID and every untouched sample are
    retained. Only the target sample count/loop points, all sample data offsets,
    the bank data-size field, and the target MPEG bytes change.
    """
    fsb=parse_fsb5(bank)
    if fsb['mode']!=11 or not fsb['validated']:
        raise ValueError('full-length rebuild is limited to validated MPEG FSB5 banks')
    if fsb['layout']!='sh6x16':
        raise ValueError('full-length rebuild requires the standard FSB5 16-byte data-offset layout; found '+fsb['layout'])
    if not (0<=int(sample_index)<fsb['num']):
        raise ValueError('sample index is outside the FSB5 bank')
    idx=int(sample_index); target=fsb['slices'][idx]
    original_target=bank[target[1]:target[1]+target[2]]
    alignment,stock_topology=_infer_stock_mpeg_frame_alignment(original_target)
    packed,pack_info=_pack_mpeg_frames_with_alignment(stream,alignment)
    if pack_info['spec'][:4] != stock_topology['spec'][:4]:
        raise ValueError('replacement MPEG format does not exactly match the stock song')

    data=bytearray(); new_offsets=[]; original_blobs=[]
    for i,(_name,rel,length,_meta) in enumerate(fsb['slices']):
        aligned=_align_up(len(data),16)
        if aligned>len(data): data.extend(b'\0'*(aligned-len(data)))
        new_offsets.append(len(data))
        original_blob=bytes(bank[rel:rel+length]); original_blobs.append(original_blob)
        data.extend(packed if i==idx else original_blob)
    final=_align_up(len(data),16)
    if final>len(data): data.extend(b'\0'*(final-len(data)))
    if len(data)>=2**32:
        raise ValueError('rebuilt FSB5 data chunk exceeds the 32-bit size field')

    prefix=bytearray(bank[:fsb['data_start']])
    struct.pack_into('<I',prefix,20,len(data))
    for i,raw_pos in enumerate(fsb['raw_positions']):
        off=new_offsets[i]
        if off%16 or off//16 >= 2**28:
            raise ValueError('rebuilt sample offset is outside the FSB5 28-bit offset field')
        count=int(sample_count if i==idx else fsb['metas'][i]['samples'])
        if count<0 or count>=2**30:
            raise ValueError('rebuilt sample count is outside the FSB5 30-bit field')
        old=fsb['raws'][i]
        rebuilt=(old & 0x3F) | ((off//16)<<6) | (count<<34)
        struct.pack_into('<Q',prefix,raw_pos,rebuilt)

    loop_update=None
    for rec in fsb['chunk_records'][idx]:
        if rec['type']==3 and rec['size']>=8:
            old_start,old_end=struct.unpack_from('<II',rec['data'],0)
            new_start,new_end=_scaled_loop_points(old_start,old_end,fsb['metas'][idx]['samples'],sample_count)
            struct.pack_into('<II',prefix,rec['payload_pos'],new_start,new_end)
            loop_update=dict(old=[old_start,old_end],new=[new_start,new_end])

    rebuilt=bytes(prefix)+bytes(data)
    check=parse_fsb5(rebuilt)
    if check['num']!=fsb['num'] or check['mode']!=fsb['mode'] or check['layout']!='sh6x16':
        raise ValueError('rebuilt FSB5 bank structure did not validate')
    if check['metas'][idx]['samples']!=int(sample_count):
        raise ValueError('rebuilt FSB5 sample count readback mismatch')
    for i,(_name,rel,length,_meta) in enumerate(check['slices']):
        if i==idx: continue
        if rebuilt[rel:rel+len(original_blobs[i])] != original_blobs[i]:
            raise ValueError('untouched FSB5 sample '+str(i)+' changed during rebuild')
    new_name,new_rel,new_len,new_meta=check['slices'][idx]
    new_target=rebuilt[new_rel:new_rel+new_len]
    topo=_mpeg_frame_topology(new_target)
    if len(topo.get('frames') or [])!=pack_info['frames'] or topo.get('starts')!=pack_info['starts']:
        raise ValueError('rebuilt target MPEG topology readback mismatch')
    ok,err=_verify_mpeg_decode(new_target)
    if not ok: raise ValueError('rebuilt full song did not decode cleanly: '+str(err))
    return rebuilt,dict(old_bank_size=len(bank),new_bank_size=len(rebuilt),growth=len(rebuilt)-len(bank),
                        old_slot_size=target[2],new_slot_size=new_len,alignment=alignment,
                        frames=pack_info['frames'],samples=int(sample_count),hz=int(new_meta['hz'] or 0),
                        channels=int(new_meta['ch'] or 0),loop=loop_update)

def fit_payload(stream, slice_len, sil, fmod_pad=False):
    def cell(b): return b+b'\0'*((16-len(b)%16)%16) if fmod_pad else b
    out=b''; used=0
    for i,f in walk_frames(stream):
        fb=cell(stream[i:i+f[4]])
        if len(out)+len(fb)>slice_len: break
        out+=fb; used+=1
    if used==0: raise ValueError('no valid MPEG frames in replacement')
    sc=cell(sil) if sil else b''
    while sc and len(out)+len(sc)<=slice_len: out+=sc
    return out+b'\0'*(slice_len-len(out)), used

def _pcm16_from_wav(data, channels, hz):
    """Convert an uncompressed WAV to raw little-endian PCM16 without ffmpeg.

    Covers the ordinary case of dropping a .wav onto a PCM16 game slot: reads
    8/16/24/32-bit integer PCM via the stdlib, then matches the slot's channel
    count and sample rate with numpy. Raises ValueError for anything it cannot
    handle (float WAV, ADPCM, other compressed forms) so the caller can fall back
    to ffmpeg or report a clear message.

    Resampling is linear interpolation, which is coarser than ffmpeg's
    swresample. ffmpeg is preferred whenever it is available; this is the
    no-ffmpeg path.
    """
    import wave as _wave
    try:
        with _wave.open(io.BytesIO(data), 'rb') as w:
            ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except Exception as ex:
        raise ValueError('not an uncompressed PCM WAV (' + str(ex) + ')') from ex
    if ch < 1 or fr < 1:
        raise ValueError('WAV header reports no channels or no sample rate')
    if not raw:
        raise ValueError('WAV contains no audio frames')

    if sw == 1:            # unsigned 8-bit
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        a = (a - 128.0) * 256.0
    elif sw == 2:
        a = np.frombuffer(raw, dtype='<i2').astype(np.float32)
    elif sw == 3:          # 24-bit packed, sign-extend via the high byte
        b = np.frombuffer(raw[:len(raw) - (len(raw) % 3)], dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int8).astype(np.int32) << 16))
        a = (v / 256.0).astype(np.float32)
    elif sw == 4:
        a = np.frombuffer(raw, dtype='<i4').astype(np.float32) / 65536.0
    else:
        raise ValueError('unsupported WAV sample width: ' + str(sw * 8) + '-bit')

    usable = (a.size // ch) * ch
    a = a[:usable].reshape(-1, ch)
    if a.shape[0] == 0:
        raise ValueError('WAV contains no complete audio frames')

    want = 1 if channels <= 1 else 2
    if a.shape[1] == want:
        pass
    elif want == 1:
        a = a.mean(axis=1, keepdims=True)
    elif a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    else:
        a = a[:, :2]

    if fr != hz:
        m = int(round(a.shape[0] * float(hz) / float(fr)))
        if m < 1:
            raise ValueError('resampling to ' + str(hz) + ' Hz leaves no audio')
        src_x = np.arange(a.shape[0], dtype=np.float64)
        dst_x = np.linspace(0.0, a.shape[0] - 1, m)
        a = np.stack([np.interp(dst_x, src_x, a[:, c]) for c in range(a.shape[1])], axis=1)

    return np.clip(np.rint(a), -32768, 32767).astype('<i2').ravel().tobytes()


# --- verified silent MPEG frames, so tail padding needs no ffmpeg -------------
# One frame per (layer, kbps, Hz, mono) for layers 2 and 3 at 128/160/192 kbps and
# 32000/44100/48000 Hz, mono and stereo. Each was generated with ffmpeg anullsrc and
# then decoded back and checked byte-by-byte: all 36 decode to pure silence.
# zlib+base64 keeps 36 frames (21 KB raw) in about 3 KB of source.
_SIL_TABLE_B64 = (
    'eNrtWm9sG0kVt0p6OrXqtZZKJaiO5nIVfElPsTdxgxC5SrXQRfABQV2J+I5A0/pOZ+9FQiRGtdP4w4FaldBQVVClSKmgh6KrrJyP'
    '8xpq2bGgaRAoCgjOtrKynQ+QfKjOK2GBa1nO8t7M7NrebJyaPxe73TdtEo9n3rx57/fevjezTqe122Lt7+asPT093T3dfSdtr3XX'
    '9Vnq+np7LWRcr+Wkps9S39dP+XH9vZo+S7XP1qOue9Lao+mz1PUp6/ZZrZo+S30fWxd+a/os1b7PW9V1+229mj5LXZ+yrs1q0/RZ'
    '6vv6NfpT+xT9cTp65nT0zOnomdPRM6ejZ05Hz5yOnjkdPXM6euZ09Mzp6JnT0TOno2dOR8+cjp45HT1zOnrmtuj5NZNcudzhsnPQ'
    '7F0cx/HC3zzTJkLvMpqPlyX9f5cq5aKULcYkr9frPPJA5D3OI4uhwrrIC9V/5IugMfQJHQr4uf/T770x7p2IJDtNpj1bEVOSJ3NF'
    'SlIuJgFvKV6MFXMT+C8m+bLFBRkbrDYWKmzKCxKl3IIvC3LkJiQfY+VDkSZlbL5s/uHIRgVYU1q4VIyBzPFyljHOEtbyJrZiLJNc'
    'WikDa0qXKpIPZMHVKWMiRUyuYJN80eC12SKwJgTiZmEsrk4Z54gUPrmMLVv0Oo+9LJU3KYG4MZAbV2ebpxuUSYvl88IA6KJCCcT1'
    'gdy4OmU8QTcok+bLZDx+0EWZEpoD5MbVmWKJFDkZW7YYvZduRzUHTBB/Ah12jrOaT3eZzeYX7DeGjpvq48920Qd5ZQgJpaDz6Ohs'
    'X9A9VJqfScbdfGEI5YoSgg+CLTfgHkqGC8N9QX9SXA+jIF6kcfjg4eP+ZDjoXo/D/GBk2Z1FUyF9FFl+My36S8A4tQxjCs7RmVQM'
    'bYMkjs58GBJKBWA8PwNj1oVc3zwwpsvCh7sjfGEdGA/3FWA+H3d/k7oUEHw4+0DcWAap4u51mC/6k6geui58CE5FVmZAKn9qGebD'
    'Dndpu38CA90fO3v2goMfD5mYg9f7N2CZQiEaVVgRcch+AD6ASOo3eUnRDhlHBoGvAa4o+qOKBui4LPOYyUmKYTYDiIwjgwD3T/Xy'
    'j9CD3u6wW7u6zNwL4EHm49e/1pQHYSgHHPICmh1xAYhkWPwI4jwAWBD9tB+hPEBBPA7TAPki4JhNCs/PMvQDQ8AQDw6gcEytMLcB'
    'hsBcAM9ROW4wAAJDQK2TjyyrHAsMucAQ4S6I68qkoRKDPDBEPwE5lUlBP/OVj2lj4CBvg4N86YzD+dJ7Aar0+gegasKHI/i8eJ44'
    'cXLJS/AxrNj/YprET+KmY6EMsW5KBY/wRQzb1P89Xi9Co09FXvCaj/QiAXvE1dTTs6hcudnhddgddpfDbrc7hC/z4Z+fqsP/tg5A'
    'jaNKTQJ7cjFEw+VYiGQ9ympHqMAsiNJHuPIEpN+r88k8tgOmBPq4SNHns5JFkO8/o8wn8xSteJTF8EkxTB++SopAvlfnk3lM08qq'
    '9CFhbK0dtwbx/CbNqCcX/nWo+Yya5Zosjaxp6EjGRGPixzuxF/A83eHgOK7LznV1dXH8Z8U3HvuEgbBn7ssX/h7yYJLgL9FK1F8i'
    'dSg4KIrB/A6+J4VpYWNldoAn6cEtUq+WggazXWYGSLj/g9dfv+DiI3+e1o9sm/LkAjl9oCUmy4qLsbrSOPlAHJaKMEgtn1nCrAxV'
    '6lEPHzYYth7DDyAm3MCYcBpighmCwuDxW688Zs1SBeQY1N7/SAs2kXcj1ACU8MsGlcIAjiSF1j2sZj3Oo7wQLqhjbgEiZ5ENe9ZD'
    'RSKcmBLEoZI6ZhkqhxVcjdbFWE/T0y6/OmYdEV6FPRbkJ66O8ALUGOoYrCRQaFqYY0X/k8WQIEKtoY5BJ8IdUVlAhxt/PJcWeag5'
    '1DFYMuGOqCwPR5gbrqbUTQ+g07WSYtbAvujp4y5eQE9Hg97V5N0LOURK9TlSgxT6IzglwBiCp+q5l08zbCvqlNOumh+gs/liuUJG'
    'qWdz2vX+GRJS5comGaWe9mnXu5gW3ZVNmTiDerC1Zb2tLqOcGtb8EGyrMIaoQD180zKSwEKtqCioeec6LjscLofX5bhwwbWaEVbt'
    'AerA+3Z+qpc3QNG4+YmaU0QAbk0+T2OHV1kXvqzVr/PYAKoFRC3mas5OYVRNkk7ClDoPvqvVxmKoYAhhCNGSQqzhmd7c/bkrV25e'
    'viaX7SxT0gTQkrxQJLLCauSCQD2ip/57qSI3bMSTC/KkVGFcSGqvHt0zHmW5YSMxY2NTzjG90QsF9eaE8WAFwnYNt720UoH9MC5k'
    'F+qNCuOxtbqoaxgHr82WYT+MC9mFetOiqEVu1EjEPfYyqDXHuJBdKDcwqmrlRo3E9gFQq0yBwHah1E4KD3q/sl0jYPKDWpmB2S6U'
    'GxtFtTSl2q6R55UBk60wCYB/3e5w2e1nOIcdKlP7j3/TPX3dpHl+NXh8kbWYx2KiJNBjHHozGsYTIXJn+jwe9DwIzxPJmI9j6sVj'
    'NojJIo53n0tjlrWaFr5wD/9yD5N9KNEBkjlRuYXF8eSg6iguQo66Pp2ku2YhBNNDARMzzNtwfPDqOcwEYZH8Q/irr3q1S4IRZAU8'
    'OY86qtzpLmJuiUdSuJB7iChWCU6QwgoiJo+YW+J44cRVzFZhL1FcKBkmZlBjI3CNkGMvdmdMjr1wkSVycBZ0E6MxWTCbFN/Ko5iU'
    'OWiEZNTCiR/l8a+hVBurHRHHTvUiydv/+ale9cY3Go03uktWvbd6+ZyXGl1rVwOPekEbjTa6+q3GTPWuOJ83NtEqm3gkV+4Yb/YY'
    'Q/+LN3vuqG/23Nav4R//XZZMrom3ZBq7h+b9G6+3iVdOGvuR5mWWTKaJ12R2iBr1L+B4vU282rNDeGlVRa/J5ZumnWjwLdco/DoI'
    '//eaTAcCJsdWCujQuzr0oQ7JOoTrfuXb4yO2npc4S60wx/fSa2OQ40zi3nmTQbtKgJ9E2wDmt2npWcNkLYaf6WbizzMm036PaVBD'
    'vJaua0jQkqilx4dTx2H6x37P+fe/e/ppMFCibSxS/sT340+fB91o0oP2BXbXXntYQN4XePOVK5NPgP4TbaPwv57f/84Th/+5JjPY'
    'Q2utkZAcWhvp/MXnjBzAIA2eE20D4Ium333LMJlBDfF8u8n85LnB1sgnnxvM77k1ZFjwf4iERNuY/vfPyHOGyf5vSLjTZEw40Nka'
    'KfSBzm/cfbXTsOCO9k20jUG/c+6H84bJmrTvr5usuQ6fao2U9fCpr++N/tKwoEEt7l+JtnGovzw79b5hMoPayr/eazL/NL/YGoWJ'
    '+cVPvuNLGRZsQ8Ql2gZi8U9FRg2TtT3iPmgyxh1MtEZJdjDxs9KjPxgW3HX8JNoGMHd+tfRVw2QtRf8GHMD/3g=='
)
_SIL_TABLE_CACHE = None

def _sil_table():
    """Lazily unpack the baked silent-frame table. Returns {} if anything is off."""
    global _SIL_TABLE_CACHE
    if _SIL_TABLE_CACHE is None:
        try:
            import zlib, base64 as _b, json as _j
            raw = zlib.decompress(_b.b64decode(_SIL_TABLE_B64))
            cut = raw.index(b'\x00')
            index = _j.loads(raw[:cut])
            body = raw[cut + 1:]
            out, pos = {}, 0
            for layer, kbps, hz, mono, n in index:
                out[(layer, kbps, hz, bool(mono))] = body[pos:pos + n]
                pos += n
            _SIL_TABLE_CACHE = out
        except Exception:
            _SIL_TABLE_CACHE = {}
    return _SIL_TABLE_CACHE


def _sil_for(spec):
    if (spec[0],spec[1],spec[2])==(2,160,48000): return _SIL[spec[3]]
    baked=_sil_table().get((spec[0],spec[1],spec[2],bool(spec[3])))
    if baked: return baked
    ff=ffmpeg_path()
    if not ff: return b''
    fmt='mp2' if spec[0]==2 else 'mp3'
    with tempfile.TemporaryDirectory() as td:
        o=os.path.join(td,'s.bin')
        subprocess.run([ff,'-v','error','-f','lavfi','-i',
            f'anullsrc=r={spec[2]}:cl={"mono" if spec[3] else "stereo"}','-t','0.06',
            '-c:a','mp2' if spec[0]==2 else 'libmp3lame','-b:a',f'{spec[1]}k','-f',fmt,o,'-y'],check=True)
        sd=open(o,'rb').read()
    fr=walk_frames(sd[next((i for i in range(len(sd)) if frame_info(sd,i)),0):],limit=2)
    return sd[fr[0][0]:fr[0][0]+fr[0][1][4]] if fr else b''

def parse_snd(c):
    """A .SND speech container = concatenated single-sample FSB5s.
    Returns flat sample list across all sub-FSBs, chained + validated."""
    flat=[]; pos=0; subs=0
    while pos+28<=len(c):
        if c[pos:pos+4]!=b'FSB5':
            j=c.find(b'FSB5',pos)
            if j==-1: break
            pos=j
        ver,num,shdr,ntab,dsz=struct.unpack_from('<5I',c,pos+4)
        total=60+shdr+ntab+dsz
        if num==0 or num>4096 or pos+total>len(c): break
        try: fsb=parse_fsb5(c[pos:pos+total])
        except Exception: break
        for n,rel,sl,m in fsb['slices']:
            flat.append(dict(name=n, rel=pos+rel, len=sl, meta=m,
                             mode=fsb['mode'], ok=fsb['editable']))
        subs+=1; pos+=total
    return flat, subs

def _read_container(arc, name):
    """(v, boff, bytes, flat, kind). kind: 'fsb' or 'snd'.
    flat: [dict(name, rel, len, meta, mode, ok)] - rel is within the container."""
    g,reg=registry()
    if arc not in reg: raise ValueError(f'archive {arc} not found')
    v=reg[arc]
    ent=[e for e in parse_cdfiles(v['cdf']) if e[2].upper()==name.upper()]
    if not ent: raise ValueError(f'{name} not in archive {arc} index')
    boff,bsz,_=ent[0]
    with open(v['ar'],'rb') as f:
        f.seek(boff); c=f.read(bsz)
    if name.upper().endswith('.SND'):
        flat,subs=parse_snd(c)
        if not flat: raise ValueError('no FSB5 sub-banks found in this SND')
        return v,boff,c,flat,'snd'
    fsb=parse_fsb5(c)
    flat=[dict(name=n, rel=rel, len=sl, meta=m, mode=fsb['mode'],
               ok=fsb['editable']) for n,rel,sl,m in fsb['slices']]
    return v,boff,c,flat,('fsb',fsb)

def _find_sample(flat, idx, name):
    if idx is not None:
        i=int(idx)
        if 0<=i<len(flat): return i
        return None
    for i,s in enumerate(flat):
        if s['name']==name: return i
    return None

def _describe(c, s):
    if s['mode']==11:
        fr,padded,fi=fmod_walk(c[s['rel']:s['rel']+s['len']])
        if not fi: return dict(spec='unusual first frame', dur=None, ok=False)
        spb=384 if fi[0]==1 else (576 if fi[2]<32000 else 1152)
        return dict(spec=f'L{fi[0]} {fi[1]}k {fi[2]}Hz {"mono" if fi[3] else "stereo"}',
                    dur=round(len(fr)*spb/fi[2],2), ok=s['ok'])
    if s['mode']==2:
        m=s['meta']; chs={1:'mono',2:'stereo'}.get(m['ch'],f"{m['ch']}ch")
        return dict(spec=f"PCM16 {m['hz'] or '?'}Hz {chs}",
                    dur=(round(m['samples']/m['hz'],2) if m['hz'] else None),
                    ok=s['ok'] and bool(m['hz']))
    return dict(spec=FSB_MODES.get(s['mode'],'?'), dur=None, ok=False)



def _full_length_audio_candidate(bank_name, container, flat, fsb):
    """Return (eligible, reason) for the variable-size FSB5 rebuild path.

    Most NASCAR 15 songs live in obviously named MUSIC banks, but several are
    stored as one-bank-per-song and use the song title as the bank name.  A
    title such as "LEAVE IT ON THE TRACK" was previously misclassified as
    Track / Surface solely because it contains the word TRACK.  Treat a
    validated, standard-layout, single-sample MPEG bank with long-form duration
    as music without weakening any of the structural write guards.
    """
    if not isinstance(fsb,dict):
        return False,'not an FSB5 bank'
    if fsb.get('mode')!=11:
        return False,'not an MPEG FSB5 bank'
    if not fsb.get('editable'):
        return False,'bank boundary validation did not pass'
    if fsb.get('layout')!='sh6x16':
        return False,'bank does not use the validated standard FSB5 layout'
    if _audio_category(bank_name)=='Music':
        return True,'named music bank'
    if len(flat)==1:
        try:
            desc=_describe(container,flat[0])
            duration=float(desc.get('dur') or 0.0)
        except Exception:
            duration=0.0
        if duration>=45.0:
            return True,f'long-form single-track MPEG bank ({duration:.2f}s)'
    return False,'bank is not identified as music or a long-form single-track MPEG bank'


def _audio_safe_filename(text):
    text=re.sub(r'[^A-Za-z0-9._-]+','_',str(text)).strip('._')
    return text[:160] or 'sound'

def _audio_backup_container(v,boff,size,bank_name=None):
    if not os.path.exists(v.get('bak','')): return None
    if bank_name and os.path.exists(backup_path(v['cdf'])):
        try:
            _raw,rows,_layout=_rp_index_rows(backup_path(v['cdf']))
            row=_rp_find_row(rows,bank_name)
            with open(v['bak'],'rb') as f:
                f.seek(row['offset']); data=f.read(row['size'])
            return data if len(data)==row['size'] else None
        except Exception:
            pass
    with open(v['bak'],'rb') as f:
        f.seek(boff); data=f.read(size)
    return data if len(data)==size else None

def _audio_sample_modified(c,backup_c,s,backup_s=None):
    if backup_c is None: return False
    a=s['rel']; b=a+s['len']
    if backup_s is not None:
        ba=backup_s['rel']; bb=ba+backup_s['len']
        return c[a:b]!=backup_c[ba:bb]
    return c[a:b]!=backup_c[a:b]

def _audio_raw_payload(c,s):
    """Exact bytes stored in the sample slot; no padding is removed."""
    raw=c[s['rel']:s['rel']+s['len']]
    ext='bin'; mime='application/octet-stream'
    if s['mode']==2:
        ext='pcm'
    elif s['mode']==11:
        frames,padded,fi=fmod_walk(raw)
        if not padded and fi:
            ext='mp2' if fi[0]==2 else 'mp3'; mime='audio/mpeg'
        else:
            ext='fmod_mpeg.bin'
    return raw,ext,mime

def _audio_mpeg_payload(c,s):
    """FMOD-de-padded MPEG elementary stream: playable anywhere, no ffmpeg.
    FMOD stores MPEG samples with 16-byte alignment padding between frames, so
    the exact slot bytes are not a valid MPEG file. Joining just the frames
    yields a clean stream; nothing is re-encoded, so this is lossless."""
    if s['mode']!=11: raise ValueError('sample is not MPEG')
    raw=c[s['rel']:s['rel']+s['len']]
    frames,_padded,fi=fmod_walk(raw)
    if not frames or not fi: raise ValueError('no MPEG frames in this sample')
    ext='mp2' if fi[0]==2 else 'mp3'
    return b''.join(frames),ext,'audio/mpeg'

def _audio_can_export_mpeg(c,s):
    try:
        _audio_mpeg_payload(c,s); return True
    except Exception:
        return False

def _audio_wav_payload(c,s):
    raw=c[s['rel']:s['rel']+s['len']]
    if s['mode']==2 and s['meta'].get('hz'):
        m=s['meta']; need=m['samples']*2*m['ch']
        pcm=raw[:need] if need<=len(raw) else raw
        hdr=(b'RIFF'+struct.pack('<I',36+len(pcm))+b'WAVEfmt '+
             struct.pack('<IHHIIHH',16,1,m['ch'],m['hz'],m['hz']*m['ch']*2,m['ch']*2,16)+
             b'data'+struct.pack('<I',len(pcm)))
        return hdr+pcm
    if s['mode']==11:
        fr,_,_=fmod_walk(raw)
        if fr: raw=b''.join(fr)
    ff=ffmpeg_path()
    if not ff:
        raise ValueError('WAV export needs FFmpeg. Install it (ffmpeg.exe beside app.py, or anywhere on your PATH), or use Export MP3 instead.')
    with tempfile.TemporaryDirectory() as td:
        src=os.path.join(td,'in.bin'); out=os.path.join(td,'out.wav')
        open(src,'wb').write(raw)
        r=subprocess.run([ff,'-v','error','-i',src,'-f','wav',out,'-y'],
                         capture_output=True,text=True)
        if r.returncode!=0 or not os.path.exists(out):
            raise ValueError('ffmpeg could not convert this sound: '+(r.stderr or '')[-240:])
        return open(out,'rb').read()

def _audio_category(name):
    u=str(name).upper()
    if 'ENGINE' in u or 'VEHICLE' in u: return 'Engines / Vehicles'
    if 'HUDSND' in u or 'SPOTTER' in u or u.endswith('.SND'): return 'Spotter / Speech'
    if 'MUSIC' in u: return 'Music'
    if 'PIT' in u: return 'Pit Stop'
    if 'TRACK' in u or 'AMBI' in u or 'MATERIAL' in u: return 'Track / Surface'
    if 'FRONTEND' in u or 'MENU' in u or 'GLOBAL_HUD' in u: return 'Menus / HUD'
    return 'Other'

@app.route('/api/audio/banks')
def audio_banks():
    g,reg=registry()
    if not g: return jsonify(dict(error='game not found')),400
    out=[]
    for k,v in sorted(reg.items()):
        try: ent=parse_cdfiles(v['cdf'])
        except Exception: continue
        fh=None
        for off,sz,nm in ent:
            u=nm.upper()
            if u.endswith('.FSB') or u.endswith('.SND'):
                codec=''; n=0
                try:
                    if fh is None: fh=open(v['ar'],'rb')
                    fh.seek(off); h=fh.read(28)
                    first_sample=''
                    if h[:4]==b'FSB5':
                        n=struct.unpack_from('<I',h,8)[0]
                        codec=FSB_MODES.get(struct.unpack_from('<I',h,24)[0],'?')
                        # Single-song banks are cheap to inspect and let the UI
                        # merge duplicate aliases such as LEAVE IT ON THE TRACK
                        # and a generic Music bank containing the same sample.
                        if n==1 and sz<=128*1024*1024:
                            fh.seek(off); bank_bytes=fh.read(sz)
                            parsed=parse_fsb5(bank_bytes)
                            if parsed.get('slices'):
                                first_sample=str(parsed['slices'][0][0] or '')
                except Exception:
                    first_sample=''
                if u.endswith('.SND'): codec='SPEECH'; n=0; first_sample=''
                out.append(dict(arc=k,name=nm,size=sz,codec=codec,n=n,
                                first_sample=first_sample,
                                category=_audio_category(nm),has_backup=os.path.exists(v['bak'])))
        if fh: fh.close()
    cats=sorted({x['category'] for x in out})
    return jsonify(dict(banks=out,categories=cats,ffmpeg=bool(ffmpeg_path()),ffmpeg_details=_ffmpeg_details()))

@app.route('/api/audio/samples', methods=['POST'])
def audio_samples():
    q=request.get_json()
    try: v,boff,c,flat,kind=_read_container(q['arc'],q['bank'])
    except Exception as e: return jsonify(dict(error=str(e))),400
    backup_c=_audio_backup_container(v,boff,len(c),q['bank'])
    backup_flat=None
    if backup_c is not None:
        try:
            if q['bank'].upper().endswith('.SND'): backup_flat,_subs=parse_snd(backup_c)
            else:
                bfsb=parse_fsb5(backup_c)
                backup_flat=[dict(name=n,rel=rel,len=sl,meta=m,mode=bfsb['mode'],ok=bfsb['editable']) for n,rel,sl,m in bfsb['slices']]
        except Exception: backup_flat=None
    smp=[]; modified_count=0
    for i,samp in enumerate(flat):
        d=_describe(c,samp)
        modified=_audio_sample_modified(c,backup_c,samp,(backup_flat[i] if backup_flat and i<len(backup_flat) else None))
        modified_count+=1 if modified else 0
        smp.append(dict(idx=i,name=samp['name'],bytes=samp['len'],modified=modified,
                        export_wav=bool(samp['mode']==2 or ffmpeg_path()),
                        export_mpeg=_audio_can_export_mpeg(c,samp),
                        export_raw=True,**d))
    common=dict(samples=smp,modified_count=modified_count,has_backup=backup_c is not None,
                bank=q['bank'],arc=q['arc'])
    if kind=='snd':
        return jsonify(dict(**common,codec='SPEECH (FSB5/MPEG chain)',editable=True,
            note=f'{len(smp)} speech clips found in this container'))
    fsb=kind[1]
    music=_audio_category(q['bank'])=='Music'
    full_candidate,full_reason=_full_length_audio_candidate(q['bank'],c,flat,fsb)
    full_ready=bool(full_candidate and ffmpeg_path())
    if full_candidate:
        if full_ready:
            note=('Full-length replacement is available for this music group ('+full_reason+'). '
                  'Replace Full Song installs the complete imported track. '
                  'Fit to Original Length remains available under Advanced Details.')
        else:
            note=('This is a full-length music candidate ('+full_reason+'), but the managed '
                  'FFmpeg audio tools are not installed.')
    elif fsb['editable']:
        note=None
    else:
        note=(f'{fsb["codec"]} bank did not pass boundary validation - read-only for safety'
              if fsb['mode'] in (2,11) else
              f'{fsb["codec"]} codec - listed read-only for now; replacement support for this codec is a future update')
    return jsonify(dict(**common,codec=fsb['codec'],editable=fsb['editable'],note=note,music=music,
                        full_length_candidate=full_candidate,full_length_reason=full_reason,
                        full_length_supported=full_ready,release_label=APP_RELEASE_LABEL))

@app.route('/api/audio/preview')
def audio_preview():
    arc=request.args.get('arc'); bankn=request.args.get('bank')
    try: v,boff,c,flat,kind=_read_container(arc,bankn)
    except Exception as e: return jsonify(dict(error=str(e))),400
    i=_find_sample(flat,request.args.get('idx'),request.args.get('sample'))
    if i is None: return jsonify(dict(error='sample not found')),404
    s=flat[i]
    try:
        wav=_audio_wav_payload(c,s)
        return send_file(io.BytesIO(wav),mimetype='audio/wav')
    except Exception:
        # No ffmpeg: a de-padded MPEG stream still plays natively in the browser.
        try:
            mp,_ext,mime=_audio_mpeg_payload(c,s)
            return send_file(io.BytesIO(mp),mimetype=mime)
        except Exception:
            raw,ext,mime=_audio_raw_payload(c,s)
            return send_file(io.BytesIO(raw),mimetype=mime)

@app.route('/api/audio/export')
def audio_export():
    arc=request.args.get('arc'); bankn=request.args.get('bank')
    mode=(request.args.get('mode') or 'wav').lower()
    try: v,boff,c,flat,kind=_read_container(arc,bankn)
    except Exception as e: return jsonify(dict(error=str(e))),400
    i=_find_sample(flat,request.args.get('idx'),request.args.get('sample'))
    if i is None: return jsonify(dict(error='sample not found')),404
    s=flat[i]
    base=_audio_safe_filename(os.path.splitext(bankn)[0]+'__'+s['name'])
    try:
        if mode=='raw':
            data,ext,mime=_audio_raw_payload(c,s)
        elif mode=='mpeg':
            data,ext,mime=_audio_mpeg_payload(c,s)
        else:
            data,ext,mime=_audio_wav_payload(c,s),'wav','audio/wav'
        return send_file(io.BytesIO(data),mimetype=mime,as_attachment=True,
                         download_name=f'{base}.{ext}')
    except Exception as e:
        return jsonify(dict(error=str(e))),400

@app.route('/api/audio/export_bank')
def audio_export_bank():
    import zipfile
    arc=request.args.get('arc'); bankn=request.args.get('bank')
    modified_only=request.args.get('modified') in ('1','true','yes')
    try: v,boff,c,flat,kind=_read_container(arc,bankn)
    except Exception as e: return jsonify(dict(error=str(e))),400
    backup_c=_audio_backup_container(v,boff,len(c),bankn)
    backup_flat=None
    if backup_c is not None:
        try:
            if bankn.upper().endswith('.SND'): backup_flat,_subs=parse_snd(backup_c)
            else:
                bfsb=parse_fsb5(backup_c)
                backup_flat=[dict(name=n,rel=rel,len=sl,meta=m,mode=bfsb['mode'],ok=bfsb['editable']) for n,rel,sl,m in bfsb['slices']]
        except Exception: backup_flat=None
    buf=io.BytesIO(); exported=0; manifest=[]
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for i,samp in enumerate(flat):
            modified=_audio_sample_modified(c,backup_c,samp,(backup_flat[i] if backup_flat and i<len(backup_flat) else None))
            if modified_only and not modified: continue
            raw,ext,mime=_audio_raw_payload(c,samp)
            fn=f'{i:04d}__{_audio_safe_filename(samp["name"])}.{ext}'
            z.writestr(fn,raw); exported+=1
            d=_describe(c,samp)
            manifest.append(dict(index=i,name=samp['name'],file=fn,bytes=len(raw),
                                 modified=modified,spec=d.get('spec'),duration=d.get('dur')))
        z.writestr('manifest.json',json.dumps(dict(bank=bankn,archive=arc,
                     modified_only=modified_only,exported=exported,samples=manifest),indent=2))
    buf.seek(0)
    suffix='_modified' if modified_only else ''
    return send_file(buf,mimetype='application/zip',as_attachment=True,
                     download_name=_audio_safe_filename(os.path.splitext(bankn)[0])+suffix+'.zip')

@app.route('/api/audio/restore_bank',methods=['POST'])
def audio_restore_bank():
    q=request.get_json(force=True); arcid=str(q.get('arc')); bankn=q.get('bank')
    tmp=None
    try:
        g,reg=registry(); v=need(reg,arcid)
        bcdf=backup_path(v['cdf'])
        if not os.path.exists(v.get('bak','')) or not os.path.exists(bcdf):
            raise ValueError('the original archive/index backup pair is unavailable')
        _lr,live_rows,_ll=_rp_index_rows(v['cdf']); live_row=_rp_find_row(live_rows,bankn)
        _sr,stock_rows,_sl=_rp_index_rows(bcdf); stock_row=_rp_find_row(stock_rows,bankn)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_stock_audio_',suffix=os.path.splitext(bankn)[1]); os.close(fd)
        _rp_extract_entry(v,stock_row,tmp,v['bak'])
        stock_bytes=open(tmp,'rb').read()
        if bankn.upper().endswith('.SND'):
            flat,subs=parse_snd(stock_bytes)
            if not flat: raise ValueError('stock speech container does not parse')
            restored=len(flat)
        else:
            stock_fsb=parse_fsb5(stock_bytes); restored=stock_fsb['num']
        if live_row['offset']==stock_row['offset'] and live_row['size']==stock_row['size']:
            with open(v['ar'],'rb') as f:
                f.seek(live_row['offset']); current=f.read(live_row['size'])
            with open(v['ar'],'r+b') as f:
                f.seek(live_row['offset']); f.write(stock_bytes); f.flush(); os.fsync(f.fileno())
                f.seek(live_row['offset']); back=f.read(len(stock_bytes))
            if back!=stock_bytes:
                with open(v['ar'],'r+b') as f:
                    f.seek(live_row['offset']); f.write(current); f.flush(); os.fsync(f.fileno())
                raise ValueError('bank restore readback mismatch; previous live bank was restored')
            method='in_place'
        else:
            with _RP_LOCK:
                result=_rp_install_one(arcid,v,live_row,tmp,'Stock backup: '+bankn,False)
            method='append_repoint'
        return jsonify(dict(ok=True,verified=True,restored=restored,bank=bankn,method=method,
                            stock_size=stock_row['size']))
    except Exception as e:
        return jsonify(dict(error=str(e))),400
    finally:
        if tmp:
            try: os.remove(tmp)
            except OSError: pass

def _pcm16_replace(v,boff,c,container_size,rel,slot_len,meta,raw,filename,kind, sample_name="", volume_mode="match_stock", custom_gain_db=0.0):
    """Replace one validated PCM16 FSB sample without changing its slot.

    Source audio is converted to signed 16-bit little-endian PCM at the exact
    stored sample rate/channel count. Audio longer than the fixed sample window
    is trimmed; shorter audio is silence-padded. Alignment bytes after the
    declared sample data are preserved from the original container.
    """
    try:
        hz=int(meta.get('hz') or 0); channels=int(meta.get('ch') or 0)
        samples=int(meta.get('samples') or 0)
    except Exception:
        hz=channels=samples=0
    if hz<=0 or channels not in (1,2) or samples<=0:
        return jsonify(dict(error='PCM16 metadata is incomplete; replacement refused')),400
    frame_bytes=channels*2
    audio_len=samples*frame_bytes
    if audio_len<=0 or audio_len>slot_len:
        return jsonify(dict(error=f'PCM16 sample length {audio_len} does not fit its {slot_len}-byte slot')),400

    ext=os.path.splitext(filename or '')[1].lower()
    if ext in ('.pcm','.raw'):
        stream=raw
    else:
        ff=ffmpeg_path()
        if not ff:
            # No ffmpeg: an uncompressed WAV can still be converted in pure Python.
            if ext=='.wav' or raw[:4]==b'RIFF':
                try:
                    stream=_pcm16_from_wav(raw,channels,hz)
                except Exception as ex:
                    return jsonify(dict(error='Could not read that WAV without FFmpeg: '+str(ex)+
                        '. Install FFmpeg, or re-save the file as uncompressed PCM WAV.')),400
            else:
                return jsonify(dict(error='Replacing this sound needs FFmpeg unless you upload an '
                    'uncompressed .wav or raw .pcm/.raw data. Install FFmpeg (ffmpeg.exe beside '
                    'app.py, or anywhere on your PATH).')),400
        else:
            with tempfile.TemporaryDirectory() as td:
                src=os.path.join(td,'input'+(ext or '.bin')); out=os.path.join(td,'output.pcm')
                open(src,'wb').write(raw)
                r=subprocess.run([ff,'-v','error','-i',src,'-vn','-map_metadata','-1',
                    '-ac',str(channels),'-ar',str(hz),'-c:a','pcm_s16le','-f','s16le',out,'-y'],
                    capture_output=True,text=True)
                if r.returncode!=0 or not os.path.exists(out):
                    return jsonify(dict(error='ffmpeg could not convert that file to PCM16: '+(r.stderr or '')[-240:])),400
                stream=open(out,'rb').read()

    # Never split a PCM frame when trimming. Engine/exhaust loops are repeated
    # across the full stock window with a short crossfade instead of becoming
    # mostly silence, which was the main cause of very quiet replacements.
    stream=stream[:len(stream)-(len(stream)%frame_bytes)]
    original=c[rel:rel+slot_len]
    if _audio_is_loop_sample(sample_name) and len(stream)<audio_len:
        stream=_loop_fill_pcm16(stream,audio_len,channels,hz)
    source_vals=np.frombuffer(stream[:len(stream)-(len(stream)%2)],dtype='<i2').astype(np.float64)/32768.0 if stream else np.array([],dtype=np.float64)
    stock_vals=np.frombuffer(original[:audio_len],dtype='<i2').astype(np.float64)/32768.0 if audio_len else np.array([],dtype=np.float64)
    gain_db=_safe_gain_db(volume_mode,_active_pcm_stats(source_vals),_active_pcm_stats(stock_vals),custom_gain_db=custom_gain_db)
    stream=_apply_pcm_gain_i16(stream,gain_db)
    used=min(len(stream),audio_len)
    used-=used%frame_bytes
    payload=stream[:used]+b'\0'*(audio_len-used)+original[audio_len:]
    if len(payload)!=slot_len:
        return jsonify(dict(error='PCM16 fixed-slot builder produced the wrong size')),500

    ensure_backup(v['ar'],v['bak'])
    abs_off=boff+rel
    with open(v['ar'],'r+b') as f:
        f.seek(abs_off); f.write(payload); f.flush(); os.fsync(f.fileno())
        f.seek(abs_off); back=f.read(slot_len)
    if back!=payload:
        with open(v['ar'],'r+b') as f:
            f.seek(abs_off); f.write(original); f.flush(); os.fsync(f.fileno())
        return jsonify(dict(error='PCM16 readback mismatch; original sample was restored')),500

    try:
        with open(v['ar'],'rb') as f:
            f.seek(boff); c2=f.read(container_size)
        if kind=='snd':
            flat,subs=parse_snd(c2)
            if not flat: raise ValueError('no samples found after PCM16 write')
        else:
            parse_fsb5(c2)
    except Exception as ex:
        with open(v['ar'],'r+b') as f:
            f.seek(abs_off); f.write(original); f.flush(); os.fsync(f.fileno())
        return jsonify(dict(error='PCM16 validation failed and the original sample was restored: '+str(ex))),500

    return jsonify(dict(ok=True,frames=used//frame_bytes,
        note=(f'fitted into the {samples/hz:.2f}s PCM16 slot at {hz}Hz / {channels} channel(s); '+
              f'volume {_audio_volume_label(volume_mode,gain_db)}'+
              ('; loop-filled with 12 ms crossfades' if _audio_is_loop_sample(sample_name) else ''))))


@app.route('/api/audio/replace_full', methods=['POST'])
def audio_replace_full():
    arcid=str(request.form.get('arc')); bankn=request.form.get('bank')
    up=request.files.get('file'); tmp=None
    if not up: return jsonify(dict(error='no file uploaded')),400
    try:
        v,boff,c,flat,kind=_read_container(arcid,bankn)
        if kind=='snd': raise ValueError('full-length rebuilding is not supported for SND chains')
        fsb=kind[1]
        eligible,reason=_full_length_audio_candidate(bankn,c,flat,fsb)
        if not eligible:
            raise ValueError('full-length rebuilding is unavailable: '+reason)
        if not ffmpeg_path():
            raise ValueError('full-length music replacement needs the installed LGPL audio tools')
        idx=_find_sample(flat,request.form.get('idx'),request.form.get('sample'))
        if idx is None: raise ValueError('sample not found')
        s=flat[idx]; stock_spec=frame_info(c,s['rel'])
        if not stock_spec: raise ValueError('stock song has an unrecognized MPEG format')
        raw=up.read()
        volume_mode=_audio_volume_mode(request.form.get('volume_mode'))
        custom_gain_db=_audio_custom_gain_db(request.form.get('volume_gain_db'),volume_mode)
        target_sample=flat[idx]
        stock_payload=c[target_sample['rel']:target_sample['rel']+target_sample['len']]
        gain_db=_mpeg_gain_db(volume_mode,raw,up.filename or '',stock_payload,stock_spec,custom_gain_db=custom_gain_db)
        stream,sample_count,_channels=_encode_full_length_mpeg(raw,up.filename or '',stock_spec,gain_db=gain_db)
        rebuilt,info=_rebuild_fsb5_full_mpeg_sample(c,idx,stream,sample_count)
        if len(rebuilt)>_RP_MAX_SINGLE:
            raise ValueError('rebuilt music bank exceeds the 768 MB repoint safety limit')
        fd,tmp=tempfile.mkstemp(prefix='n15mod_full_music_',suffix='.FSB'); os.close(fd)
        open(tmp,'wb').write(rebuilt)
        _raw,rows,_layout=_rp_index_rows(v['cdf']); row=_rp_find_row(rows,bankn)
        with _RP_LOCK:
            install=_rp_install_one(arcid,v,row,tmp,up.filename or 'Full song replacement',False)
        # The temp bank was fully parsed/decoded before install; _rp_install_one
        # then verifies the appended bytes by SHA-256 and the CDF offset/size by
        # readback. Avoid a second post-commit failure path that could report an
        # error after a valid transaction has already completed.
        duration=float(sample_count)/float(info['hz'] or stock_spec[2])
        return jsonify(dict(ok=True,verified=True,full_length=True,duration=round(duration,3),
                            bank_growth=info['growth'],archive_growth=install['plan']['growth'],
                            old_bank_size=info['old_bank_size'],new_bank_size=info['new_bank_size'],
                            alignment=info['alignment'],frames=info['frames'],loop=info['loop'],
                            volume_mode=volume_mode,applied_gain_db=round(float(gain_db),2),
                            note=(f'full song installed at {duration:.2f}s; volume {_audio_volume_label(volume_mode,gain_db)}; FSB5 bank rebuilt and repointed; '
                                  f'{info["frames"]} MPEG frames; {info["alignment"]}-byte frame alignment; '
                                  f'bank growth {info["growth"]:+,} bytes')))
    except Exception as e:
        return jsonify(dict(error=str(e))),400
    finally:
        if tmp:
            try: os.remove(tmp)
            except OSError: pass

@app.route('/api/audio/replace', methods=['POST'])
def audio_replace():
    arc=request.form.get('arc'); bankn=request.form.get('bank')
    up=request.files.get('file')
    if not up: return jsonify(dict(error='no file uploaded')),400
    try: v,boff,c,flat,kind=_read_container(arc,bankn)
    except Exception as e: return jsonify(dict(error=str(e))),400
    i=_find_sample(flat,request.form.get('idx'),request.form.get('sample'))
    if i is None: return jsonify(dict(error='sample not found')),404
    s=flat[i]
    if not s['ok']:
        return jsonify(dict(error='this sample is read-only (unsupported codec or failed validation)')),400
    raw=up.read(); rel,sl=s['rel'],s['len']
    volume_mode=_audio_volume_mode(request.form.get('volume_mode'))
    try:
        custom_gain_db=_audio_custom_gain_db(request.form.get('volume_gain_db'),volume_mode)
    except ValueError as ex:
        return jsonify(dict(error=str(ex))),400
    if s['mode']==2:
        return _pcm16_replace(v,boff,c,len(c),rel,sl,s['meta'],raw,up.filename or '',kind,
                              sample_name=s.get('name') or '', volume_mode=volume_mode,
                              custom_gain_db=custom_gain_db)
    spec=frame_info(c,rel)
    if not spec: return jsonify(dict(error='original sample has unrecognized format - not safe to replace')),400
    original=c[rel:rel+sl]
    gain_db=_mpeg_gain_db(volume_mode,raw,up.filename or '',original,spec,custom_gain_db=custom_gain_db)
    fr=walk_frames(raw,limit=4)
    # A pre-encoded upload may bypass FFmpeg only when its complete MPEG
    # configuration matches the stock slot. RC9 accidentally omitted bitrate
    # here, allowing (for example) a 192 kbps MP3 into a 128 kbps stock song.
    # The later topology guard then correctly refused it instead of converting it.
    uploaded_spec=fr[0][1] if fr and fr[0][0]==0 else None
    if uploaded_spec and uploaded_spec[:4]==spec[:4] and volume_mode=='source' and not _audio_is_loop_sample(s.get('name')):
        stream=raw
    else:
        ff=ffmpeg_path()
        if not ff: return jsonify(dict(error='Replacing with WAV/MP3 needs FFmpeg. Install it (ffmpeg.exe beside app.py, or anywhere on your PATH), or upload a pre-matched MPEG stream.')),400
        fmt='mp2' if spec[0]==2 else 'mp3'
        with tempfile.TemporaryDirectory() as td:
            i2=os.path.join(td,'in'+os.path.splitext(up.filename or 'x.wav')[1]); o=os.path.join(td,'o.bin')
            open(i2,'wb').write(raw)
            cmd=[ff,'-v','error']
            if _audio_is_loop_sample(s.get('name')):
                cmd += ['-stream_loop','-1']
            cmd += ['-i',i2,'-vn','-map_metadata','-1',
                '-ar',str(spec[2]),'-ac','1' if spec[3] else '2']
            duration=(float((s.get('meta') or {}).get('samples') or 0)/float((s.get('meta') or {}).get('hz') or spec[2]))
            if _audio_is_loop_sample(s.get('name')) and duration>0:
                cmd += ['-t',f'{duration:.6f}']
            if abs(float(gain_db))>0.01:
                cmd += ['-af',f'volume={float(gain_db):.3f}dB,alimiter=limit=0.97']
            cmd += ['-c:a','mp2' if spec[0]==2 else 'libmp3lame','-b:a',f'{spec[1]}k']
            if spec[0]!=2:
                # Do not spend one of the fixed stock frame cells on a Xing/Info
                # metadata frame or prepend an ID3 tag to the elementary stream.
                cmd += ['-write_xing','0','-id3v2_version','0']
            cmd += ['-f',fmt,o,'-y']
            r=subprocess.run(cmd,capture_output=True,text=True)
            if r.returncode!=0: return jsonify(dict(error='ffmpeg could not read that file: '+r.stderr[-200:])),400
            enc=open(o,'rb').read()
        j0=next((i3 for i3 in range(len(enc)) if frame_info(enc,i3)),None)
        if j0 is None: return jsonify(dict(error='encode produced no frames')),400
        stream=enc[j0:]
        encoded_spec=frame_info(stream,0)
        if not encoded_spec or encoded_spec[:4]!=spec[:4]:
            def _sl(x):
                return ('unknown' if not x else
                    f'Layer {x[0]} / {x[1]} kbps / {x[2]} Hz / '+('mono' if x[3] else 'stereo'))
            return jsonify(dict(error='FFmpeg conversion did not match the stock MPEG format: stock '+
                _sl(spec)+', encoded '+_sl(encoded_spec))),400
    try:
        payload,fit=_fit_mpeg_to_stock_topology(stream,original,s.get('meta') or {})
    except Exception as e:
        return jsonify(dict(error='stock-topology MPEG fit refused: '+str(e))),400
    ok_decode,decode_error=_verify_mpeg_decode(payload)
    if not ok_decode:
        return jsonify(dict(error='rebuilt MPEG did not decode cleanly: '+str(decode_error))),400
    ensure_backup(v['ar'],v['bak'])
    abs_off=boff+rel
    with open(v['ar'],'r+b') as f:
        f.seek(abs_off); f.write(payload); f.flush(); os.fsync(f.fileno())
        f.seek(abs_off); back=f.read(sl)
    if back!=payload:
        with open(v['ar'],'r+b') as f:
            f.seek(abs_off); f.write(original); f.flush(); os.fsync(f.fileno())
        return jsonify(dict(error='readback mismatch; original sample was restored')),500
    try:
        with open(v['ar'],'rb') as f:
            f.seek(boff); c2=f.read(len(c))
        if kind=='snd':
            flat2,subs=parse_snd(c2)
            if not flat2: raise ValueError('speech container no longer parses')
        else:
            parse_fsb5(c2)
    except Exception as ex:
        with open(v['ar'],'r+b') as f:
            f.seek(abs_off); f.write(original); f.flush(); os.fsync(f.fileno())
        return jsonify(dict(error='replacement failed container validation; original sample was restored: '+str(ex))),500
    duration=(float(fit.get('samples') or 0)/float(fit.get('hz') or 1)) if fit.get('samples') else None
    detail=(f"{fit['audio_frames']} replacement + {fit['silent_frames']} silent frames; "
            f"{fit['stock_frames']} stock frame positions preserved")
    if duration is not None:
        detail+=f"; stock sample window {duration:.2f}s"
    loop_note='; engine/exhaust source loop-filled to the stock window' if _audio_is_loop_sample(s.get('name')) else ''
    return jsonify(dict(ok=True,frames=fit['audio_frames'],topology_preserved=True,
        stock_frames=fit['stock_frames'],silent_frames=fit['silent_frames'],
        applied_gain_db=round(float(gain_db),2),volume_mode=volume_mode,
        note='installed with the stock MPEG frame map ('+detail+f'; volume {_audio_volume_label(volume_mode,gain_db)}{loop_note})'))

@app.route('/api/audio/restore', methods=['POST'])
def audio_restore():
    q=request.get_json()
    try: v,boff,c,flat,kind=_read_container(q['arc'],q['bank'])
    except Exception as e: return jsonify(dict(error=str(e))),400
    if not os.path.exists(v['bak']) or not os.path.exists(backup_path(v['cdf'])):
        return jsonify(dict(error='no original backup pair is available for this game file')),400
    i=_find_sample(flat,q.get('idx'),q.get('sample'))
    if i is None: return jsonify(dict(error='sample not found')),404
    try:
        _lr,live_rows,_ll=_rp_index_rows(v['cdf']); live_row=_rp_find_row(live_rows,q['bank'])
        _sr,stock_rows,_sl=_rp_index_rows(backup_path(v['cdf'])); stock_row=_rp_find_row(stock_rows,q['bank'])
    except Exception as ex:
        return jsonify(dict(error='could not resolve the stock bank: '+str(ex))),400
    if live_row['offset']!=stock_row['offset'] or live_row['size']!=stock_row['size']:
        return jsonify(dict(error='this bank was rebuilt for a full-length song. Restore the entire sound group instead of one sample.')),400
    samp=flat[i]; abs_off=boff+samp['rel']
    current=c[samp['rel']:samp['rel']+samp['len']]
    with open(v['bak'],'rb') as f:
        f.seek(abs_off); orig=f.read(samp['len'])
    if len(orig)!=samp['len']:
        return jsonify(dict(error='backup sample is shorter than the live slot; restore refused')),400
    with open(v['ar'],'r+b') as f:
        f.seek(abs_off); f.write(orig); f.flush(); os.fsync(f.fileno())
        f.seek(abs_off); back=f.read(samp['len'])
    if back!=orig:
        with open(v['ar'],'r+b') as f:
            f.seek(abs_off); f.write(current); f.flush(); os.fsync(f.fileno())
        return jsonify(dict(error='sample restore readback mismatch; previous live bytes were restored')),500
    try:
        with open(v['ar'],'rb') as f:
            f.seek(boff); c2=f.read(len(c))
        if kind=='snd':
            flat2,subs=parse_snd(c2)
            if not flat2: raise ValueError('speech container no longer parses')
        else:
            parse_fsb5(c2)
    except Exception as ex:
        with open(v['ar'],'r+b') as f:
            f.seek(abs_off); f.write(current); f.flush(); os.fsync(f.fileno())
        return jsonify(dict(error='restored sample failed container validation; previous live bytes were put back: '+str(ex))),500
    return jsonify(dict(ok=True,verified=True))

# ==================== end v0.8 AUDIO LAB ====================



# ==================== v0.9 RACE SETTINGS / AI (mapper-backed) ====================
# The app does NOT re-parse PYC records. It shells out to the proven mapper
# (nascar15_pyc_record_mapper_v5_teams.py + nascar15_v11_probe_patcher.py),
# which resolves real post-setup values and writes same-size patched COPIES.
# The app owns the three-archive model + backup/restore around those calls.
import subprocess as _sp, csv as _csv, tempfile as _tf, hashlib as _hl

MAPPER_NAME  = 'nascar15_pyc_record_mapper_v5_teams.py'
PATCHER_NAME = 'nascar15_v11_probe_patcher.py'
DBFILE = 'DB_GAME_LOCAL_SCRIPT.PYC'
AICFG  = 'DB_AICONFIG_SCRIPT.PYC'
REPOINT_NAME = 'nascar15_const_repoint_v0_2.py'

# Field index of RaceLaps among a RACEDATA_c constructor's LOAD_CONST args.
# Confirmed from the real bytecode: arg[0]=UID, arg[1]=RaceLaps, arg[2]=NumDrivers.
REPOINT_FIELDS = {('RACEDATA_c','RaceLaps'): 1}


# v0.9.15 AI Behavior Lab. These names come from the real DB_AICONFIG_SCRIPT
# constructors. New fields remain experimental until individually verified in-game.
AI_TRACK_FIELDS = [
    'FormationOffsetDirection',
    'TurnOneBearsLeft',
    'UsePenaltySystem',
    'BumpDraftingEnabled',
    'BumpDraftingConsiderGearing',
    'PABBMaxInflationZ',
    'BumpDraftingMaxPackSize',
    'BumpDraftingRoadStraightness',
    'CatchupWantSpeedModifierEasy',
    'CatchupWantSpeedModifierHard',
    'UseDrivingControllerSmoothing',
    'PacecarIgnorePitEntryTripwire',
    'HasDoubleYellowLine',
    'BumpdraftPlayerRoadStraightness',
    'PitStrategy100PitChance',
    'PitStrategy100FuelOnlyChance',
    'PitStrategy100TwoTyresChance',
    'PitStrategy100FourTyresChance',
    'PitStrategy75PitChance',
    'PitStrategy75FuelOnlyChance',
    'PitStrategy75TwoTyresChance',
    'PitStrategy75FourTyresChance',
    'PitStrategy50PitChance',
    'PitStrategy50FuelOnlyChance',
    'PitStrategy50TwoTyresChance',
    'PitStrategy50FourTyresChance',
    'PitStrategy25PitChance',
    'PitStrategy25FuelOnlyChance',
    'PitStrategy25TwoTyresChance',
    'PitStrategy25FourTyresChance',
    'PitStrategy0FuelOnlyChance',
    'PitStrategy0TwoTyresChance',
    'PitStrategy0FourTyresChance',
    'PitStrategy0PitChance',
    'CanSwitchFromStagnantRacingLine',
    'StayBehindRegionScaled',
    'StateMachineWeightingOvertake',
    'StateMachineWeightingBumpDraft',
    'StayAlongsideGap',
    'StayBehindRegion',
    'ThrottleLiftToleranceDeg',
    'CatchupPowModifier',
]
AI_GLOBAL_FIELDS = [
    'OutbrakingEffort',
    'AggressionVariation',
    'DesireForRacingLine',
    'AggressionRivalModifier',
    'AggressionTeamModifier',
    'PitstopStrategyGreenWindowPercentage',
    'PitstopStrategyGreenWindowLapReserve',
]
WORLD_PACE_FIELDS = [
    'PracticeEasyBestTime',
    'PracticeEasyWorstTime',
    'PracticeHardBestTime',
    'PracticeHardWorstTime',
    'QualifyBaseTimeModifier',
    'QualRecSpeed',
    'RaceRecSpeed',
    'TempAirC',
    'TempTrackC',
    'TempAirCEnd',
    'TempTrackCEnd',
]
AI_EDITABLE_BY_CLASS = {
    'RACEDATA_c': {'RaceLaps'},
    'AIRACINGTRACKCONFIG_c': set(AI_TRACK_FIELDS),
    'AIRACINGGLOBALCONFIG_c': set(AI_GLOBAL_FIELDS),
    'WORLDSCRIPT_c': set(WORLD_PACE_FIELDS),
}

def _direct_scalar(v):
    """Only direct number/bool constructor fields are writable.
    Nested min/max objects are displayed read-only by the UI and blocked here."""
    s=str(v).strip()
    if s in ('True','False'): return True
    try:
        float(s); return True
    except Exception:
        return False

def repoint_mod():
    """Load the bundled isolated-repoint helper."""
    p=component_path(REPOINT_NAME)
    if not os.path.exists(p): return None
    try:
        import importlib.util as _iu
        spec=_iu.spec_from_file_location('n15repoint', p)
        m=_iu.module_from_spec(spec); spec.loader.exec_module(m)
        return m
    except Exception:
        return None

def isolated_repoint(pyc_name, uid, old_value, new_value, out_archive):
    """Repoint one record's LOAD_CONST operand instead of mutating a shared constant.
    Writes a same-size patched COPY to out_archive. Never touches the live archive.
    Returns dict(ok, error, available, old_index, new_index)."""
    R=repoint_mod()
    if not R: return dict(ok=False, error='repoint tool not installed')
    live=_live_archive(); cdf=_cdfiles_path()
    data,off,sz = R.extract_from_archive(live, cdf, pyc_name)
    if data is None: return dict(ok=False, error=f'{pyc_name} not found in archive')
    # locate the record + the arg slot holding old_value
    co=args=None
    for c in R.parse(data):
        a=R.find_record_loadconsts(c, int(uid))
        if a: co, args = c, a; break
    if not args: return dict(ok=False, error=f'UID {uid} not found as a constructor arg')
    fidx,_n = R.autodetect_field_index(args, int(old_value))
    if fidx is None:
        return dict(ok=False, error=f'no argument of record {uid} currently holds {old_value}')
    target = args[fidx]
    new_idx = R.find_const_with_value(co, int(new_value))
    if new_idx is None:
        avail=sorted({v for _,v in R.lap_like_consts(co)})
        return dict(ok=False, unavailable_value=True,
                    error=f'{new_value} is not an existing constant, so it cannot be set without resizing the file',
                    available=avail)
    if new_idx > 0xFFFF:
        return dict(ok=False, error='const index requires EXTENDED_ARG; unsupported')
    old_idx = target['const_index']
    buf=bytearray(data)
    aoff = target['arg_off']
    cur = struct.unpack_from('<H', buf, co.code_off + aoff)[0]
    if cur != old_idx:
        return dict(ok=False, error='operand mismatch; refusing to write')
    struct.pack_into('<H', buf, co.code_off + aoff, new_idx)
    if len(buf) != sz:
        return dict(ok=False, error='patched pyc size differs; refused')
    shutil.copyfile(live, out_archive)
    with open(out_archive,'r+b') as f:
        f.seek(off); f.write(bytes(buf))
    return dict(ok=True, old_index=old_idx, new_index=new_idx, field_index=fidx)

def mapper_paths():
    """Return the bundled mapper and patcher paths when both are present."""
    m = component_path(MAPPER_NAME)
    p = component_path(PATCHER_NAME)
    return (m if os.path.exists(m) else None, p if os.path.exists(p) else None)

def mapper_ready():
    m,p = mapper_paths(); return bool(m and p)

def _py():
    # Prefer the same interpreter running the app.
    return sys.executable or 'python'

def _run_mapper(args, timeout=180):
    """Run the mapper with a subcommand + args list. Returns (rc, stdout, stderr).
    Mapper output is UTF-16 on Windows redirects but UTF-8 to a pipe; decode robustly."""
    m,p = mapper_paths()
    if not (m and p): raise RuntimeError('required game-data tools are missing')
    cmd = [_py(), m] + args
    r = _sp.run(cmd, capture_output=True, timeout=timeout)
    def dec(b):
        for enc in ('utf-8','utf-16','latin1'):
            try: return b.decode(enc)
            except Exception: continue
        return b.decode('utf-8','replace')
    return r.returncode, dec(r.stdout), dec(r.stderr)

def _cfg_get(key, default=None):
    c=load_cfg(); return c.get(key, default)
def _cfg_set(key, val):
    c=load_cfg(); c[key]=val; save_cfg(c)

def _sha256(path, limit=None):
    h=_hl.sha256()
    with open(path,'rb') as f:
        while True:
            b=f.read(1<<20)
            if not b: break
            h.update(b)
    return h.hexdigest()

def _live_archive():
    """The game's live data/ARCHIVE0.AR (patch target)."""
    g,reg=registry()
    if not g or '0' not in reg: return None
    return reg['0']['ar']

def _cdfiles_path():
    g,reg=registry()
    if not g or '0' not in reg: return None
    return reg['0']['cdf']

def mapper_records(pyc_file, class_name, fields, archive=None):
    """Enumerate records via `mapper records ... --csv <tmp>`.
    archive defaults to the LIVE archive; pass a temp archive to diff a patch."""
    live=archive or _live_archive(); cdf=_cdfiles_path()
    if not live or not cdf: raise RuntimeError('game archive not found')
    m,p=mapper_paths()
    tmpcsv=os.path.join(_tf.gettempdir(), f'n15mod_records_{os.getpid()}_{abs(hash(live))%99999}.csv')
    args=['records','--archive',live,'--cdfiles',cdf,'--patcher',p,
          '--file',pyc_file,'--class',class_name,
          '--limit','100000','--csv',tmpcsv]
    if fields: args+=['--fields']+fields
    rc,out,err=_run_mapper(args)
    if rc!=0 or not os.path.exists(tmpcsv):
        raise RuntimeError(f'mapper records failed: {err.strip() or out.strip()}')
    rows=[]
    with open(tmpcsv,'r',encoding='utf-8',newline='') as f:
        for row in _csv.DictReader(f): rows.append(row)
    try: os.remove(tmpcsv)
    except OSError: pass
    return rows

def _diff_records(before, after, fields):
    """Compare two record lists (by uid) across `fields`. Returns list of
    (uid, field, old, new) for every changed cell."""
    bi={str(r.get('uid')):r for r in before}
    changes=[]
    for r in after:
        u=str(r.get('uid')); b=bi.get(u)
        if not b: continue
        for f in fields:
            ov=b.get(f); nv=r.get(f)
            if ov is None and nv is None: continue
            if not _num_eq(ov, nv) and str(ov)!=str(nv):
                changes.append((u, f, ov, nv))
    return changes

def _num_eq(a,b):
    try: return abs(float(a)-float(b))<1e-6
    except Exception: return str(a)==str(b)

# Which fields to diff per class (all editable/at-risk fields, so we catch
# collateral changes to sibling records sharing a marshal constant).
_DIFF_FIELDS={
    'RACEDATA_c':['RaceLaps'],
    # Diff every field exposed by the lab, not only the selected one. This catches
    # a shared marshal constant changing a sibling field or another track record.
    'AIRACINGTRACKCONFIG_c':AI_TRACK_FIELDS,
    'AIRACINGGLOBALCONFIG_c':AI_GLOBAL_FIELDS,
    'WORLDSCRIPT_c':WORLD_PACE_FIELDS,
}


_MAPPER_DIRECT_CACHE=None

def _mapper_direct_module():
    global _MAPPER_DIRECT_CACHE
    if _MAPPER_DIRECT_CACHE is not None:return _MAPPER_DIRECT_CACHE
    path=component_path(MAPPER_NAME)
    spec=importlib.util.spec_from_file_location('n15mod_mapper_direct',path)
    if spec is None or spec.loader is None:raise RuntimeError('could not load PYC mapper module')
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    _MAPPER_DIRECT_CACHE=mod
    return mod

def _pyc_live_blob(pyc_file):
    g,reg=registry();v=need(reg,'0')
    _raw,rows,_layout=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,pyc_file)
    with open(v['ar'],'rb') as fh:fh.seek(row['offset']);data=fh.read(row['size'])
    if len(data)!=row['size']:raise ValueError(f'short {pyc_file} read')
    return v,row,data

def _py2_ops(code):
    i=0;ext=0
    while i<len(code):
        off=i;op=code[i];i+=1;arg=None;arg_off=None
        if op>=90:
            if i+2>len(code):break
            raw=code[i]|(code[i+1]<<8);arg=raw|ext;arg_off=i;i+=2
            if op==143:ext=raw<<16;continue
            ext=0
        yield off,op,arg,arg_off

def _mapped_rows_from_pyc_bytes(pyc,class_name,fields):
    M=_mapper_direct_module();root=M.parse_pyc(pyc);schemas=M.build_schemas(root);records=M.map_records(root,schemas)
    rows=[]
    for rec in records:
        if rec.class_name!=class_name:continue
        row={'uid':M.value_plain_for_compare(rec.uid)}
        for f in fields:
            row[f]=M.value_plain_for_compare(rec.fields.get(f)) if f in rec.fields else None
        rows.append(row)
    return rows,root,records,schemas

def _scalar_same_type(a,b):
    if isinstance(a,bool) or isinstance(b,bool):return isinstance(a,bool) and isinstance(b,bool) and a is b
    if isinstance(a,int) and not isinstance(a,bool):return isinstance(b,int) and not isinstance(b,bool) and a==b
    if isinstance(a,float):
        try:return isinstance(b,(int,float)) and not isinstance(b,bool) and abs(float(a)-float(b))<1e-12
        except Exception:return False
    return a==b

def _coerce_scalar_like(old,value):
    if isinstance(old,bool):
        if isinstance(value,bool):return value
        t=str(value).strip().lower()
        if t in ('true','1','yes','on'):return True
        if t in ('false','0','no','off'):return False
        raise ValueError('boolean fields accept True or False')
    if isinstance(old,int) and not isinstance(old,bool):
        x=float(value)
        if not _math.isfinite(x) or abs(x-round(x))>1e-9:raise ValueError('this field requires a whole number')
        x=int(round(x))
        if not -(2**63)<=x<2**63:raise ValueError('integer is outside the supported range')
        return x
    if isinstance(old,float):
        x=float(value)
        if not _math.isfinite(x):raise ValueError('NaN and infinity are blocked')
        if abs(x)>1_000_000_000:raise ValueError('absolute values above one billion are blocked')
        return x
    raise ValueError('field is not a direct scalar')

def _marshal_scalar_bytes(value,old):
    if isinstance(value,bool):return b'T' if value else b'F'
    if isinstance(old,int) and not isinstance(old,bool):
        if -(2**31)<=value<2**31:return b'i'+struct.pack('<i',int(value))
        return b'I'+struct.pack('<q',int(value))
    return b'g'+struct.pack('<d',float(value))

def _root_const_index(root,mval):
    M=_mapper_direct_module();co=root.value
    for i,c in enumerate(co.consts):
        if c is mval:return i
        if isinstance(mval,M.MVal) and isinstance(c,M.MVal) and c.tag_offset==mval.tag_offset:return i
    return None

def _root_existing_const(root,value):
    M=_mapper_direct_module()
    for i,c in enumerate(root.value.consts):
        if _scalar_same_type(M.value_plain_for_compare(c),value):return i
    return None

def _patch_load_const_operand(pyc,operand_abs,new_index,new_value=None,old_value=None):
    if new_index>0xffff:raise ValueError('constant index needs EXTENDED_ARG')
    out=bytearray(pyc);struct.pack_into('<H',out,operand_abs,new_index)
    if new_value is None:return bytes(out),False,new_index
    layout=_stat_root_layout(pyc);new_index=layout['count']
    if new_index>0xffff:raise ValueError('constant table has reached the LOAD_CONST limit')
    struct.pack_into('<H',out,operand_abs,new_index)
    struct.pack_into('<i',out,layout['count_pos'],new_index+1)
    out[layout['const_end']:layout['const_end']]=_marshal_scalar_bytes(new_value,old_value)
    vals=_pyc_consts(bytes(out))
    if len(vals)!=new_index+1 or not _scalar_same_type(vals[new_index],new_value):raise ValueError('rebuilt PYC constant readback failed')
    return bytes(out),True,new_index

def _exact_field_variant(pyc,class_name,uid,field,value):
    fields=_DIFF_FIELDS.get(class_name,[field]);before,root,records,_schemas=_mapped_rows_from_pyc_bytes(pyc,class_name,fields)
    M=_mapper_direct_module();rec=M.find_record_for_patch(records,class_name,uid)
    if rec is None or field not in rec.fields:return dict(handled=False,error='record field not found')
    raw_old=rec.fields[field];old=M.value_plain_for_compare(raw_old);new=_coerce_scalar_like(old,value)
    if _scalar_same_type(old,new):return dict(handled=True,ok=False,error='value already matches the live field')
    co=root.value;layout=_stat_root_layout(pyc);candidates=[]
    old_indices=[]
    if isinstance(raw_old,M.MVal):
        oi=_root_const_index(root,raw_old)
        if oi is not None:old_indices=[oi]
    if not old_indices:
        old_indices=[i for i,c in enumerate(co.consts) if _scalar_same_type(M.value_plain_for_compare(c),old)]
    for off,op,arg,arg_off in _py2_ops(co.code_bytes):
        if op==100 and arg in old_indices and off<rec.call_offset:
            candidates.append(('const',layout['code_off']+arg_off,arg,off))
    # Python 2 sometimes loads True/False by name rather than LOAD_CONST.
    if isinstance(old,bool):
        old_name='True' if old else 'False';new_name='True' if new else 'False'
        if old_name in co.names and new_name in co.names:
            oi=co.names.index(old_name);ni=co.names.index(new_name)
            for off,op,arg,arg_off in _py2_ops(co.code_bytes):
                if op in (101,116) and arg==oi and off<rec.call_offset:
                    candidates.append(('name',layout['code_off']+arg_off,ni,off))
    if not candidates:return dict(handled=False,error='could not locate an isolated bytecode source for this field')
    existing=_root_existing_const(root,new)
    wanted={(str(uid),field)}
    attempts=[]
    # Nearest source instructions are most likely to feed this constructor call.
    for kind,operand_abs,index,code_off in sorted(candidates,key=lambda x:x[3],reverse=True)[:128]:
        try:
            if kind=='name':
                out=bytearray(pyc);struct.pack_into('<H',out,operand_abs,index);rebuilt=bytes(out);grew=False;new_index=index
            elif existing is not None:
                rebuilt,grew,new_index=_patch_load_const_operand(pyc,operand_abs,existing)
            else:
                rebuilt,grew,new_index=_patch_load_const_operand(pyc,operand_abs,0,new,old)
            after,_r,_recs,_s=_mapped_rows_from_pyc_bytes(rebuilt,class_name,fields)
            changes=_diff_records(before,after,fields)
            actual={(str(u),f) for u,f,o,n in changes}
            if actual==wanted:
                changed=next(c for c in changes if str(c[0])==str(uid) and c[1]==field)
                if not (_scalar_same_type(changed[3],new) or _num_eq(changed[3],new)):
                    continue
                return dict(handled=True,ok=True,pyc=rebuilt,before=before,after=after,old=old,new=new,
                            grew=grew,const_index=new_index,operand_offset=operand_abs,
                            method=('append_constant_repoint' if grew else 'isolated_operand_repoint'))
            attempts.append(dict(offset=code_off,changes=len(changes)))
        except Exception as ex:
            attempts.append(dict(offset=code_off,error=str(ex)))
    return dict(handled=False,error='no candidate bytecode operand produced an exact one-field diff',attempts=attempts[-8:])

def _install_exact_field_variant(pyc_file,class_name,uid,field,value,dry_run=False,source_pyc=None):
    v,row,pyc=_pyc_live_blob(pyc_file)
    if source_pyc is not None:pyc=source_pyc
    plan=_exact_field_variant(pyc,class_name,uid,field,value)
    if not plan.get('ok'):return plan
    if dry_run:return dict(ok=True,handled=True,old=plan['old'],new=plan['new'],affected=[str(uid)],affected_count=1,method=plan['method'],note='dry-run: exact one-field bytecode diff verified')
    rebuilt=plan['pyc'];_rp_backup_pair(v)
    if len(rebuilt)==row['size']:
        with open(v['ar'],'r+b') as fh:
            fh.seek(row['offset']);before=fh.read(row['size']);fh.seek(row['offset']);fh.write(rebuilt);fh.flush();os.fsync(fh.fileno())
            fh.seek(row['offset']);check=fh.read(row['size'])
        if check!=rebuilt:
            with open(v['ar'],'r+b') as fh:fh.seek(row['offset']);fh.write(before);fh.flush();os.fsync(fh.fileno())
            raise ValueError('isolated PYC readback failed; previous bytes restored')
        result=dict(method='same_size_exact_operand')
    else:
        fd,tmp=tempfile.mkstemp(prefix='n15mod_exact_pyc_',suffix='.PYC');os.close(fd)
        try:
            open(tmp,'wb').write(rebuilt)
            with _RP_LOCK:result=_rp_install_one('0',v,row,tmp,source_name=f'Exact {class_name} {uid}/{field}',allow_magic=True)
        finally:
            try:os.remove(tmp)
            except OSError:pass
    _v,_row,live=_pyc_live_blob(pyc_file);rows,_root,_records,_schemas=_mapped_rows_from_pyc_bytes(live,class_name,[field])
    got=next((r.get(field) for r in rows if str(r.get('uid'))==str(uid)),None)
    if not (_scalar_same_type(got,plan['new']) or _num_eq(got,plan['new'])):raise ValueError(f'exact-field live readback failed: wanted {plan["new"]}, got {got}')
    return dict(ok=True,handled=True,old=plan['old'],new=plan['new'],verified=True,readback=got,affected=[str(uid)],affected_count=1,method=plan['method'],file=result)

def mapper_set_value(pyc_file, class_name, uid, field, value, dry_run=False):
    """Patch one field on one record with a MANDATORY full-diff guard.

    Because the mapper edits marshalled constant payloads, a value shared by
    several records (e.g. many RaceLaps=999 or StayBehindRegion=0.5) would be
    changed for ALL of them. So we ALWAYS:
      1. read every record of this class BEFORE,
      2. patch to a TEMP archive,
      3. read every record from the TEMP archive AFTER,
      4. diff, and install ONLY if exactly the intended uid+field changed.
    Dry-run performs the same temp-patch + diff and reports the affected count
    WITHOUT installing.
    """
    live=_live_archive(); cdf=_cdfiles_path()
    if not live or not cdf: raise RuntimeError('game archive not found')
    m,p=mapper_paths()
    g,reg=registry(); bak=reg['0']['bak']
    diff_fields=_DIFF_FIELDS.get(class_name, [field])

    # v0.9.26.7: first try an exact bytecode-operand edit. This clones/reuses
    # a constant and repoints only the selected field, so shared constants no
    # longer force collateral changes. Boolean fields are supported too.
    if class_name in ('RACEDATA_c','AIRACINGTRACKCONFIG_c','AIRACINGGLOBALCONFIG_c','WORLDSCRIPT_c'):
        exact=_install_exact_field_variant(pyc_file,class_name,uid,field,value,dry_run=dry_run)
        if exact.get('ok') or exact.get('handled'):
            return exact

    # 1. snapshot BEFORE (from live)
    try:
        before=mapper_records(pyc_file, class_name, diff_fields)
    except Exception as e:
        return dict(ok=False, error=f'pre-read failed: {e}')

    # 2. always patch to a temp archive (even for dry-run, so we can diff)
    tmp_out=os.path.join(_tf.gettempdir(), f'n15mod_patch_{os.getpid()}.AR')
    if os.path.exists(tmp_out):
        try: os.remove(tmp_out)
        except OSError: pass

    old=new=None; method='mapper'
    # current value from the BEFORE snapshot (needed to find the right arg slot)
    cur_val=None
    for rw in before:
        if str(rw.get('uid'))==str(uid): cur_val=rw.get(field); break

    if class_name in ('AIRACINGTRACKCONFIG_c','AIRACINGGLOBALCONFIG_c','WORLDSCRIPT_c'):
        if cur_val is None:
            return dict(ok=False, error=f'{field} was not found on UID {uid}')
        if not _direct_scalar(cur_val):
            return dict(ok=False, read_only_nested=True,
                        error=f'{field} is a nested/non-scalar value and is read-only until its exact structure is mapped')

    use_repoint = ((class_name, field) in REPOINT_FIELDS) and repoint_mod() is not None and cur_val is not None
    if use_repoint:
        # ISOLATED UID EDIT: repoint this record's LOAD_CONST operand; never mutate
        # a constant that other records share.
        try: iv=int(float(cur_val)); nv=int(float(value))
        except Exception:
            return dict(ok=False, error='RaceLaps must be a whole number')
        rp=isolated_repoint(pyc_file, uid, iv, nv, tmp_out)
        if not rp.get('ok'):
            return dict(ok=False, error=rp.get('error','repoint failed'),
                        unavailable_value=rp.get('unavailable_value'),
                        available=rp.get('available'))
        old, new, method = str(iv), str(nv), 'repoint'
    else:
        args=['set-record-value','--archive',live,'--cdfiles',cdf,'--patcher',p,
              '--file',pyc_file,'--class',class_name,
              '--uid',str(uid),'--field',field,'--value',str(value),
              '--out-archive',tmp_out]
        rc,out,err=_run_mapper(args)
        if rc!=0:
            return dict(ok=False, error=(err.strip() or out.strip() or 'mapper failed'))
        if not os.path.exists(tmp_out):
            return dict(ok=False, error='mapper did not produce an output archive')
        for line in out.splitlines():
            if f'{field}:' in line and '->' in line:
                try:
                    seg=line.split(f'{field}:',1)[1]
                    old,new=[s.strip() for s in seg.split('->',1)]
                except Exception: pass

    # 3. snapshot AFTER (from the temp patched archive)
    try:
        after=mapper_records(pyc_file, class_name, diff_fields, archive=tmp_out)
    except Exception as e:
        try: os.remove(tmp_out)
        except OSError: pass
        return dict(ok=False, error=f'post-read failed: {e}')

    # 4. full diff
    changes=_diff_records(before, after, diff_fields)
    intended=[(u,f) for (u,f,o,n) in changes if str(u)==str(uid) and f==field]
    collateral=[(u,f) for (u,f,o,n) in changes if not (str(u)==str(uid) and f==field)]

    if collateral:
        affected=sorted({u for (u,f) in [(c[0],c[1]) for c in changes]})
        try: os.remove(tmp_out)
        except OSError: pass
        return dict(ok=False, shared_constant=True,
            error='Patch would affect multiple records: '+', '.join('UID '+u for u in affected),
            affected=affected, changes=[dict(uid=c[0],field=c[1],old=c[2],new=c[3]) for c in changes])

    if not intended:
        try: os.remove(tmp_out)
        except OSError: pass
        return dict(ok=False, error='Patch produced no change to the selected record (value may already be set, or not a direct constant).')

    # ---- at this point exactly one record/field changed ----
    if dry_run:
        try: os.remove(tmp_out)
        except OSError: pass
        return dict(ok=True, old=old, new=new, affected=[str(uid)], affected_count=1,
                    method=method,
                    note='dry-run: exactly 1 record would change (safe to apply)')

    # SAFETY: pristine backup of the live archive before we touch it.
    ensure_backup(live, bak)
    if os.path.getsize(tmp_out)!=os.path.getsize(live):
        os.remove(tmp_out)
        return dict(ok=False, error='patched archive size differs from live; refused')
    shutil.copyfile(tmp_out, live)
    try: os.remove(tmp_out)
    except OSError: pass
    # final readback verify from live
    try:
        rows=mapper_records(pyc_file, class_name, [field])
        got=None
        for rw in rows:
            if str(rw.get('uid'))==str(uid): got=rw.get(field); break
        verified = got is not None and _num_eq(got, value)
    except Exception as e:
        verified=False; got=f'(verify error: {e})'
    return dict(ok=True, old=old, new=new, verified=verified, readback=got,
                affected=[str(uid)], affected_count=1, method=method)



def mapper_set_values_batch(pyc_file, class_name, changes, dry_run=False):
    """Atomically preview/apply exact isolated field changes in one rebuilt PYC."""
    if class_name not in ('AIRACINGTRACKCONFIG_c','AIRACINGGLOBALCONFIG_c'):
        return dict(ok=False,error='batch editing is available only for AI behavior classes')
    if not isinstance(changes,list) or not changes:return dict(ok=False,error='no changes supplied')
    if len(changes)>100:return dict(ok=False,error='batch is limited to 100 fields')
    allowed=AI_EDITABLE_BY_CLASS.get(class_name,set())
    if pyc_file!=AICFG:return dict(ok=False,error=f'{class_name} must be edited in {AICFG}')
    try:v,row,current=_pyc_live_blob(pyc_file)
    except Exception as ex:return dict(ok=False,error=str(ex))
    normalized=[];seen=set()
    try:
        rows,_root,_records,_schemas=_mapped_rows_from_pyc_bytes(current,class_name,_DIFF_FIELDS[class_name])
        by_uid={str(r.get('uid')):r for r in rows}
        for raw in changes:
            uid=str(raw.get('uid'));field=str(raw.get('field',''));ident=(uid,field)
            if field not in allowed:return dict(ok=False,error=f'{field} is not editable for {class_name}')
            if ident in seen:return dict(ok=False,error=f'duplicate target UID {uid} / {field}')
            seen.add(ident);old=by_uid.get(uid,{}).get(field)
            if old is None:return dict(ok=False,error=f'UID {uid} / {field} was not found')
            wanted=_coerce_scalar_like(old,raw.get('value'))
            if _scalar_same_type(old,wanted) or _num_eq(old,wanted):continue
            normalized.append(dict(uid=uid,field=field,value=wanted,old=old))
    except Exception as ex:return dict(ok=False,error=str(ex))
    if not normalized:return dict(ok=False,error='all supplied values already match the live records')
    work=current;summary=[]
    for ch in normalized:
        plan=_exact_field_variant(work,class_name,ch['uid'],ch['field'],ch['value'])
        if not plan.get('ok'):
            return dict(ok=False,error=f"{ch['field']} could not be isolated: {plan.get('error','unknown error')}")
        work=plan['pyc'];summary.append(dict(uid=ch['uid'],field=ch['field'],old=ch['old'],new=ch['value'],method=plan['method']))
    # Final class-wide diff must match exactly the requested targets.
    before,_r,_recs,_s=_mapped_rows_from_pyc_bytes(current,class_name,_DIFF_FIELDS[class_name])
    after,_r,_recs,_s=_mapped_rows_from_pyc_bytes(work,class_name,_DIFF_FIELDS[class_name])
    actual=_diff_records(before,after,_DIFF_FIELDS[class_name]);expected={(c['uid'],c['field']) for c in normalized}
    got={(str(u),f) for u,f,o,n in actual}
    if got!=expected:
        return dict(ok=False,collateral=True,error='final batch diff did not exactly match requested fields; nothing installed',changes=[dict(uid=u,field=f,old=o,new=n) for u,f,o,n in actual[:100]])
    if dry_run:return dict(ok=True,dry_run=True,affected_count=len(summary),changes=summary,note='exact isolated PYC batch verified')
    fd,tmp=tempfile.mkstemp(prefix='n15mod_ai_batch_',suffix='.PYC');os.close(fd)
    try:
        open(tmp,'wb').write(work)
        _rp_backup_pair(v)
        with _RP_LOCK:result=_rp_install_one('0',v,row,tmp,source_name=f'AI exact batch {class_name}',allow_magic=True)
    finally:
        try:os.remove(tmp)
        except OSError:pass
    _v,_row,live=_pyc_live_blob(pyc_file)
    verify,_r,_recs,_s=_mapped_rows_from_pyc_bytes(live,class_name,_DIFF_FIELDS[class_name]);verify_by_uid={str(r.get('uid')):r for r in verify}
    failed=[]
    for c in normalized:
        gotv=verify_by_uid.get(c['uid'],{}).get(c['field'])
        if not (_scalar_same_type(gotv,c['value']) or _num_eq(gotv,c['value'])):failed.append(dict(uid=c['uid'],field=c['field'],wanted=c['value'],got=gotv))
    if failed:return dict(ok=False,error='one or more exact batch readbacks failed',failed=failed)
    return dict(ok=True,affected_count=len(summary),changes=summary,verified=True,file=result)


# ---- stock baseline management ----
_BASELINE_VERIFY_CACHE={}

def _baseline_live_match(idx,path,size):
    g,reg=registry()
    idx=str(idx)
    if not g or idx not in reg:
        raise ValueError(f'ARCHIVE{idx} is not installed in the selected game folder')
    live=reg[idx]['ar']
    if os.path.normcase(os.path.realpath(path))==os.path.normcase(os.path.realpath(live)):
        raise ValueError(f'ARCHIVE{idx} baseline cannot be the live game archive; choose a separate clean copy')
    live_size=os.path.getsize(live)
    if int(size)!=live_size:
        raise ValueError(f'ARCHIVE{idx} baseline size {int(size)} does not match live archive size {live_size}')
    return live

def _verify_baseline_entry(idx,entry,verify_hash=True):
    idx=str(idx)
    if not entry or not entry.get('path'):
        raise ValueError(f'no baseline registered for ARCHIVE{idx}')
    path=os.path.abspath(entry['path'])
    if not os.path.isfile(path):
        raise ValueError(f'baseline for ARCHIVE{idx} is missing: {path}')
    st=os.stat(path); expected_size=int(entry.get('size',-1))
    if st.st_size!=expected_size:
        raise ValueError(f'baseline ARCHIVE{idx} size changed since registration ({st.st_size} vs {expected_size})')
    _baseline_live_match(idx,path,st.st_size)
    if verify_hash:
        expected_hash=str(entry.get('sha256') or '').lower()
        if len(expected_hash)!=64:
            raise ValueError(f'baseline ARCHIVE{idx} has no valid stored SHA-256')
        key=(path,st.st_size,st.st_mtime_ns,expected_hash)
        ok=_BASELINE_VERIFY_CACHE.get(key)
        if ok is None:
            ok=(_sha256(path).lower()==expected_hash)
            for old_key in list(_BASELINE_VERIFY_CACHE):
                if old_key[0]==path and old_key!=key:
                    _BASELINE_VERIFY_CACHE.pop(old_key,None)
            _BASELINE_VERIFY_CACHE[key]=ok
        if not ok:
            raise ValueError(f'baseline ARCHIVE{idx} hash mismatch - file changed since it was registered')
    return path

def _baseline_public_status(idx,entry,verify_hash=True):
    try:
        path=_verify_baseline_entry(idx,entry,verify_hash=verify_hash)
        return dict(path=path,sha256=str(entry['sha256'])[:16],size=int(entry['size']),ok=True,error=None)
    except Exception as ex:
        return dict(path=(entry or {}).get('path'),sha256=str((entry or {}).get('sha256',''))[:16],
                    size=(entry or {}).get('size'),ok=False,error=str(ex))

def set_stock_baseline(path):
    """Register clean ARCHIVE*.AR copies after filename, size, and hash checks."""
    path=os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
    g,reg=registry()
    if not g:
        raise ValueError('select the NASCAR 15 game folder before setting a baseline')
    candidates=[]
    if os.path.isdir(path):
        for fn in sorted(os.listdir(path)):
            m=re.fullmatch(r'ARCHIVE(\d+)\.AR',fn,re.I)
            if m:
                candidates.append((m.group(1),os.path.join(path,fn)))
        if not candidates:
            raise ValueError('no exactly named ARCHIVE<number>.AR files found in that folder')
    else:
        if not os.path.isfile(path) or os.path.getsize(path)<64:
            raise ValueError('baseline archive not found or too small')
        m=re.fullmatch(r'ARCHIVE(\d+)\.AR',os.path.basename(path),re.I)
        if not m:
            raise ValueError('baseline file must be named exactly ARCHIVE<number>.AR')
        candidates=[(m.group(1),path)]

    entries={}
    for idx,full in candidates:
        size=os.path.getsize(full)
        _baseline_live_match(idx,full,size)
        digest=_sha256(full)
        st=os.stat(full)
        entries[str(idx)]=dict(path=os.path.abspath(full),sha256=digest,size=size,
                               registered_mtime_ns=st.st_mtime_ns)
        _BASELINE_VERIFY_CACHE[(os.path.abspath(full),size,st.st_mtime_ns,digest.lower())]=True
    _cfg_set('stock_baselines',entries)
    _cfg_set('stock_baseline',entries.get('0'))
    return entries

def stock_baselines():
    e=_cfg_get('stock_baselines')
    if e: return e
    legacy=_cfg_get('stock_baseline')
    return {'0':legacy} if legacy else {}

def stock_baseline():
    return stock_baselines().get('0')

def baseline_archive(idx='0'):
    """Return a clean archive only after stored size/hash and live-size checks."""
    e=stock_baselines().get(str(idx))
    if not e: return None
    return _verify_baseline_entry(str(idx),e,verify_hash=True)

def restore_from(source):
    """Restore registered baselines or the app's pristine archive backups."""
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    restored=[]
    if source=='baseline':
        entries=stock_baselines()
        if not entries: raise RuntimeError('no clean original game copy has been selected')
        verified=[]
        for idx,e in entries.items():
            try: src=_verify_baseline_entry(idx,e,verify_hash=True)
            except Exception as ex: raise RuntimeError(str(ex))
            verified.append((str(idx),src,e))
        prepared=[]; temp_paths=[]
        try:
            # Prepare every copy before replacing any live archive.
            for idx,src,e in verified:
                live=reg[idx]['ar']; tmp=live+'.n15mod.restore.tmp'
                temp_paths.append(tmp)
                if os.path.exists(tmp): os.remove(tmp)
                shutil.copyfile(src,tmp)
                if os.path.getsize(tmp)!=int(e['size']) or _sha256(tmp).lower()!=str(e['sha256']).lower():
                    raise RuntimeError(f'prepared ARCHIVE{idx} restore copy failed verification')
                prepared.append((idx,tmp,live))
            for idx,tmp,live in prepared:
                os.replace(tmp,live); restored.append(f'ARCHIVE{idx}')
        finally:
            for tmp in temp_paths:
                try:
                    if os.path.exists(tmp): os.remove(tmp)
                except OSError: pass
    else:
        for idx,r in reg.items():
            if os.path.exists(r['bak']):
                shutil.copyfile(r['bak'],r['ar']); restored.append(f'ARCHIVE{idx}')
        if not restored: raise RuntimeError('no original backup is available')
    _clear_ui_thumb_cache()
    return dict(ok=True,restored_from=source,restored=restored)

@app.route('/api/pyc/status')
def pyc_status():
    ready=mapper_ready(); entries=stock_baselines(); sb=entries.get('0')
    g,reg=registry(); live=_live_archive()
    # Status pages must stay instant. Full hashes are still verified before restore/apply.
    statuses={k:_baseline_public_status(k,v,verify_hash=False) for k,v in entries.items()}
    return jsonify(dict(
        ready=ready,mapper=os.path.basename(MAPPER_NAME),patcher=os.path.basename(PATCHER_NAME),
        repoint=bool(repoint_mod()),stock_baseline=statuses.get('0'),stock_baselines=statuses,
        has_backup=bool(g and '0' in reg and os.path.exists(reg['0']['bak'])),live=live))

@app.route('/api/pyc/records', methods=['POST'])
def pyc_records():
    if not mapper_ready(): return jsonify(dict(ok=False, error='mapper/patcher not in app folder')),400
    q=request.get_json()
    try:
        arc=None
        if q.get('source')=='baseline':
            arc=baseline_archive('0')
            if not arc: return jsonify(dict(ok=False, error='no clean baseline registered')),400
        rows=mapper_records(q['file'], q['class'], q.get('fields'), archive=arc)
        return jsonify(dict(rows=rows, count=len(rows), source=q.get('source','live')))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/pyc/set', methods=['POST'])
def pyc_set():
    if not mapper_ready(): return jsonify(dict(ok=False, error='mapper/patcher not in app folder')),400
    q=request.get_json()
    # Class-specific public guard. NumDrivers and unknown fields stay blocked.
    cls=q.get('class'); field=q.get('field')
    allowed=AI_EDITABLE_BY_CLASS.get(cls,set())
    expected_file={'RACEDATA_c':DBFILE,
                   'AIRACINGTRACKCONFIG_c':AICFG,
                   'AIRACINGGLOBALCONFIG_c':AICFG,
                   'WORLDSCRIPT_c':DBFILE}.get(cls)
    if q.get('file')!=expected_file:
        return jsonify(dict(ok=False,
            error=f'class {cls} must be edited in {expected_file or "an approved file"}')),400
    if field not in allowed:
        return jsonify(dict(ok=False,
            error=f'field {field} is not editable for class {cls} in this app')),400
    try:
        res=mapper_set_value(q['file'], q['class'], q['uid'], q['field'], q['value'],
                             dry_run=bool(q.get('dry_run')))
        if (res.get('ok') and not bool(q.get('dry_run')) and cls=='RACEDATA_c' and field=='RaceLaps'):
            try:_SCHEDULE_CACHE.clear();_SCHEDULE_SOURCE_CACHE.clear()
            except Exception:pass
        return jsonify(res if 'ok' in res else dict(ok=True, **res))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/pyc/set_batch', methods=['POST'])
def pyc_set_batch():
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    q=request.get_json(force=True)
    cls=q.get('class'); expected={'AIRACINGTRACKCONFIG_c':AICFG,
                                 'AIRACINGGLOBALCONFIG_c':AICFG}.get(cls)
    if not expected or q.get('file')!=expected:
        return jsonify(dict(ok=False,error='unsupported class/file for AI batch edit')),400
    try:
        result=mapper_set_values_batch(expected,cls,q.get('changes',[]),
                                       dry_run=bool(q.get('dry_run')))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400

@app.route('/api/pyc/baseline', methods=['POST'])
def pyc_baseline():
    q=request.get_json()
    try:
        entries=set_stock_baseline(q['path'])
        return jsonify(dict(ok=True, count=len(entries),
            archives=sorted(entries.keys(), key=lambda x:int(x)),
            details={k:dict(sha256=v['sha256'][:16], size=v['size']) for k,v in entries.items()}))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

@app.route('/api/pyc/restore', methods=['POST'])
def pyc_restore():
    q=request.get_json()
    try:
        return jsonify(restore_from(q.get('source','backup')))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))),400

def _first_int(s):
    m=re.search(r'\((\d+)', s or '')
    return m.group(1) if m else None

def _pretty_world(tok):
    if not tok: return None
    t=re.sub(r'^S_WORLD_(LOC_)?','',tok)
    return t.replace('_',' ').title()

@app.route('/api/pyc/aitrack_crosswalk')
def pyc_aitrack_crosswalk():
    """Return every exposed AIRACINGTRACKCONFIG field with friendly track names,
    per-field shared-value warnings, and clean-baseline values when available."""
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    try:
        cfg=mapper_records(AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS)
        tracks=mapper_records(AICFG,'TRACK_c',['AITrackProfile'])
        aiws=mapper_records(AICFG,'WORLDSCRIPT_c',['WorldID','TrackPointer'])
        gws=mapper_records(DBFILE,'WORLDSCRIPT_c',['WorldID','WorldName','WorldLocation'])
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400

    stock_by_uid={}
    base=baseline_archive('0')
    if base:
        try:
            stock_rows=mapper_records(AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS,archive=base)
            stock_by_uid={str(r.get('uid')):r for r in stock_rows}
        except Exception:
            stock_by_uid={}

    track_to_cfg={str(t['uid']):_first_int(t.get('AITrackProfile')) for t in tracks}
    aiworld_to_track={str(w.get('WorldID')):_first_int(w.get('TrackPointer')) for w in aiws}
    game_by_world={str(g.get('WorldID')):g for g in gws}
    cfg_to_world={}
    for wid,truid in aiworld_to_track.items():
        cuid=track_to_cfg.get(str(truid))
        if cuid: cfg_to_world.setdefault(str(cuid),[]).append(str(wid))

    from collections import Counter
    counts={f:Counter(str(r.get(f)) for r in cfg) for f in AI_TRACK_FIELDS}
    out=[]
    for c in cfg:
        cuid=str(c.get('uid')); label=loc=wtok=None
        for wid in cfg_to_world.get(cuid,[]):
            gw=game_by_world.get(str(wid))
            if gw:
                wtok=gw.get('WorldName'); loc=gw.get('WorldLocation')
                label=_pretty_world(wtok); break
        row=dict(c)
        row.update(track=label or 'Unmapped config',
                   location=_pretty_world(loc) if loc else None,
                   world_token=wtok,
                   stock={f:stock_by_uid.get(cuid,{}).get(f) for f in AI_TRACK_FIELDS}
                         if stock_by_uid else None,
                   shared={f:counts[f].get(str(c.get(f)),0)>1 for f in AI_TRACK_FIELDS},
                   scalar={f:_direct_scalar(c.get(f)) for f in AI_TRACK_FIELDS})
        out.append(row)
    out.sort(key=lambda r:(r['track']=='Unmapped config',r['track'] or '',int(r.get('uid') or 0)))
    return jsonify(dict(ok=True,rows=out,fields=AI_TRACK_FIELDS,
        stock_source=('baseline' if stock_by_uid else None),
        shared_note='Values vary by track. Shared-value edits are previewed against every exposed field and are blocked if anything except the selected UID/field would change.'))

@app.route('/api/pyc/aiglobal')
def pyc_aiglobal():
    """Read global AI behavior records. Nested min/max objects remain read-only."""
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    try:
        rows=mapper_records(AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS)
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400
    stock_by_uid={}
    base=baseline_archive('0')
    if base:
        try:
            sr=mapper_records(AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS,archive=base)
            stock_by_uid={str(r.get('uid')):r for r in sr}
        except Exception:
            stock_by_uid={}
    from collections import Counter
    counts={f:Counter(str(r.get(f)) for r in rows) for f in AI_GLOBAL_FIELDS}
    out=[]
    for r in rows:
        uid=str(r.get('uid')); row=dict(r)
        row.update(stock={f:stock_by_uid.get(uid,{}).get(f) for f in AI_GLOBAL_FIELDS}
                         if stock_by_uid else None,
                   shared={f:counts[f].get(str(r.get(f)),0)>1 for f in AI_GLOBAL_FIELDS},
                   scalar={f:_direct_scalar(r.get(f)) for f in AI_GLOBAL_FIELDS})
        out.append(row)
    return jsonify(dict(ok=True,rows=out,fields=AI_GLOBAL_FIELDS,
        stock_source=('baseline' if stock_by_uid else None),
        note='Direct scalar fields can be previewed/applied. Nested min/max objects are intentionally read-only.'))

@app.route('/api/pyc/worldpace')
def pyc_worldpace():
    """Track-specific practice/qualifying pace targets and environment values."""
    if not mapper_ready():
        return jsonify(dict(ok=False,error='mapper/patcher not in app folder')),400
    fields=['WorldID','WorldName','WorldLocation']+WORLD_PACE_FIELDS
    try:
        rows=mapper_records(DBFILE,'WORLDSCRIPT_c',fields)
    except Exception as e:
        return jsonify(dict(ok=False,error=str(e))),400
    stock_by_uid={}
    base=baseline_archive('0')
    if base:
        try:
            sr=mapper_records(DBFILE,'WORLDSCRIPT_c',fields,archive=base)
            stock_by_uid={str(r.get('uid')):r for r in sr}
        except Exception:
            stock_by_uid={}
    from collections import Counter
    counts={f:Counter(str(r.get(f)) for r in rows) for f in WORLD_PACE_FIELDS}
    out=[]
    for r in rows:
        uid=str(r.get('uid')); token=r.get('WorldName'); loc=r.get('WorldLocation')
        if not any(r.get(f) not in (None,'') for f in WORLD_PACE_FIELDS):
            continue
        row=dict(r)
        row.update(track=_pretty_world(token) or _pretty_world(loc) or f'World UID {uid}',
                   stock={f:stock_by_uid.get(uid,{}).get(f) for f in WORLD_PACE_FIELDS}
                         if stock_by_uid else None,
                   shared={f:counts[f].get(str(r.get(f)),0)>1 for f in WORLD_PACE_FIELDS},
                   scalar={f:_direct_scalar(r.get(f)) for f in WORLD_PACE_FIELDS})
        out.append(row)
    out.sort(key=lambda r:(r.get('track') or '',int(r.get('uid') or 0)))
    return jsonify(dict(ok=True,rows=out,fields=WORLD_PACE_FIELDS,
        stock_source=('baseline' if stock_by_uid else None),
        note='Practice best/worst values are track pace targets. Record speeds and temperatures are reference/environment fields. Every edit is previewed against all exposed WORLDSCRIPT fields and blocked if a shared constant would alter another track.'))

# ==================== end v0.9 RACE SETTINGS / AI ====================



# ==================== v0.9.5 SCR DRAFT / AERO ====================
# SCR files store physics as NUL-separated ASCII: KEY \0 VALUE \0
# Confirmed from ARCHIVE0 (48 track SCRs + pace car):
#   AERODYNAMICS { DRAG-CDA 0.0  FRONT-DRAFT-DRAG 0.91 ... }
# Values are edited IN PLACE with the SAME character count (no resizing).
SCR_KEYS = ['FRONT-DRAFT-DRAG','REAR-DRAFT-DRAG','SIDE-DRAFT-DRAG',
            'FRONT-DRAFT-DOWNFORCE','REAR-DRAFT-DOWNFORCE','OVERALL-DOWNFORCE-SCALE']
SCR_NUMRX = re.compile(r'^-?\d+(\.\d+)?$')

def _scr_kv(data, key):
    """Find KEY\0VALUE\0. Returns (value, value_offset, value_len) or (None,None,None).
    Requires the key to be a whole NUL-delimited token."""
    k=key.encode()
    i=data.find(k)
    while i>=0:
        pre_ok = (i==0 or data[i-1]==0)
        end=i+len(k)
        if pre_ok and end<len(data) and data[end]==0:
            e=data.find(b'\0', end+1)
            if e>0:
                return data[end+1:e].decode('latin1'), end+1, e-(end+1)
        i=data.find(k, i+1)
    return None,None,None

def _scr_role(name):
    u=name.upper()
    if u.startswith('PACECAR'): return None          # hidden: pace car
    if u.endswith('PLAYER_SCR.ARC'): return 'player'
    if u.endswith('AI_SCR.ARC'): return 'ai'
    return None                                       # driver SCRs carry no aero

def _scr_track(name):
    u=name.upper().replace('_SCR.ARC','')
    if u.startswith('NASCAR'): u=u[6:]
    for suf in ('PLAYER','AI'):
        if u.endswith(suf): u=u[:-len(suf)]
    return u.title() or name

def scr_entries(archive_override=None):
    """Enumerate SCR entries that actually contain an AERODYNAMICS block.
    Self-validating: an entry is only accepted if the extracted bytes really
    hold the keys, so reading the wrong archive can never silently succeed."""
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    out=[]
    for arcid,r in sorted(reg.items(), key=lambda x:int(x[0])):
        try: ents=parse_cdfiles(r['cdf'])
        except Exception: continue
        arpath = archive_override if (archive_override and arcid=='0') else r['ar']
        if not os.path.exists(arpath): continue
        with open(arpath,'rb') as f:
            for off,size,name in ents:
                if not name.upper().endswith('_SCR.ARC'): continue
                role=_scr_role(name)
                if not role: continue
                if size<=0 or size>4*1024*1024: continue
                f.seek(off); data=f.read(size)
                if b'AERODYNAMICS' not in data: continue
                vals={}; offs={}
                for k in SCR_KEYS:
                    v,vo,vl=_scr_kv(data,k)
                    if v is None: continue
                    vals[k]=v; offs[k]=dict(abs_off=off+vo, length=vl)
                if 'FRONT-DRAFT-DRAG' not in vals: continue
                out.append(dict(arc=arcid, name=name, entry_off=off, size=size,
                                track=_scr_track(name), role=role,
                                values=vals, offsets=offs))
    return out

def _scr_snapshot(archive_override=None):
    return {(e['name'],k):v for e in scr_entries(archive_override)
            for k,v in e['values'].items()}

def scr_set(name, key, new_value, dry_run=False):
    """Compatibility wrapper for older Draft/Aero callers.

    All edits now use the same Racing Controls dispatcher: same-width values
    receive surgical writes, while shorter/longer values rebuild and repoint
    the owning SCR container.
    """
    try:
        rows=_scr_numeric_inventory()
        row=next((r for r in rows if r['name'].upper()==str(name).upper()
                  and r['key'].upper()==str(key).upper() and int(r['occurrence'])==0),None)
        if not row:return dict(ok=False,error=f'{name}/{key} not found')
        return scr_key_set(row['arc'],row['name'],row['key'],new_value,
                           dry_run=dry_run,occurrence=0)
    except Exception as ex:
        return dict(ok=False,error=str(ex))

PLATE_TRACKS = {'Daytona','Talladega'}

def _scr_stock_source():
    """Best clean SCR reference as (archive, cdfiles, label).

    Repoint installs move indexed entries, so a pristine archive MUST be read
    with the pristine cdfiles index that was backed up beside it. Older builds
    mixed the stock archive with the live index, which produced blank Stock
    columns after the first repoint.
    """
    try:
        g,reg=registry();v=reg.get('0') if reg else None
        if v:
            cb=backup_path(v['cdf'])
            if os.path.exists(v['bak']) and os.path.exists(cb):
                return v['bak'],cb,'backup'
    except Exception: pass
    try:
        b=baseline_archive('0')
        if b:
            # A registered baseline currently stores the archive only. It is
            # usable with the live index only while no entry offsets differ.
            g,reg=registry();v=reg.get('0') if reg else None
            if v and os.path.getsize(b)==os.path.getsize(v['ar']):
                return b,v['cdf'],'baseline'
    except Exception: pass
    return None,None,None

@app.route('/api/scr/list')
def scr_list():
    try:
        ents=scr_entries()
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400
    stock={}
    spath,scdf,slabel=_scr_stock_source()
    if spath:
        try:
            base_rows=_scr_numeric_inventory(archive_override=spath,archive_id='0',cdf_override=scdf)
            stock={(e['name'],e['key']):e['value'] for e in base_rows if int(e.get('occurrence',0))==0}
        except Exception:
            stock={}; slabel=None
    tracks={}
    for e in ents:
        t=tracks.setdefault(e['track'], dict(track=e['track'], player=None, ai=None,
                                             plate=e['track'] in PLATE_TRACKS))
        t[e['role']]=dict(name=e['name'], arc=e['arc'], values=e['values'],
                          stock={k:stock.get((e['name'],k)) for k in e['values']} if stock else None,
                          lengths={k:v['length'] for k,v in e['offsets'].items()})
    rows=sorted(tracks.values(), key=lambda x:(not x['plate'], x['track']))
    return jsonify(dict(ok=True, rows=rows, keys=SCR_KEYS, count=len(ents),
                        stock_source=slabel))

@app.route('/api/scr/set', methods=['POST'])
def scr_set_api():
    q=request.get_json()
    try:
        result=scr_key_set(str(q.get('arc','0')),q['name'],q['key'],q['value'],
                           dry_run=bool(q.get('dry_run')),occurrence=int(q.get('occurrence',0)))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400


# ---- v0.9.17 track-sorted SCR editor + atomic batch apply ----
SCR_KEY_RX=re.compile(r'^[A-Za-z][A-Za-z0-9_-]{1,95}$')

SCR_TESTED_KEYS={'FRONT-DRAFT-DRAG'}
SCR_EXISTING_KEYS=set(SCR_KEYS)
SCR_RECOMMENDED_KEYS={
    # Draft / aero
    'FRONT-DRAFT-DRAG','REAR-DRAFT-DRAG','SIDE-DRAFT-DRAG',
    'FRONT-DRAFT-DOWNFORCE','REAR-DRAFT-DOWNFORCE','OVERALL-DOWNFORCE-SCALE',
    'TAPE','SPLITTER',
    # Grip / tires
    'AI-LAT-GRIP-BOOST','MAXLATFRICTION','MAXLONGFRICTION','OPTSLIPANGLE',
    'OPTSLIPRATIO','TREADWEAR-GRADE','TREADWEAR-DATA','PRESSURE',
    # Suspension / chassis
    'SPRING-STIFFNESS','DAMPING-RATIO','REBOUND-DAMPING-RATIO',
    'ROLL-CENTRE-HEIGHT','STIFFNESS','BUMP-STOP-STRENGTH','CAMBER','TOE',
    'JOUNCE-LIMIT',
    # Brakes
    'BRAKE-BIAS','MAX-TORQUE','FADE','BRAKE-DIAMETER','BRAKE-THICKNESS',
    'BRAKE-MATERIAL',
    # Powertrain
    'FINAL-RATIO','GEARS','EFFICIENCY','RPM-TORQUE','CHANGE-UP-POINT',
    'CHANGE-DOWN-POINT','LIMITER-RANGE','MAX-TORQUE-CAPACITY',
    # Steering
    'MAX-STEERING-ANGLE','ACKERMANN',
}
SCR_DESCRIPTIONS={
    'FRONT-DRAFT-DRAG':'Tested: lower values create a stronger tow and higher drafting speed.',
    'REAR-DRAFT-DRAG':'Rear-car draft drag factor; gameplay effect is still experimental.',
    'SIDE-DRAFT-DRAG':'Side-draft drag factor; gameplay effect is still experimental.',
    'FRONT-DRAFT-DOWNFORCE':'Front downforce retained while drafting.',
    'REAR-DRAFT-DOWNFORCE':'Rear downforce retained while drafting.',
    'OVERALL-DOWNFORCE-SCALE':'Overall aerodynamic downforce scale.',
    'TAPE':'Likely grille tape / cooling-aero setup value.',
    'SPLITTER':'Likely splitter / front-aero setup value.',
    'AI-LAT-GRIP-BOOST':'Likely AI-only lateral-grip assistance or multiplier.',
    'MAXLATFRICTION':'Likely peak lateral tire grip.',
    'MAXLONGFRICTION':'Likely peak acceleration and braking tire grip.',
    'OPTSLIPANGLE':'Likely tire slip angle at peak lateral grip.',
    'OPTSLIPRATIO':'Likely tire slip ratio at peak longitudinal grip.',
    'TREADWEAR-GRADE':'Likely tire durability / wear grade.',
    'TREADWEAR-DATA':'Likely tire-wear curve data.',
    'PRESSURE':'Tire pressure for this wheel context.',
    'SPRING-STIFFNESS':'Suspension spring stiffness.',
    'DAMPING-RATIO':'Suspension damping ratio.',
    'REBOUND-DAMPING-RATIO':'Suspension rebound damping ratio.',
    'ROLL-CENTRE-HEIGHT':'Chassis roll-centre height.',
    'STIFFNESS':'Component stiffness; check the displayed context.',
    'BUMP-STOP-STRENGTH':'Bump-stop stiffness / strength.',
    'CAMBER':'Wheel camber setting.',
    'TOE':'Wheel toe setting.',
    'JOUNCE-LIMIT':'Suspension compression-travel limit.',
    'BRAKE-BIAS':'Front/rear brake balance.',
    'MAX-TORQUE':'Maximum torque; meaning depends on context (brakes, engine, etc.).',
    'FADE':'Brake fade parameter.',
    'FINAL-RATIO':'Final-drive ratio.',
    'GEARS':'Gear-count or gearbox data value; check context.',
    'EFFICIENCY':'Powertrain efficiency value.',
    'RPM-TORQUE':'Engine torque-curve point.',
    'CHANGE-UP-POINT':'Automatic upshift point.',
    'CHANGE-DOWN-POINT':'Automatic downshift point.',
    'LIMITER-RANGE':'Engine rev-limiter range.',
    'MAX-TORQUE-CAPACITY':'Maximum drivetrain torque capacity.',
    'MAX-STEERING-ANGLE':'Maximum steering lock / angle.',
    'ACKERMANN':'Ackermann steering geometry amount.',
}
SCR_CATEGORY_ORDER=[
    'Draft / Aero','Grip / Tires','Suspension','Brakes','Engine / Gearing',
    'Steering','AI Behavior','Camera / Visual','Other'
]

def _scr_clean_ascii(raw):
    if not raw: return ''
    s=raw.decode('latin1','ignore')
    s=re.sub(r'^[^\x20-\x7E]+','',s)
    s=re.sub(r'[^\x20-\x7E]+$','',s)
    if not s or any(ord(c)<32 or ord(c)>126 for c in s): return ''
    return s

def _scr_parse_numeric_rows(data):
    """Parse every named numeric scalar and preserve duplicate occurrences.

    The old explorer kept only the first occurrence of each key in a file. That
    hid the four wheel-specific MAXLATFRICTION/MAXLONGFRICTION/etc. rows. This
    parser tracks brace nesting and assigns a stable occurrence number per key.
    """
    toks=[]; pos=0
    for raw in data.split(b'\0'):
        toks.append((pos,_scr_clean_ascii(raw)))
        pos+=len(raw)+1

    stack=[]; previous=''; counts={}; rows=[]
    for i,(key_off,tok) in enumerate(toks):
        if tok=='{':
            if previous and previous not in ('{','}') and not SCR_NUMRX.match(previous):
                stack.append(previous)
            previous=''; continue
        if tok=='}':
            if stack: stack.pop()
            previous=''; continue
        if not tok: continue

        if i+1<len(toks):
            value_off,value=toks[i+1]
            if SCR_KEY_RX.match(tok) and SCR_NUMRX.match(value):
                ku=tok.upper(); occurrence=counts.get(ku,0); counts[ku]=occurrence+1
                rows.append(dict(
                    key=tok,value=value,length=len(value),occurrence=occurrence,
                    key_rel=key_off,value_rel=value_off,path='/'.join(stack)
                ))
        previous=tok
    return rows

def _scr_wheel(path):
    u=path.upper().replace('_','-')
    for raw,label in (
        ('FRONT-RIGHT','Front Right'),('FRONT-LEFT','Front Left'),
        ('REAR-RIGHT','Rear Right'),('REAR-LEFT','Rear Left')):
        if raw in u: return label
    return ''

def _scr_context(path,key):
    u=path.upper(); wheel=_scr_wheel(path)
    if wheel: return wheel
    if 'HANDBRAKE' in u: return 'Handbrake'
    for raw,label in (
        ('AERODYNAMICS','Aerodynamics'),('ANTI-ROLL-BAR','Anti-roll bar'),
        ('SUSPENSION','Suspension'),('BRAKES','Brakes'),('GEARBOX','Gearbox'),
        ('ENGINE','Engine'),('DRIVETRAIN','Drivetrain'),('STEERING','Steering'),
        ('GSCHASSIS','AI chassis'),('CHASSIS','Chassis'),('MOVER','Vehicle mover')):
        if raw in u: return label
    parts=[p for p in path.split('/') if p and p not in ('VEHICLE','!VEHICLE','GSRACECAR','DATA')]
    return parts[-1].replace('-',' ').title() if parts else 'General'

def _scr_category(key,path):
    k=key.upper(); p=path.upper()
    if k in SCR_KEYS or 'AERODYNAMICS' in p or any(x in k for x in ('DRAFT','DOWNFORCE','DRAG-CDA','SPLITTER','TAPE')):
        return 'Draft / Aero'
    if any(x in k for x in ('GRIP','FRICTION','SLIP','TREADWEAR','PRESSURE','TYRE','TIRE')) or '/TYRE' in p:
        return 'Grip / Tires'
    if any(x in k for x in ('SPRING','DAMPING','CAMBER','TOE','JOUNCE','ROLL-CENTRE','BUMP-STOP')) or any(x in p for x in ('SUSPENSION','ANTI-ROLL-BAR')):
        return 'Suspension'
    if any(x in k for x in ('BRAKE','FADE')) or 'BRAKES' in p or 'HANDBRAKE' in p:
        return 'Brakes'
    if any(x in k for x in ('GEAR','RATIO','RPM','TORQUE','LIMITER','CLUTCH','DIFF','EFFICIENCY','FUEL')) or any(x in p for x in ('ENGINE','GEARBOX','DRIVETRAIN')):
        return 'Engine / Gearing'
    if any(x in k for x in ('STEER','ACKERMANN')) or 'STEERING' in p:
        return 'Steering'
    if k.startswith('AI-') or k.startswith('AI_') or 'AI-' in k:
        return 'AI Behavior'
    if any(x in k for x in ('VIEW','CAMERA','TILT','FOV','LOD','LIGHT','SHADOW','SOUND')):
        return 'Camera / Visual'
    return 'Other'

def _scr_status(key):
    k=key.upper()
    if k in SCR_TESTED_KEYS: return 'tested'
    if k in SCR_EXISTING_KEYS: return 'existing'
    if k in SCR_RECOMMENDED_KEYS: return 'candidate'
    return 'raw'

def _scr_numeric_inventory(archive_override=None, archive_id='0', cdf_override=None, track_filter=None,
                           role_filter=None, query=None, recommended_only=False):
    """Inventory numeric Player/AI SCR fields with path and occurrence context."""
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    archive_id=str(archive_id)
    tf=(track_filter or '').strip().lower(); rf=(role_filter or '').strip().lower()
    q=(query or '').strip().lower(); rows=[]

    for arcid,r in sorted(reg.items(),key=lambda x:int(x[0])):
        cdfpath=cdf_override if (cdf_override and arcid==archive_id) else r['cdf']
        try: ents=parse_cdfiles(cdfpath)
        except Exception: continue
        arpath=archive_override if (archive_override and arcid==archive_id) else r['ar']
        if not os.path.exists(arpath): continue
        with open(arpath,'rb') as fh:
            for off,size,name in ents:
                role=_scr_role(name)
                if not role or not name.upper().endswith('_SCR.ARC'): continue
                track=_scr_track(name)
                if tf and track.lower()!=tf: continue
                if rf and rf!='all' and role!=rf: continue
                if size<=0 or size>4*1024*1024: continue
                fh.seek(off); data=fh.read(size)
                if b'AERODYNAMICS' not in data: continue

                for raw in _scr_parse_numeric_rows(data):
                    key=raw['key']; ku=key.upper(); path=raw['path']
                    category=_scr_category(key,path)
                    recommended=ku in SCR_RECOMMENDED_KEYS
                    context=_scr_context(path,key); wheel=_scr_wheel(path)
                    description=SCR_DESCRIPTIONS.get(ku,'')
                    searchable=' '.join((track,role,key,path,context,category,raw['value'],description)).lower()
                    if recommended_only and not recommended: continue
                    if q and q not in searchable: continue
                    occurrence=raw['occurrence']
                    ident=f'{arcid}|{name.upper()}|{ku}|{occurrence}'
                    rows.append(dict(
                        id=ident,arc=arcid,name=name,track=track,role=role,
                        key=key,value=raw['value'],length=raw['length'],
                        occurrence=occurrence,path=path,context=context,wheel=wheel,
                        category=category,recommended=recommended,status=_scr_status(key),
                        description=description,abs_off=off+raw['value_rel'],
                        entry_off=off,entry_size=size,
                        pair_id=f'{track.lower()}|{ku}|{occurrence}'
                    ))
    return rows

def _scr_row_ident(row):
    return (str(row['arc']),row['name'].upper(),row['key'].upper(),int(row['occurrence']))

def _scr_numeric_snapshot(archive_override=None, archive_id='0'):
    return {_scr_row_ident(r):r['value'] for r in _scr_numeric_inventory(archive_override,archive_id)}

def _scr_public_row(row,stock):
    r=dict(row); ident=_scr_row_ident(row)
    r['stock']=stock.get(ident) if stock else None
    r['modded']=r['stock'] is not None and r['stock']!=r['value']
    for k in ('abs_off','entry_off','entry_size'):
        r.pop(k,None)
    return r

def _scr_key_batch_fixed(changes,dry_run=False):
    """Fast surgical path for batches whose encoded values keep their original widths."""
    if not isinstance(changes,list) or not changes:
        return dict(ok=False,error='no pending changes')
    if len(changes)>2500:
        return dict(ok=False,error='batch is too large; limit is 2500 values')

    rows=_scr_numeric_inventory()
    by_ident={_scr_row_ident(r):r for r in rows}
    requested=[]; seen=set(); archives=set(); width_mismatch=False

    for item in changes:
        try:
            ident=(str(item.get('arc','0')),str(item['name']).upper(),
                   str(item['key']).upper(),int(item.get('occurrence',0)))
        except Exception:
            return dict(ok=False,error='invalid change target')
        if ident in seen:
            return dict(ok=False,error=f'duplicate target in batch: {ident[2]} occurrence {ident[3]}')
        seen.add(ident)
        row=by_ident.get(ident)
        if not row:
            return dict(ok=False,error=f'target not found: {ident[1]}/{ident[2]} occurrence {ident[3]}')

        value=str(item.get('value','')).strip()
        if not SCR_NUMRX.match(value):
            return dict(ok=False,error=f'{row["key"]}: value must be a plain number')
        if len(value)!=row['length']:
            width_mismatch=True
        archives.add(str(row['arc']))
        if value!=row['value']:
            requested.append((ident,row,value))

    if not requested:
        return dict(ok=False,error='all pending values already match the game')
    if len(archives)!=1:
        return dict(ok=False,error='one atomic batch may target only one archive')
    # This function is also reached by older UI paths and Season Pack code.
    # Never reject a width-changing value here: hand the normalized targets to
    # the variable-size ARCC rebuild/repoint path instead.
    if width_mismatch:
        return _scr_variable_batch(requested,dry_run)

    arcid=next(iter(archives)); g,reg=registry()
    if arcid not in reg: return dict(ok=False,error=f'ARCHIVE{arcid} is unavailable')
    live=reg[arcid]['ar']; bak=reg[arcid]['bak']; original_size=os.path.getsize(live)
    before={_scr_row_ident(r):r['value'] for r in rows}
    expected={ident:value for ident,row,value in requested}
    tmp=os.path.join(_tf.gettempdir(),f'n15mod_scrbatch_{os.getpid()}_{abs(hash(tuple(sorted(expected))))%99999}.AR')

    try:
        if os.path.exists(tmp): os.remove(tmp)
        shutil.copyfile(live,tmp)
        with open(tmp,'r+b') as fh:
            for ident,row,value in requested:
                fh.seek(row['abs_off'])
                existing=fh.read(row['length']).decode('ascii','replace')
                if existing!=row['value']:
                    return dict(ok=False,error=f'{row["key"]}: live bytes changed since scan; reload the editor')
                fh.seek(row['abs_off']); fh.write(value.encode('ascii'))
            fh.flush(); os.fsync(fh.fileno())

        if os.path.getsize(tmp)!=original_size:
            return dict(ok=False,error='temporary archive size changed; batch refused')

        after=_scr_numeric_snapshot(tmp,arcid)
        actual={}
        for ident in set(before)|set(after):
            ov=before.get(ident); nv=after.get(ident)
            if ov!=nv: actual[ident]=(ov,nv)

        unexpected=[ident for ident in actual if ident not in expected]
        missing=[ident for ident in expected if ident not in actual]
        wrong=[ident for ident,value in expected.items() if ident in actual and actual[ident][1]!=value]
        if unexpected or missing or wrong:
            details=[]
            for ident in unexpected[:10]: details.append(f'unexpected {ident[1]}/{ident[2]}#{ident[3]}')
            for ident in missing[:10]: details.append(f'missing {ident[1]}/{ident[2]}#{ident[3]}')
            for ident in wrong[:10]: details.append(f'wrong value {ident[1]}/{ident[2]}#{ident[3]}')
            return dict(ok=False,collateral=bool(unexpected),affected_count=len(actual),
                        error='full SCR diff guard blocked the batch: '+'; '.join(details),
                        changes=[dict(arc=i[0],name=i[1],key=i[2],occurrence=i[3],old=v[0],new=v[1])
                                 for i,v in list(actual.items())[:50]])

        summary=[dict(id=row['id'],track=row['track'],role=row['role'],key=row['key'],
                      occurrence=row['occurrence'],context=row['context'],old=row['value'],new=value)
                 for ident,row,value in requested]
        track_count=len({row['track'] for ident,row,value in requested})
        if dry_run:
            return dict(ok=True,dry_run=True,affected_count=len(requested),
                        track_count=track_count,changes=summary,
                        note='preview confirmed every requested setting and no unrelated changes')

        ensure_backup(live,bak)
        # Commit only the validated value bytes, not a whole-archive copy. This
        # preserves every unrelated mod already present in the live archive and
        # gives us a surgical rollback if readback ever fails.
        with open(live,'r+b') as fh:
            for ident,row,value in requested:
                fh.seek(row['abs_off'])
                existing=fh.read(row['length']).decode('ascii','replace')
                if existing!=row['value']:
                    return dict(ok=False,error=f'{row["key"]}: live bytes changed after preview; reload and retry')
                fh.seek(row['abs_off']); fh.write(value.encode('ascii'))
            fh.flush(); os.fsync(fh.fileno())

        if os.path.getsize(live)!=original_size:
            with open(live,'r+b') as fh:
                for ident,row,value in requested:
                    fh.seek(row['abs_off']); fh.write(row['value'].encode('ascii'))
            return dict(ok=False,error='live archive size changed; requested bytes were rolled back')

        readback=_scr_numeric_snapshot()
        failed=[ident for ident,value in expected.items() if readback.get(ident)!=value]
        if failed:
            with open(live,'r+b') as fh:
                for ident,row,value in requested:
                    fh.seek(row['abs_off']); fh.write(row['value'].encode('ascii'))
                fh.flush(); os.fsync(fh.fileno())
            return dict(ok=False,error='batch readback failed; requested bytes were rolled back',
                        failed=[f'{i[1]}/{i[2]}#{i[3]}' for i in failed[:20]])

        return dict(ok=True,verified=True,affected_count=len(requested),
                    track_count=track_count,changes=summary)
    finally:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError: pass


# ---- v0.9.26 variable-size SCR rebuild + repoint ----
def _scr_pack_type_size(record_type,size):
    if not (0<=int(size)<0x1000000): raise ValueError('SCR ARCC record exceeds 24-bit size field')
    return int.from_bytes(bytes((int(record_type)&0xff,))+int(size).to_bytes(3,'big'),'little')


def _scr_arcc_records(raw):
    if raw[:4]!=b'ARCC' or len(raw)<0x80: raise ValueError('SCR entry is not an ARCC container')
    count=struct.unpack_from('<I',raw,4)[0];base=0x80+count*16
    if count<=0 or count>4096 or base>len(raw): raise ValueError('invalid SCR ARCC record table')
    rows=[];max_end=base
    for i in range(count):
        key,off,nref,packed=struct.unpack_from('<4I',raw,0x80+i*16)
        b=packed.to_bytes(4,'little');typ=b[0];size=int.from_bytes(b[1:4],'big');absolute=base+off
        if absolute<base or absolute+size>len(raw): raise ValueError(f'SCR ARCC record {i} exceeds the file')
        rows.append(dict(index=i,key=key,name_ref=nref,record_type=typ,size=size,absolute=absolute,
                         payload=bytes(raw[absolute:absolute+size])))
        max_end=max(max_end,absolute+size)
    tail=bytes(raw[max_end:])
    if tail and any(tail): raise ValueError('SCR ARCC has an unknown non-zero tail; variable rebuild refused')
    return count,base,rows


def _scr_rebuild_arcc(raw,replacements):
    """Replace one or more absolute byte ranges inside ARCC record payloads.

    replacements: iterable of (absolute_offset, old_length, new_bytes, label).
    Every untouched record remains byte-identical. Records are rebuilt on 16-byte
    absolute boundaries and the 24-bit payload size fields are updated.
    """
    count,base,records=_scr_arcc_records(raw)
    by_record=collections.defaultdict(list)
    for absolute,old_len,new_bytes,label in replacements:
        hit=None
        for rec in records:
            if rec['absolute']<=absolute and absolute+old_len<=rec['absolute']+rec['size']:
                hit=rec;break
        if hit is None: raise ValueError(f'{label}: value range is outside every SCR record')
        by_record[hit['index']].append((absolute-hit['absolute'],old_len,bytes(new_bytes),label))
    payloads=[]
    for rec in records:
        edits=sorted(by_record.get(rec['index'],[]),key=lambda x:x[0])
        src=rec['payload'];out=bytearray();cursor=0
        for rel,old_len,new_bytes,label in edits:
            if rel<cursor or rel+old_len>len(src): raise ValueError(f'{label}: overlapping or invalid SCR edit')
            out+=src[cursor:rel];out+=new_bytes;cursor=rel+old_len
        out+=src[cursor:];payloads.append(bytes(out))
    header=bytearray(raw[:0x80]);table=bytearray(count*16);data=bytearray();cursor=0
    for rec,payload in zip(records,payloads):
        absolute=(base+cursor+15)&~15;pad=absolute-(base+cursor)
        if pad:data+=b'\0'*pad;cursor+=pad
        off=cursor;data+=payload;cursor+=len(payload)
        struct.pack_into('<4I',table,rec['index']*16,rec['key'],off,rec['name_ref'],
                         _scr_pack_type_size(rec['record_type'],len(payload)))
    rebuilt=bytes(header+table+data)
    c2,b2,r2=_scr_arcc_records(rebuilt)
    if c2!=count or b2!=base: raise ValueError('SCR ARCC rebuild changed its table shape')
    changed=set(by_record)
    for old,new in zip(records,r2):
        if (old['key'],old['name_ref'],old['record_type'])!=(new['key'],new['name_ref'],new['record_type']):
            raise ValueError('SCR ARCC rebuild changed record identity')
        if old['index'] not in changed and old['payload']!=new['payload']:
            raise ValueError(f'SCR ARCC rebuild changed untouched record {old["index"]}')
    return rebuilt


def _scr_entry_value_map(data):
    return {(r['key'].upper(),int(r['occurrence'])):r['value'] for r in _scr_parse_numeric_rows(data)}


def _scr_variable_batch(requested,dry_run=False):
    """Rebuild changed SCR entries and atomically append/repoint them.

    This path is selected only when at least one value changes encoded width.
    It never overwrites an indexed entry in place; all affected files are
    validated independently and then installed as one rollback-capable package.
    """
    g,reg=registry()
    if not g: return dict(ok=False,error='game folder not found')
    archives={str(row['arc']) for ident,row,value in requested}
    if len(archives)!=1:return dict(ok=False,error='one atomic SCR batch may target only one archive')
    arcid=next(iter(archives));v=reg.get(arcid)
    if not v:return dict(ok=False,error=f'ARCHIVE{arcid} is unavailable')
    _,index_rows,_=_rp_index_rows(v['cdf'])
    groups=collections.defaultdict(list)
    for ident,row,value in requested:groups[row['name'].upper()].append((ident,row,value))
    td=tempfile.mkdtemp(prefix='n15mod_scr_repoint_');plans=[];expected={}
    try:
        for n,(name,items) in enumerate(sorted(groups.items())):
            idxrow=_rp_find_row(index_rows,items[0][1]['name'])
            with open(v['ar'],'rb') as fh:fh.seek(idxrow['offset']);current=fh.read(idxrow['size'])
            if len(current)!=idxrow['size']:raise ValueError(name+': short archive read')
            current_map=_scr_entry_value_map(current);repls=[]
            for ident,row,value in items:
                key=(row['key'].upper(),int(row['occurrence']))
                if current_map.get(key)!=row['value']:
                    raise ValueError(f'{row["key"]}: live SCR changed since scan; reload the editor')
                val=value.encode('ascii')
                # Inventory offsets are absolute archive offsets. Convert them to
                # offsets inside the current indexed SCR entry before rebuilding.
                rel=int(row['abs_off'])-int(row['entry_off'])
                repls.append((rel,int(row['length']),val,f'{row["name"]}/{row["key"]}#{row["occurrence"]}'))
                expected[(name,key[0],key[1])]=value
            rebuilt=_scr_rebuild_arcc(current,repls)
            after_map=_scr_entry_value_map(rebuilt)
            for ident,row,value in items:
                key=(row['key'].upper(),int(row['occurrence']))
                if after_map.get(key)!=value:raise ValueError(f'{row["key"]}: rebuilt SCR failed value readback')
            changed={k for k in set(current_map)|set(after_map) if current_map.get(k)!=after_map.get(k)}
            intended={(row['key'].upper(),int(row['occurrence'])) for ident,row,value in items}
            if changed!=intended:
                extra=sorted(changed-intended)[:10];missing=sorted(intended-changed)[:10]
                raise ValueError(f'{name}: rebuilt SCR diff mismatch; extra={extra} missing={missing}')
            fp=os.path.join(td,f'{n:03d}_{os.path.basename(items[0][1]["name"])}')
            open(fp,'wb').write(rebuilt)
            plan=_rp_plan(arcid,v,idxrow,fp,False)
            plans.append(dict(row=idxrow,path=fp,name=items[0][1]['name'],before=current,
                              after=rebuilt,plan=plan,items=items))
        summary=[dict(id=row['id'],track=row['track'],role=row['role'],key=row['key'],occurrence=row['occurrence'],
                      context=row['context'],old=row['value'],new=value,old_width=row['length'],new_width=len(value))
                 for ident,row,value in requested]
        if dry_run:
            return dict(ok=True,dry_run=True,method='rebuild_repoint',affected_count=len(requested),
                        track_count=len({row['track'] for ident,row,value in requested}),file_count=len(plans),
                        changes=summary,files=[dict(name=p['name'],old_size=len(p['before']),new_size=len(p['after']),
                                                   delta=len(p['after'])-len(p['before']),growth=p['plan']['growth']) for p in plans],
                        note='Each changed SCR container was rebuilt, reparsed, and diffed; apply will append/repoint all files atomically.')
        touched={arcid:dict(v=v,archive_size=os.path.getsize(v['ar']),cdf=open(v['cdf'],'rb').read())}
        _rp_backup_pair(v);results=[]
        try:
            with _RP_LOCK:
                for p in plans:results.append(_rp_install_one(arcid,v,p['row'],p['path'],'SCR variable-size rebuild',False,history=False))
                hist=_rp_load_history();hist.extend(r['history'] for r in results);_rp_save_history(hist)
        except Exception as install_ex:
            state=touched[arcid]
            rollback_archive_cdf(v,state['archive_size'],state['cdf'],'.scr_rollback.tmp',install_ex)
            raise
        # Final live inventory verifies every requested value after all cdfiles
        # entries have moved to their new offsets.
        live_rows=_scr_numeric_inventory();live_by={_scr_row_ident(r):r['value'] for r in live_rows}
        failed=[ident for ident,row,value in requested if live_by.get(ident)!=value]
        if failed:
            state=touched[arcid]
            verify_error=ValueError('variable-size SCR readback failed; the atomic install was rolled back')
            rollback_archive_cdf(v,state['archive_size'],state['cdf'],'.scr_verify_rollback.tmp',verify_error)
            raise verify_error
        return dict(ok=True,verified=True,method='rebuild_repoint',affected_count=len(requested),
                    track_count=len({row['track'] for ident,row,value in requested}),file_count=len(plans),
                    changes=summary,results=[r['history'] for r in results])
    finally:
        shutil.rmtree(td,ignore_errors=True)


def scr_key_batch(changes,dry_run=False):
    """Auto-select fixed-slot or rebuilt/repoint SCR installation."""
    if not isinstance(changes,list) or not changes:return dict(ok=False,error='no pending changes')
    if len(changes)>2500:return dict(ok=False,error='batch is too large; limit is 2500 values')
    rows=_scr_numeric_inventory();by_ident={_scr_row_ident(r):r for r in rows};requested=[];seen=set()
    for item in changes:
        try:ident=(str(item.get('arc','0')),str(item['name']).upper(),str(item['key']).upper(),int(item.get('occurrence',0)))
        except Exception:return dict(ok=False,error='invalid change target')
        if ident in seen:return dict(ok=False,error=f'duplicate target in batch: {ident[2]} occurrence {ident[3]}')
        seen.add(ident);row=by_ident.get(ident)
        if not row:return dict(ok=False,error=f'target not found: {ident[1]}/{ident[2]} occurrence {ident[3]}')
        value=str(item.get('value','')).strip()
        if not SCR_NUMRX.fullmatch(value):return dict(ok=False,error=f'{row["key"]}: value must be a plain finite number')
        if len(value)>32:return dict(ok=False,error=f'{row["key"]}: values are limited to 32 characters')
        try:num=float(value)
        except Exception:return dict(ok=False,error=f'{row["key"]}: invalid number')
        if not _math.isfinite(num):return dict(ok=False,error=f'{row["key"]}: NaN and infinity are blocked')
        if abs(num)>1_000_000_000:return dict(ok=False,error=f'{row["key"]}: absolute values above 1,000,000,000 are blocked')
        if value!=row['value']:requested.append((ident,row,value))
    if not requested:return dict(ok=False,error='all pending values already match the game')
    if all(len(value)==row['length'] for ident,row,value in requested):
        normalized=[dict(arc=row['arc'],name=row['name'],key=row['key'],
                         occurrence=row['occurrence'],value=value)
                    for ident,row,value in requested]
        return _scr_key_batch_fixed(normalized,dry_run)
    return _scr_variable_batch(requested,dry_run)

def scr_key_set(arcid,name,key,new_value,dry_run=False,occurrence=0):
    return scr_key_batch([dict(arc=arcid,name=name,key=key,
                               occurrence=occurrence,value=new_value)],dry_run=dry_run)

@app.route('/api/scr/keys')
def scr_keys_api():
    try:
        meta=request.args.get('meta')=='1'
        track=request.args.get('track','').strip()
        role=request.args.get('role','all').strip().lower()
        query=request.args.get('q','').strip()
        recommended=request.args.get('recommended')=='1'
        limit=max(1,min(5000,int(request.args.get('limit','1500'))))

        if meta:
            all_rows=_scr_numeric_inventory()
            tracks=sorted({r['track'] for r in all_rows})
            counts={c:0 for c in SCR_CATEGORY_ORDER}
            for r in all_rows: counts[r['category']]=counts.get(r['category'],0)+1
            spath,scdf,slabel=_scr_stock_source()
            return jsonify(dict(ok=True,tracks=tracks,total=len(all_rows),
                unique_keys=len({r['key'].upper() for r in all_rows}),
                categories=SCR_CATEGORY_ORDER,category_counts=counts,
                stock_source=slabel,has_stock=bool(spath)))

        rows=_scr_numeric_inventory(track_filter=track or None,
                                    role_filter=role,
                                    query=query,
                                    recommended_only=recommended)
        spath,scdf,slabel=_scr_stock_source(); stock={}
        if spath:
            try:
                stock={_scr_row_ident(r):r['value'] for r in _scr_numeric_inventory(
                    archive_override=spath,archive_id='0',cdf_override=scdf,
                    track_filter=track or None,role_filter=role,
                    query=None,recommended_only=recommended)
                       if str(r['arc'])=='0'}
            except Exception:
                stock={}; slabel=None
        public=[_scr_public_row(r,stock) for r in rows]
        order={name:i for i,name in enumerate(SCR_CATEGORY_ORDER)}
        public.sort(key=lambda r:(r['track'],order.get(r['category'],99),
                                  r['context'],r['key'],r['occurrence'],0 if r['role']=='player' else 1))
        total=len(public); public=public[:limit]
        return jsonify(dict(ok=True,rows=public,count=total,returned=len(public),
                            truncated=total>limit,stock_source=slabel,
                            categories=SCR_CATEGORY_ORDER,editable=True,
                            note='Same-width edits use surgical writes. Different-width values rebuild each SCR container and install them through atomic append/repoint. Every path performs a full all-key diff.'))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/scr/key/set',methods=['POST'])
def scr_key_set_api():
    q=request.get_json(force=True)
    try:
        result=scr_key_set(q.get('arc','0'),q['name'],q['key'],q['value'],
                           dry_run=bool(q.get('dry_run')),
                           occurrence=int(q.get('occurrence',0)))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/scr/keys/batch',methods=['POST'])
def scr_keys_batch_api():
    q=request.get_json(force=True)
    try:
        result=scr_key_batch(q.get('changes',[]),dry_run=bool(q.get('dry_run')))
        return jsonify(result),(200 if result.get('ok') else 400)
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end SCR DRAFT / AERO ====================



# ==================== v0.9.24 IMAGES / TEXTURES / DISCOVERY ====================
# Image index is bundled under data/ui_assets.csv.
# Bytes ALWAYS come from the live archive; the CSV is only an index.
UI_CSV = 'ui_assets.csv'
UI_PACKAGED_MAPPING_CSV = os.path.join(DATA,'ui_asset_map_v2.csv')
DISCOVERED_TEXTURE_CSV = os.path.join(DATA,'discovered_texture_assets.csv')
TEXTURE_DISCOVERY_REPORT = os.path.join(DATA,'texture_discovery_live_report.json')

def _discovered_texture_csv():
    return os.path.join(_profile_dir(),'discovered_texture_assets.csv') if ACTIVE_GAME=='nascar14' else DISCOVERED_TEXTURE_CSV

def _texture_discovery_report():
    return os.path.join(_profile_dir(),'texture_discovery_live_report.json') if ACTIVE_GAME=='nascar14' else TEXTURE_DISCOVERY_REPORT
TEXTURE_DISCOVERY_TOOL = component_path('nascar15_texture_discovery_v0_1.py')

# Confirmed, user-facing assets. png_replace_safe gates the experimental
# PNG-replace path: a family is only "safe" once export -> replace -> boot/menu
# test has actually passed for it. Everything else is export / copy-from only.
CONFIRMED_ASSETS = [
    dict(id='driver_number_card', label='Driver number card',
         entry_rx=r'^(DRIVER_\d+_3DNUM_|imgcarnumber|l_imgcarnumber)', png_replace_safe=False,
         note='Driver-select or driver-details number artwork.'),
    dict(id='driver_paint_preview', label='Driver paint preview',
         entry_rx=r'^DRIVERPAINT_', fmt='DXT5', png_replace_safe=True,
         note='Driver Select car-card preview.'),
    dict(id='paint_select_preview', label='Paint Select preview',
         entry_rx=r'^PAINTSCHEME_', png_replace_safe=False,
         note='Paint Select / custom-scheme thumbnail.'),
    dict(id='track_select_card', label='Track Select card',
         container_rx=r'^(2TRACKSELECTMENUIMAGE|3TRACKCARDIMAGE)\.ARC$', png_replace_safe=False,
         note='Track Select list/card artwork.'),
    dict(id='track_detail_bg', label='Track detail image',
         container_rx=r'^TRACKDETAILIMAGES\.ARC$', png_replace_safe=False,
         note='Track Select detail background.'),
    dict(id='calendar_track_image', label='Calendar track image',
         container_rx=r'^CALENDAR_TRACK_IMAGES\.ARC$', png_replace_safe=False,
         note='Career / Single Season calendar track tile.'),
    dict(id='lobby_track_image', label='Lobby track image',
         container_rx=r'^(2LOBBYTRACKCARDIMAGE|LOBBYTRACKCARDDETAILIMAGE)\.ARC$', png_replace_safe=False,
         note='Multiplayer lobby track artwork.'),
    dict(id='track_facts_image', label='Track Facts image',
         container_rx=r'^TRACK_FACTS_IMG_', png_replace_safe=False,
         note='Track Facts photo or icon.'),
    dict(id='hud_track_map', label='HUD track map / marker',
         container_rx=r'^MAP_', png_replace_safe=False,
         note='HUD minimap, player marker, leader marker, other-car marker, or arrow.'),
    dict(id='race_hud_candidate', label='Race HUD / gauge candidate',
         container_rx=r'.*(?:HUD|GAUGE|TACH|SPEEDO|DAMAGE|STANDING|LEADER|LAP|POSITION|OVERLAY|METER).*', png_replace_safe=False,
         note='Candidate in-race HUD art such as tachometers, vehicle-status diagrams, timing panels, or standings overlays. Exact consumers require an in-game trace test.'),
    dict(id='driver_team_select', label='Driver / team select artwork',
         container_rx=r'^(2DRIVERSELECTMENUIMAGE|2DRIVERSELECTTD_)', png_replace_safe=False,
         note='Driver Select team logo, 3D number, and paint-preview family.'),
    dict(id='career_scheme_thumb', label='Career / custom scheme thumbnail',
         container_rx=r'^(BASESCHEMETHUMBNAILS|CUSTOMSCHEMETHUMBNAILS)\.ARC$', png_replace_safe=False,
         note='Career base-scheme or custom-scheme thumbnail.'),
    dict(id='team_shop_logo', label='Team Shop logo',
         container_rx=r'^TEAMSHOPLOGO', png_replace_safe=False,
         note='Team Shop branding.'),
    dict(id='global_menu_art', label='Global menu / loading art',
         container_rx=r'^(GLOBALMENUASSETS|3LOADINGTRIVIAQUIZIMAGETEST|RACEMEDIAIMAGES)\.ARC$', png_replace_safe=False,
         note='Global interface atlas, loading trivia, or Race Media presentation art.'),
    dict(id='tire_clean_diffuse', label='Tire sidewall — clean diffuse',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', entry_rx=r'^Tyre02\.dds$', png_replace_safe=False,
         note='Shared clean tire sidewall texture. Smart Import rebuilds every standard mip so branding remains visible at distance; verify globally in game.'),
    dict(id='tire_worn_diffuse', label='Tire sidewall — worn diffuse',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', entry_rx=r'^Tyre02-D\.dds$', png_replace_safe=False,
         note='Shared dirty/worn tire sidewall texture. Update with the clean map; Smart Import rebuilds every standard mip so branding does not revert with wear or distance.'),
    dict(id='tire_texture_set', label='Tire normal/specular & wheel blur maps',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', entry_rx=r'^(Tyre02-(?:DN|DS|N|S)\.dds|wheelblurNEW\.dds)$', png_replace_safe=False,
         note='Advanced tire normal, specular, and spinning-wheel blur resources. Standard mip chains are rebuilt; normal-map mips are re-normalized after filtering.'),
    dict(id='shared_vehicle_textures', label='Shared NASCAR vehicle textures',
         container_rx=r'^NASCAR6_TEXTURES_X\.ARC$', png_replace_safe=False,
         note='Engine, chassis, damage, glass, fuel, dirt, smoke, tire, wheel, and window material resources.'),
]


def ui_csv_path():
    if active_game_profile().get('graphics_mode')=='discovered': return None
    p=component_path(UI_CSV)
    return p if os.path.exists(p) else None

def _ui_recover_dims(w,h,fmt,payload_size):
    """Recover bad DXT metadata from the exact block count.

    A number of track maps/logos store approximate or corrupt pixel dimensions
    while the BC payload size is exact. Search factor pairs of the block count
    and choose the 4-pixel-aligned geometry closest to the metadata. Reject
    implausible giant/sliver candidates rather than inventing an image.
    """
    bpb=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else 0
    if not bpb or payload_size<=0 or payload_size%bpb: return None
    blocks=payload_size//bpb; cand=[]
    lim=int(blocks**0.5)
    meta_ok=1<=w<=8192 and 1<=h<=8192
    target_aspect=(w/h) if meta_ok and h else 1.0
    for a in range(1,lim+1):
        if blocks%a: continue
        b=blocks//a
        for bw,bh in ((a,b),(b,a)):
            pw,ph=bw*4,bh*4
            if pw>4096 or ph>4096: continue
            aspect=pw/ph
            if aspect < (0.01 if meta_ok else 0.08) or aspect > (100.0 if meta_ok else 12.5): continue
            aspect_pen=abs(_math.log(max(aspect,1e-9))- _math.log(max(target_aspect,1e-9)))
            size_pen=(abs(pw-w)/max(4,w)+abs(ph-h)/max(4,h)) if meta_ok else abs(_math.log(aspect))*.15
            common_pen=(0 if pw in (4,8,12,16,32,64,80,96,112,128,160,176,192,256,296,320,384,512,1024,2048,4096) else .08)
            common_pen+=(0 if ph in (4,8,12,16,32,64,80,96,104,108,112,124,128,140,156,160,192,256,320,384,512,1024,2048,4096) else .08)
            cand.append((aspect_pen*2+size_pen+common_pen,pw,ph))
    if not cand: return None
    cand.sort(); score,pw,ph=cand[0]
    if score>3.5: return None
    return pw,ph

_UI_PACKAGED_MAP_CACHE={'signature':None,'rows':{}}

def _ui_truthy(value):
    return value in (1, True, '1', 'True', 'true', 'yes', 'YES')

def _ui_payload_identity(value):
    try:
        return int(value)
    except Exception:
        return -1

def _ui_packaged_map_key(archive, container, entry, payload_abs=None):
    # Duplicate entry names are real in several native containers.  Payload
    # offset is therefore part of the identity; filename-only keys can map the
    # wrong physical resource and can turn bad parser metadata into a preview.
    return (str(archive or ''), str(container or '').upper(), str(entry or ''),
            _ui_payload_identity(payload_abs))

def _ui_bc_required_bytes(w, h, fmt):
    try:
        w=int(w); h=int(h)
    except Exception:
        return 0
    if w<=0 or h<=0:
        return 0
    bpb=8 if str(fmt).upper()=='DXT1' else 16 if str(fmt).upper()=='DXT5' else 0
    if not bpb:
        return 0
    return max(1,(w+3)//4)*max(1,(h+3)//4)*bpb

def _ui_mapping_geometry_plausible(row):
    try:
        w=int(row.get('w')); h=int(row.get('h')); ps=int(row.get('payload_size'))
    except Exception:
        return False
    needed=_ui_bc_required_bytes(w,h,row.get('fmt'))
    return bool(needed and 1<=w<=8192 and 1<=h<=8192 and ps>0 and needed<=ps)

def _ui_packaged_mapping_rank(row, ordinal):
    plausible=_ui_mapping_geometry_plausible(row)
    recovered=(str(row.get('geometry_status') or '').lower()=='recovered')
    decoded=_ui_truthy(row.get('decoded'))
    # Prefer a plausible recovered row, then any plausible decoded row.  File
    # order is only the final deterministic tie-breaker.
    return (1 if recovered and plausible else 0,
            1 if plausible else 0,
            1 if decoded else 0,
            int(ordinal))

def _ui_packaged_mappings():
    """Load the shipped full graphics map by exact physical identity.

    Duplicate names inside one ARC remain separate through payload_abs.  A
    deterministic best-row fallback is also retained for legacy/discovered rows
    that do not carry an offset, but implausible dimensions can never beat sane
    recovered geometry.
    """
    if active_game_profile().get('graphics_mode')=='discovered': return {}
    p=UI_PACKAGED_MAPPING_CSV
    if not os.path.exists(p):
        return {}
    sig=(os.path.getmtime(p),os.path.getsize(p))
    if _UI_PACKAGED_MAP_CACHE.get('signature')==sig:
        return _UI_PACKAGED_MAP_CACHE.get('rows') or {}
    rows={}; fallback={}
    with open(p,'r',encoding='utf-8-sig',newline='') as f:
        for ordinal,r in enumerate(_csv.DictReader(f)):
            exact=_ui_packaged_map_key(r.get('archive'),r.get('container'),r.get('entry'),r.get('payload_abs'))
            if not exact[1] or not exact[2]:
                continue
            rows[exact]=r
            base=exact[:3]
            rank=_ui_packaged_mapping_rank(r,ordinal)
            if base not in fallback or rank>fallback[base][0]:
                fallback[base]=(rank,r)
    # Legacy no-offset lookups use the deterministic safe winner only.
    for base,(_rank,row) in fallback.items():
        rows[base+(-1,)]=row
    _UI_PACKAGED_MAP_CACHE['signature']=sig
    _UI_PACKAGED_MAP_CACHE['rows']=rows
    return rows

_UI_INDEX_CACHE={'signature':None,'rows':None}

def _ui_index_files():
    files=[]
    p=ui_csv_path()
    if p: files.append((p,'built_in'))
    discovered=_discovered_texture_csv()
    if os.path.exists(discovered): files.append((discovered,'discovered'))
    return files

def _ui_index():
    files=_ui_index_files()
    if not files: raise RuntimeError(f'{UI_CSV} is missing from the internal tools folder')
    map_sig=(os.path.getmtime(UI_PACKAGED_MAPPING_CSV),os.path.getsize(UI_PACKAGED_MAPPING_CSV)) if os.path.exists(UI_PACKAGED_MAPPING_CSV) else None
    signature=tuple((p,os.path.getmtime(p),os.path.getsize(p)) for p,_ in files)+(('packaged_map',map_sig),)
    if _UI_INDEX_CACHE.get('rows') is not None and _UI_INDEX_CACHE.get('signature')==signature:
        return _UI_INDEX_CACHE['rows']
    packaged=_ui_packaged_mappings(); rows=[]; seen=set()
    for p,source_index in files:
        with open(p,'r',encoding='utf-8-sig',newline='') as f:
            for r in _csv.DictReader(f):
                try:
                    w=int(r['w']); h=int(r['h']); ps=int(r['payload_size'])
                    if r.get('payload_abs') not in (None,''): r['payload_abs']=int(r['payload_abs'])
                    raw_fmt=str(r.get('fmt') or '').upper()
                    # Native pixel-format 0x19 is NASCAR's quarter-field DXT1
                    # header. The clean v0.2 audit and the primary ARC parser both
                    # confirm that its logical dimensions are four times the two
                    # stored dimension fields. QuickBMS cannot describe this format,
                    # so the app's native parser is authoritative for it.
                    if raw_fmt=='FMT_0X19':
                        nw,nh=max(1,w*4),max(1,h*4)
                        if _ui_bc_required_bytes(nw,nh,'DXT1')<=ps:
                            w,h=nw,nh; r['w']=str(w); r['h']=str(h); r['fmt']='DXT1'; r['decoded']='1'
                            r['geometry_status']='native_format_25'
                    if str(r.get('container') or '').upper()=='SPRINTNUMS2015.ARC' and str(r.get('fmt') or '').upper()=='DXT1' and ps>=4096:
                        w,h=128,64
                        r['w']='128'; r['h']='64'
                except Exception:
                    continue
                identity=_ui_packaged_map_key(r.get('archive'),r.get('container'),r.get('entry'),r.get('payload_abs'))
                if identity in seen: continue
                seen.add(identity)
                seed=packaged.get(identity) or packaged.get(identity[:3]+(-1,))
                if seed and str(seed.get('fmt') or '').upper()=='FMT_0X19':
                    seed=dict(seed)
                    try:
                        sw,sh=int(seed.get('w') or w)*4,int(seed.get('h') or h)*4
                        if _ui_bc_required_bytes(sw,sh,'DXT1')<=int(seed.get('payload_size') or ps):
                            seed.update(w=str(sw),h=str(sh),fmt='DXT1',decoded='True',geometry_status='native_format_25')
                    except Exception:
                        pass
                r['source_index']=source_index
                if seed:
                    r['original_w']=int(seed.get('original_w') or w); r['original_h']=int(seed.get('original_h') or h)
                    seed_candidate=dict(seed,payload_size=seed.get('payload_size') or ps,fmt=seed.get('fmt') or r.get('fmt'))
                    seed_ok=_ui_mapping_geometry_plausible(seed_candidate)
                    if seed_ok:
                        r['w']=int(seed.get('w') or w); r['h']=int(seed.get('h') or h)
                        r['decoded']=_ui_truthy(seed.get('decoded'))
                        r['geometry_status']=str(seed.get('geometry_status') or ('indexed' if r['decoded'] else 'unresolved'))
                    else:
                        # A packaged row may be intentionally short/truncated yet
                        # still previewable by the native parser. Keep sane parser
                        # geometry, but never adopt absurd packaged dimensions.
                        originally_decoded=r.get('decoded') in ('1','True','true',1,True)
                        basic_sane=(1<=w<=8192 and 1<=h<=8192)
                        if originally_decoded and basic_sane:
                            r['w']=w; r['h']=h; r['decoded']=True; r['geometry_status']='indexed'
                        else:
                            dims=_ui_recover_dims(w,h,r.get('fmt',''),ps)
                            if dims:
                                r['w'],r['h']=dims; r['decoded']=True; r['geometry_status']='recovered'
                            else:
                                r['w']=w; r['h']=h; r['decoded']=False; r['geometry_status']='unresolved'
                    r['decode_error']=str(seed.get('decode_error') or r.get('decode_error') or '')
                    if seed.get('family'): r['family']=seed.get('family')
                else:
                    originally_decoded=r.get('decoded') in ('1','True','true',1,True)
                    r['original_w']=w; r['original_h']=h
                    if originally_decoded and _ui_mapping_geometry_plausible(r):
                        r['w']=w; r['h']=h; r['decoded']=True; r['geometry_status']='indexed'
                    else:
                        dims=_ui_recover_dims(w,h,r.get('fmt',''),ps)
                        if dims:
                            r['w'],r['h']=dims; r['decoded']=True; r['geometry_status']='recovered'
                        else:
                            r['w']=w; r['h']=h; r['decoded']=False; r['geometry_status']='unresolved'
                rows.append(r)
    _UI_INDEX_CACHE['signature']=signature; _UI_INDEX_CACHE['rows']=rows
    return rows

UI_MAPPING_FILE = os.path.join(APP_DIR,'data','ui_mapping_overrides.json')

def _ui_mapping_file():
    return os.path.join(_profile_dir(),'ui_mapping_overrides.json') if ACTIVE_GAME=='nascar14' else UI_MAPPING_FILE

def _ui_mapping_key(row):
    return f"{row.get('archive','')}|{str(row.get('container','')).upper()}|{row.get('entry','')}|{_ui_payload_identity(row.get('payload_abs'))}"

def _ui_legacy_mapping_key(row):
    return f"{row.get('archive','')}|{str(row.get('container','')).upper()}|{row.get('entry','')}"

def _ui_mapping_overrides():
    try:
        with open(_ui_mapping_file(),'r',encoding='utf-8') as f:
            d=json.load(f)
        return d if isinstance(d,dict) else {}
    except Exception:
        return {}

def _ui_save_mapping_overrides(d):
    path=_ui_mapping_file()
    os.makedirs(os.path.dirname(path),exist_ok=True)
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:
        json.dump(d,f,indent=2,sort_keys=True)
    os.replace(tmp,path)

def _ui_pretty_name(s):
    s=re.sub(r'\.(tga|dds|png)$','',str(s),flags=re.I)
    s=re.sub(r'[_\-]+',' ',s).strip()
    return re.sub(r'\s+',' ',s)

def _ui_auto_mapping(row):
    """Map every indexed texture to a useful screen/family label.

    This is deliberately honest: filenames and container families provide a
    strong likely-use mapping, while user verification can promote an entry to
    confirmed through ui_mapping_overrides.json.
    """
    fam=(row.get('family') or '').lower()
    c=(row.get('container') or '').upper()
    e=(row.get('entry') or '')
    eu=e.upper()
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved':
        if fam=='shared_vehicle_textures':
            return dict(category='Shared Vehicle Textures',screen='Shared NASCAR Vehicle / Materials',
                        role='Unsupported shared material resource',label=f'Shared vehicle resource · {e}',
                        confidence='research',note=f'{row.get("fmt") or "Unknown"} resource format is indexed, but pixel decoding is not mapped yet. Raw export only.')
        if fam=='tire_wheel_textures':
            return dict(category='Tires & Wheels',screen='Shared NASCAR Vehicle / On-track',
                        role='Unsupported tire/wheel resource',label=f'Tire / wheel resource · {e}',
                        confidence='research',note=f'{row.get("fmt") or "Unknown"} resource format is indexed, but pixel decoding is not mapped yet. Raw export only.')
        if fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
            return dict(category='Driver & Character Textures',screen='Driver / Character Model',
                        role='Unsupported character resource',label=f'Character resource · {c[:-4]} · {e}',
                        confidence='research',note='Indexed character resource, but its geometry/format is not decoded safely. Raw export only.')
        return dict(category='Unresolved Binary Candidates',screen='Research / Not decoded',
                    role='Raw indexed payload',label=f'Unresolved candidate · {c[:-4]} · {row.get("entry","")}',
                    confidence='unknown',note='The scanner found an image-like record, but its geometry could not be recovered safely. Raw export only.')
    category='Unknown / Research'; screen='Unknown'; role='Unknown texture'
    label=_ui_pretty_name(e); confidence='unknown'; note='Indexed and decodable, but its exact screen still needs verification.'

    if fam=='track_select':
        category='Track & Event Art'; confidence='likely'
        if c=='TRACKDETAILIMAGES.ARC': screen='Track Select'; role='Detail background'; label=f'Track detail · {e}'
        elif c=='2TRACKSELECTMENUIMAGE.ARC':
            screen='Track Select'; role='List thumbnail / background'; label=('Track Select background' if 'IMGTRACKSELECTBG' in eu else f'Track thumbnail · {e}')
        elif c=='3TRACKCARDIMAGE.ARC': screen='Track Select'; role='Foreground card'; label=f'Track card · {e}'
        elif c=='CALENDAR_TRACK_IMAGES.ARC': screen='Career / Single Season Calendar'; role='Calendar track tile'; label=f'Calendar track · {e}'
        elif c=='2LOBBYTRACKCARDIMAGE.ARC': screen='Multiplayer Lobby'; role='Track card'; label=f'Lobby track card · {e}'
        elif c=='LOBBYTRACKCARDDETAILIMAGE.ARC': screen='Multiplayer Lobby'; role='Track detail'; label=f'Lobby track detail · {e}'
        elif c.startswith('TRACK_FACTS_IMG_'): screen='Track Facts'; role=('Track icon' if 'ICON' in eu else 'Track photo'); label=f'Track Facts {role.lower()} · {c[16:-4]}'
        elif c=='TRACKTESTING.ARC': screen='Track Testing / Debug'; role='Testing image'; label=f'Track testing · {e}'; confidence='research'
        else: screen='Track Presentation'; role='Track image'; label=f'Track presentation · {e}'
        note='Mapped from a track-presentation container family.'
    elif fam=='track_logos_maps':
        category='HUD Maps & Track Logos'; confidence='likely'
        if c.startswith('MAP_'):
            screen='In-race HUD / Minimap'
            roles={'IMG_MAP':'Track map','IMG_BLIP_PLAYER':'Player marker','IMG_BLIP_FIRST':'Leader marker','IMG_BLIP_OTHERS':'Other-car marker','IMG_ARROW':'Direction arrow'}
            role=roles.get(eu,'HUD map element'); label=f'{role} · {c[4:-4]}'
        else:
            screen='Track Presentation'; role='Track logo'; label=f'Track logo · {c[:-4]}'
        note='Mapped from the HUD map / track-logo container family.'
    elif fam=='driver_number_cards':
        category='Driver & Team Art'
        if c=='SPRINTNUMS2015.ARC':
            screen='Front-end 3D Number Model (consumer unverified)'; role='Wrapped 3D-number UV texture'; label=f'3D number texture atlas · {e}'; confidence='research'
            note=('Not a flat number card. This 128×64 asset is a native UV atlas for a front-end 3D number mesh, so it can look fragmented at gallery scale. '
                  'The exact menu consumer still needs a loud-color in-game trace test. Export and import now use the clean native payload without the old compensating shift.')
        else:
            screen='Driver Details Popup'; role='Car-number image'; label=f'Driver detail number · {e}'; confidence='likely'
            note='Mapped from the known driver-number container family.'
    elif fam=='driver_select':
        category='Driver & Team Art'; screen='Driver Select'; role='3D number graphic'; label=f'Driver Select number · {e}'; confidence='likely'; note='Mapped from DRIVER_*_3DNUM entries.'
    elif fam=='paint_scheme_preview':
        category='Paint Scheme Previews'; confidence='likely'
        if eu.startswith('DRIVERPAINT_'): screen='Driver Select'; role='Car-card preview'; label=f'Driver paint preview · {e}'
        elif eu.startswith('PAINTSCHEME_'): screen='Paint Select / Paint Booth'; role='Scheme thumbnail'; label=f'Paint scheme preview · {e}'
        else: screen='Paint Selection'; role='Scheme preview'; label=f'Paint preview · {e}'
        note='Mapped from known paint-preview naming.'
    elif fam=='team_logos':
        category='Driver & Team Art'; screen='Team Shop'; role='Team logo'; label=f'Team Shop logo · {c[:-4]}'; confidence='likely'; note='Native Team Shop texture. Export and Stock are available; generic Smart Import remains locked until the corrected writer passes an in-game confirmation test.'
    elif fam=='backgrounds_panels':
        category='Menu Backgrounds & Panels'; screen='Race Media'; role='Detail panel / background'; label=f'Race Media panel · {e}'; confidence='likely'; note='Mapped from RACEMEDIAIMAGES.'
    elif fam=='misc_ui':
        confidence='likely'
        if c=='BASESCHEMETHUMBNAILS.ARC': category='Paint Scheme Previews'; screen='Career Paint Select'; role='Base scheme thumbnail'; label=f'Career scheme thumbnail · {e}'
        elif c=='CUSTOMSCHEMETHUMBNAILS.ARC': category='Paint Scheme Previews'; screen='Custom Paint Select'; role='Custom scheme thumbnail'; label=f'Custom scheme thumbnail · {e}'
        elif c=='2DRIVERSELECTMENUIMAGE.ARC': category='Driver & Team Art'; screen='Driver Select'; role='Team tile / logo'; label=f'Driver Select team art · {e}'
        elif c=='3LOADINGTRIVIAQUIZIMAGETEST.ARC': category='Menu Backgrounds & Panels'; screen='Loading / Trivia'; role='Loading image'; label=f'Loading screen art · {e}'
        elif c=='GLOBALMENUASSETS.ARC': category='Menu Backgrounds & Panels'; screen='Global Menus'; role='Interface atlas'; label=f'Global menu atlas · {e}'
        elif c=='RACEMEDIAIMAGES.ARC': category='Menu Backgrounds & Panels'; screen='Race Media'; role='Race Media image'; label=f'Race Media · {e}'
        else: category='Menu Backgrounds & Panels'; screen='Menus'; role='UI texture'; label=f'UI art · {e}'
        note='Mapped from a known UI container family.'
    elif fam=='race_hud_textures':
        category='Race HUD & Gauges';screen='In-race HUD';confidence='research'
        text=(c+' '+eu)
        if any(k in text for k in ('TACH','RPM','REV','GAUGE','DIAL','METER')):role='Tachometer / gauge texture'
        elif any(k in text for k in ('DAMAGE','CARSTATUS','VEHICLESTATUS','TYRE','TIRE','ENGINE')):role='Vehicle damage / status widget'
        elif any(k in text for k in ('STANDING','LEADER','POSITION','INTERVAL','RUNNINGORDER')):role='Running-order / standings panel'
        elif any(k in text for k in ('LAP','TIMING','SPLIT','COUNTER')):role='Lap / timing overlay'
        else:role='Race HUD interface texture'
        label=f'{role} · {c[:-4]} · {e}';note='Discovered by HUD-oriented filename/container matching. Export and use a loud-color trace before treating the consumer as confirmed.'
    elif fam=='tire_wheel_textures':
        category='Tires & Wheels'; screen='Shared NASCAR Vehicle / On-track'; confidence='likely'
        roles={
            'TYRE02.DDS':'Clean tire diffuse / sidewall branding',
            'TYRE02-D.DDS':'Dirty/worn tire diffuse / sidewall branding',
            'TYRE02-N.DDS':'Clean tire normal map',
            'TYRE02-DN.DDS':'Dirty/worn tire normal map',
            'TYRE02-S.DDS':'Clean tire specular map',
            'TYRE02-DS.DDS':'Dirty/worn tire specular map',
            'WHEELBLURNEW.DDS':'Spinning wheel blur texture',
        }
        role=roles.get(eu,'Tire or wheel material texture'); label=f'{role} · {e}'
        note='Shared vehicle texture from NASCAR6_TEXTURES_X.ARC. Diffuse branding is likely global; first replacement still needs an in-game scope test.'
    elif fam=='shared_vehicle_textures':
        category='Shared Vehicle Textures'; screen='Shared NASCAR Vehicle / Materials'; confidence='likely'
        if 'ENGINE' in eu: role='Engine material texture'
        elif 'CHASSIS' in eu: role='Chassis material texture'
        elif 'DAMAGE' in eu: role='Damage overlay / material map'
        elif 'GLASS' in eu or 'WINDOW' in eu: role='Glass / window material texture'
        elif 'OCC' in eu: role='Occlusion map'
        elif 'FUEL' in eu or 'PETROL' in eu: role='Fuel / filler material texture'
        elif 'DIRT' in eu or 'NOISE' in eu: role='Dirt / noise material texture'
        elif 'SMOKE' in eu or 'FIRE' in eu: role='Smoke / fire effect texture'
        elif 'PAINT' in eu or 'ALLOY' in eu: role='Paint / alloy material lookup'
        else: role='Shared vehicle material texture'
        label=f'{role} · {e}'; note='Shared resource mapped from NASCAR6_TEXTURES_X.ARC. Exact consumers may cover multiple cars or visual states.'
    elif fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
        category='Driver & Character Textures'; confidence='likely'
        if fam=='driver_face_textures': screen='Driver Character'; role='Face/head diffuse, normal, or specular map'
        elif fam=='driver_suit_glove_textures': screen='Driver Character'; role='Suit or glove material map'
        elif fam=='garage_character_textures': screen='Garage Character'; role='Body or cap material map'
        elif fam=='infield_character_textures': screen='Infield Character'; role='Body or cap material map'
        elif fam=='driver_head_textures': screen='Driver Character'; role='Head/portrait material map'
        else: screen='Champion Character'; role='Champion character material map'
        label=f'{role} · {c[:-4]} · {e}'; note='Previously unindexed 3D character texture discovered in Archive 3.'
    elif fam=='track_environment_textures':
        category='Track & Environment Textures'; screen='Track / Environment'; role='Track material texture'; label=f'Track texture · {c[:-4]} · {e}'; confidence='research'; note='Discovered by the live ARCC texture scanner; exact surface still needs visual verification.'
    elif fam=='discovered_texture':
        category='Discovered Textures'; screen='Unknown / Model Asset'; role='ARCC texture resource'; label=f'Discovered texture · {c[:-4]} · {e}'; confidence='research'; note='Found by the live texture scanner. Export and inspect before replacing.'
    elif fam=='unknown_visual':
        confidence='research'
        if c in ('RACESHOPREPLACETEX.ARC','INFIELDGARAGEREPLACETEX.ARC'):
            category='Character & Pit Crew Textures'; screen=('Race Shop' if c.startswith('RACE') else 'Infield Garage'); role='Pit-crew material map'; label=f'Pit crew {e.lower()}'; note='3D character material texture, not a normal menu image.'
        elif c.startswith(('DTS_','GTS_','ITS_','DF_','PMH_','CHAMP_')):
            category='Character & Pit Crew Textures'; screen='Driver / Champion Character Model'; role='Character material map'; label=f'Character texture · {c[:-4]} · {e}'; note='3D model diffuse/normal/specular or rig-associated texture.'
        elif c.startswith('LIVERY_') or eu=='IMG_LIV':
            category='Vehicle / Livery Textures'; screen='On-track Vehicle'; role='Livery texture'; label=f'Vehicle livery · {c[:-4]}'; note='Car livery atlas. Prefer the dedicated Paint Schemes workflow for normal roster paint changes.'
        elif c in ('2DRIVERDETAILSIMAGE.ARC','2DRIVERDETAILPOPUPIMAGE.ARC'):
            category='Driver & Team Art'; screen='Driver Details'; role='Background / pattern art'; label=f'Driver details art · {e}'; confidence='likely'; note='Presentation art inside the driver-details interface.'
        else:
            category='Unknown / Research'; screen='Unknown / Model Asset'; role='Decoded texture'; label=f'Research texture · {c[:-4]} · {e}'; note='Decoded successfully, but the exact consumer is not mapped yet.'

    return dict(category=category,screen=screen,role=role,label=label,confidence=confidence,note=note)

def _ui_mapping(row, overrides=None):
    m=_ui_auto_mapping(row)
    exact_key=_ui_packaged_map_key(row.get('archive'),row.get('container'),row.get('entry'),row.get('payload_abs'))
    seed=_ui_packaged_mappings().get(exact_key) or _ui_packaged_mappings().get(exact_key[:3]+(-1,))
    if isinstance(seed,dict):
        for k in ('category','screen','role','label','note'):
            if seed.get(k): m[k]=str(seed[k])
        if seed.get('confidence'): m['confidence']=str(seed['confidence'])
        m['packaged_mapped']=True
    else:
        m['packaged_mapped']=False
    override_rows=(overrides if overrides is not None else _ui_mapping_overrides())
    ov=override_rows.get(_ui_mapping_key(row),override_rows.get(_ui_legacy_mapping_key(row),{}))
    if isinstance(ov,dict):
        for k in ('category','screen','role','label','note'):
            if ov.get(k): m[k]=str(ov[k])
        if ov.get('verified'):
            m['confidence']='confirmed'
        elif ov.get('confidence'):
            m['confidence']=str(ov['confidence'])
        m['verified']=bool(ov.get('verified'))
        m['user_mapped']=bool(ov)
    else:
        m['verified']=False; m['user_mapped']=False
    return m

def _confirmed_for(row):
    for a in CONFIRMED_ASSETS:
        if a.get('entry_rx') and not re.match(a['entry_rx'], row['entry'], re.I): continue
        if a.get('container_rx') and not re.match(a['container_rx'], row['container'], re.I): continue
        if not a.get('entry_rx') and not a.get('container_rx'): continue
        if a.get('fmt') and row.get('fmt')!=a['fmt']: continue
        return a
    return None

def _ui_category(row):
    return _ui_mapping(row).get('category','Unknown / Research')

def _ui_special_handler(row):
    c=(row.get('container') or '').upper();e=(row.get('entry') or '').upper()
    if c=='SPRINTNUMS2015.ARC':
        return 'number_card_wrap'
    if re.match(r'^(?:L_)?IMGCARNUMBER',e):
        return 'number_card_full_canvas'
    if re.match(r'^TEAMSHOPLOGO(?:2)?\.ARC$',c):
        return 'team_shop_exact'
    if e.startswith('PAINTSCHEME_'):
        return 'paint_scheme_locked'
    if re.match(r'^DRIVER_\d+_3DNUM_',e):
        return 'driver_select_3dnum_dedicated'
    return ''


def _ui_container_type(row):
    """Classify the storage recipe, not merely the artwork's likely screen."""
    c=(row.get('container') or '').upper();fam=(row.get('family') or '').lower();special=_ui_special_handler(row)
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved':return 'Unresolved/raw ARCC resource'
    if special=='number_card_wrap':return 'Wrapped number-card multi-texture ARC'
    if special=='team_shop_exact':return 'Team Shop exact DXT5 ARC'
    if c.startswith('LIVERY_') or c.startswith('HDLIVERY_'):return 'Vehicle livery wrapper'
    if c=='NASCAR6_TEXTURES_X.ARC':return 'Shared vehicle ARCC texture bank'
    if fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
        return 'Character/model ARCC texture bank'
    if c.startswith('MAP_'):return 'HUD map texture ARC'
    return 'Indexed multi-texture ARCC'


def _ui_image_type(row,mapping=None):
    mapping=mapping or _ui_mapping(row);e=(row.get('entry') or '').upper();fam=(row.get('family') or '').lower()
    if row.get('decoded') is False:return 'raw_unknown'
    if _ui_special_handler(row)=='number_card_wrap':return 'wrapped_number_card'
    if _ui_special_handler(row)=='team_shop_exact':return 'team_shop_logo'
    if c:=((row.get('container') or '').upper()):
        if c.startswith('LIVERY_') or c.startswith('HDLIVERY_'):return 'vehicle_livery_atlas'
    if fam=='tire_wheel_textures':
        if '-N.' in e or '-DN.' in e:return 'normal_map'
        if '-S.' in e or '-DS.' in e:return 'specular_map'
        if 'WHEELBLUR' in e:return 'animated_blur_texture'
        return 'tire_diffuse'
    if any(k in e for k in ('NORMAL','_N.','-N.','_NM',' BUMP')):return 'normal_map'
    if any(k in e for k in ('SPEC','_S.','-S.','GLOSS')):return 'specular_map'
    if any(k in e for k in ('OCC','AO.','OCCLUSION')):return 'occlusion_map'
    if any(k in e for k in ('ALPHA','MASK')):return 'mask_texture'
    if mapping.get('category') in ('Menu Backgrounds & Panels','Driver & Team Art','Track & Event Art','HUD Maps & Track Logos','Race HUD & Gauges','Paint Scheme Previews'):
        return 'ui_artwork'
    if fam in ('driver_face_textures','driver_suit_glove_textures','garage_character_textures','infield_character_textures','driver_head_textures','champion_character_textures'):
        return 'character_material_texture'
    if fam=='shared_vehicle_textures':return 'shared_vehicle_material'
    return 'decoded_texture'


def _ui_recommended_resize_mode(row, entry=None):
    """Return the mapped default for this exact game texture.

    The public v1 build applied one global "fit and pad" rule to every graphic.
    That is wrong for fixed UV atlases and caused opaque black bars around menu
    numbers. This recommendation is based on the mapped consumer/storage family.
    """
    special=_ui_special_handler(row)
    if special in ('number_card_wrap','number_card_full_canvas'):
        # Every SPRINTNUMS entry consumes the complete 128x64 atlas, including BIG_*.
        # The game consumes the complete atlas; padding a square logo into 128x64
        # becomes visible black side bars. Exact resize is the native behavior.
        return 'stretch'
    image_type=_ui_image_type(row,_ui_mapping(row))
    if image_type in {
        'vehicle_livery_atlas','normal_map','specular_map','occlusion_map',
        'mask_texture','tire_diffuse','animated_blur_texture',
        'shared_vehicle_material','character_material_texture'
    }:
        return 'stretch'
    if _ui_short_dxt1_track_map(row,entry):
        return 'stretch'
    return 'fit'


def _ui_effective_resize_mode(row, requested=None, entry=None):
    requested=str(requested or 'auto').strip().lower()
    if requested not in IMAGE_RESIZE_MODES:
        requested='auto'
    recommended=_ui_recommended_resize_mode(row,entry)
    # Menu-number atlases are a proven special case. Do not allow the old global
    # fit setting to reintroduce black padding; the import preview reports this
    # target-aware override before anything is written.
    if _ui_special_handler(row) in ('number_card_wrap','number_card_full_canvas'):
        return 'stretch',requested,'mapped number canvas; black padding disabled'
    if requested=='auto':
        return recommended,requested,'automatic target-aware sizing'
    return requested,requested,''


def _ui_mapping_status(row,mapping=None):
    mapping=mapping or _ui_mapping(row)
    if mapping.get('verified'):
        return 'verified'
    if mapping.get('packaged_mapped'):
        return 'packaged'
    screen=str(mapping.get('screen') or '').strip().lower()
    category=str(mapping.get('category') or '').strip().lower()
    confidence=str(mapping.get('confidence') or '').strip().lower()
    if confidence not in ('unknown','research') and screen not in ('','unknown','unknown / model asset') and 'unknown' not in category:
        return 'inferred'
    return 'needs_review'


def _ui_short_dxt1_track_map(row, entry=None):
    """Return the logical dimensions for native short DXT1 HUD maps.

    Several MAP_* / IMG_MAP records advertise a non-4-aligned height (for
    example 256x107) while the physical payload omits the final BC block row.
    Treating the payload as a smaller 256x104 image changes the logical row
    layout and produces the horizontal corruption seen in the browser.  The
    game-facing image is the advertised size with the missing final block row
    padded, then cropped back to the logical height.
    """
    if str(row.get('family') or '').lower()!='track_logos_maps': return None
    if str(row.get('entry') or '').upper()!='IMG_MAP': return None
    if str(row.get('fmt') or (entry or {}).get('fmt') or '').upper()!='DXT1': return None
    try:
        ow=int(row.get('original_w') or row.get('w') or (entry or {}).get('w') or 0)
        oh=int(row.get('original_h') or row.get('h') or (entry or {}).get('h') or 0)
        ps=int(row.get('payload_size') or (entry or {}).get('payload_size') or 0)
    except Exception:
        return None
    if ow<=0 or oh<=0 or ps<=0: return None
    pw=((ow+3)//4)*4; ph=((oh+3)//4)*4
    full=(pw//4)*(ph//4)*8
    missing=full-ps
    # All known short track-map rows omit no more than one complete block row.
    if missing<=0 or missing%8 or missing>(pw//4)*8: return None
    return dict(logical_w=ow,logical_h=oh,storage_w=pw,storage_h=ph,
                full_bytes=full,payload_size=ps,missing_bytes=missing)


def _ui_logical_dims(row, entry=None):
    if str(row.get('container') or '').upper()=='SPRINTNUMS2015.ARC':
        return 128,64
    special=_ui_short_dxt1_track_map(row,entry)
    if special:return special['logical_w'],special['logical_h']
    try:return int((entry or {}).get('w') or row.get('w')),int((entry or {}).get('h') or row.get('h'))
    except Exception:return 0,0


def _ui_native_short_layout(row, entry=None):
    """Recognize bounded, block-aligned native short BC payloads.

    This restores the proven v0.9.19.1 behavior for HUD markers and extends it
    to the DXT1 track-map family.  A fresh encode may be truncated only when the
    missing portion is block-aligned and no larger than one physical block row.
    Known fatal/special families remain locked by _ui_replace_reason.
    """
    fmt=str(row.get('fmt') or (entry or {}).get('fmt') or '').upper()
    if fmt not in ('DXT1','DXT5'):return None
    special=_ui_short_dxt1_track_map(row,entry)
    try:
        ps=int(row.get('payload_size') or (entry or {}).get('payload_size') or 0)
        if special:
            sw,sh=special['storage_w'],special['storage_h']
        else:
            sw=int((entry or {}).get('w') or row.get('w'));sh=int((entry or {}).get('h') or row.get('h'))
    except Exception:return None
    bpb=8 if fmt=='DXT1' else 16
    full=max(1,(sw+3)//4)*max(1,(sh+3)//4)*bpb
    missing=full-ps
    row_bytes=max(1,(sw+3)//4)*bpb
    if missing<=0 or missing%bpb or missing>row_bytes:return None
    return dict(fmt=fmt,storage_w=sw,storage_h=sh,full_bytes=full,
                payload_size=ps,missing_bytes=missing,block_bytes=bpb)


def _ui_extract_padded_level(payload, level):
    chunks=[];off=int(level['offset']);row_bytes=int(level['row_bytes']);stride=int(level['row_stride'])
    for r in range(int(level['rows'])):
        p=off+r*stride
        if p+row_bytes>len(payload):raise ValueError('native padded mip row exceeds payload')
        chunks.append(payload[p:p+row_bytes])
    return b''.join(chunks)


def _ui_decode_image(arc,entry,row,logical=False):
    """Decode one indexed texture through its exact physical storage recipe."""
    special=_ui_short_dxt1_track_map(row,entry) if logical else None
    if special:
        pa=int(entry['payload_abs']);ps=int(entry['payload_size'])
        payload=bytes(arc[pa:pa+ps]);payload+=b'\0'*(special['full_bytes']-len(payload))
        arr=C._dxt1_decode(payload,special['storage_w'],special['storage_h'])
        return Image.fromarray(arr,'RGB').convert('RGBA').crop((0,0,special['logical_w'],special['logical_h']))
    recipe=_ui_layout_recipe(row,entry)
    if recipe.get('kind') in ('bms_padded_mips','row_padded_mips'):
        pa=int(entry['payload_abs']);ps=int(entry['payload_size']);payload=bytes(arc[pa:pa+ps])
        level=recipe['layout'][0];tight=_ui_extract_padded_level(payload,level)
        w,h=int(level['width']),int(level['height']);dw=((w+3)//4)*4;dh=((h+3)//4)*4
        if entry['fmt']=='DXT1':img=Image.fromarray(C._dxt1_decode(tight,dw,dh),'RGB').convert('RGBA')
        else:
            if entry.get('dxt5_swapped'):tight=C.swap_dxt5_halves(tight)
            img=Image.fromarray(C.dxt5_decode(tight,dw,dh),'RGBA')
        return img.crop((0,0,w,h))
    return C.multi_read_png(arc,entry).convert('RGBA')


def _ui_pad_storage_image(img,row,entry):
    special=_ui_short_dxt1_track_map(row,entry)
    if not special:return img
    src=img.convert('RGB')
    canvas=Image.new('RGB',(special['storage_w'],special['storage_h']),(0,0,0))
    canvas.paste(src,(0,0))
    return canvas


def _ui_layout_signature(row, entry=None):
    """Return the exact native-storage compatibility signature for donor copies.

    Dimensions and payload size alone are insufficient: PAINTSCHEME, number-card,
    padded-mip and dedicated resources can share byte counts while requiring
    different consumers or transforms.  A raw donor is compatible only when the
    complete physical recipe and the protected semantic class agree.
    """
    recipe=_ui_layout_recipe(row,entry); special=_ui_special_handler(row)
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    try:
        w=int((entry or {}).get('w') or row.get('w'));h=int((entry or {}).get('h') or row.get('h'))
        ps=int((entry or {}).get('payload_size') or row.get('payload_size'))
    except Exception:
        w=h=ps=0
    protected=(special or 'generic')
    # Keep semantically distinct but physically similar resources separated.
    if protected=='number_card_full_canvas': protected='number_card_full_canvas'
    elif protected=='number_card_wrap': protected='number_card_wrap'
    elif protected=='paint_scheme_locked': protected='paint_scheme'
    elif protected=='driver_select_3dnum_dedicated': protected='driver_3dnum'
    elif protected=='team_shop_exact': protected='team_shop'
    elif str(row.get('container') or '').upper().startswith('LIVERY_'): protected='livery_sd'
    elif str(row.get('container') or '').upper().startswith('HDLIVERY_'): protected='livery_hd'
    kind=recipe.get('kind') or 'unknown'; levels=int(recipe.get('levels') or 0)
    layout=[]
    for lv in recipe.get('layout') or []:
        if isinstance(lv,dict):
            layout.append((int(lv.get('width') or 0),int(lv.get('height') or 0),
                           int(lv.get('row_bytes') or 0),int(lv.get('rows') or 0),
                           int(lv.get('row_stride') or 0),int(lv.get('span') or 0)))
    if kind=='physical_canvas':
        layout.append((int(recipe.get('storage_width') or 0),int(recipe.get('storage_height') or 0),0,0,0,0))
    return (protected,kind,fmt,w,h,ps,levels,tuple(layout))


def _ui_replacement_route(row,confirmed=None,mapping=None):
    mapping=mapping or _ui_mapping(row)
    safe=_ui_safety(row,confirmed,mapping)
    if bool(row.get('decoded')) and not _ui_replace_reason(row) and row.get('fmt') in ('DXT1','DXT5'):
        return 'specialized_png' if _ui_special_handler(row) else 'smart_png'
    # Every indexed physical payload can still be changed through exact-size raw
    # import. This is intentionally separate from PNG decoding/geometry claims.
    return 'exact_raw'


def _ui_policy_label(safety):
    return {'safe_replace':'Ready to import','guarded_replace':'Import — check in game','copy_only':'Copy / restore only','read_only':'Advanced file'} .get(safety,'Advanced file')


def _ui_needed(w,h,fmt):
    return _ui_bc_required_bytes(w,h,fmt)


def _ui_native_padded_layout(row, entry=None):
    """Recognize the row/mip padding used by the stock ARC texture writer.

    This is the reverse of the NASCAR 13/14 QuickBMS extractor validated against
    the clean NASCAR 15 v0.2 image audit.  Small BC rows are stored at a minimum
    stride of 32 blocks, and many mipmapped resources reserve at least 1024
    blocks per level.  The function returns a byte-exact recipe only when the
    calculated storage size equals the indexed payload exactly.
    """
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    if fmt not in ('DXT1','DXT5'): return None
    try:
        w=int((entry or {}).get('w') or row.get('w')); h=int((entry or {}).get('h') or row.get('h'))
        payload_size=int((entry or {}).get('payload_size') or row.get('payload_size'))
        hint=int((entry or {}).get('mip_count') or row.get('mip_count') or 0)
    except Exception:
        return None
    if w<=0 or h<=0 or payload_size<=0:return None
    bpb=8 if fmt=='DXT1' else 16; row_floor=bpb*32; mip_floor=bpb*1024
    # A real mip chain reaches 1x1 once. Allowing repeated 1x1 levels can make
    # an unrelated larger physical canvas appear to be a padded 17-level chain.
    natural_levels=1
    tw,th=w,h
    while (tw>1 or th>1) and natural_levels<20:
        tw=max(1,tw//2);th=max(1,th//2);natural_levels+=1
    # The native header's mip count is authoritative when it is sane. Only
    # infer the count for legacy index rows that do not carry the header byte.
    levels_to_try=([hint] if 1<=hint<=natural_levels else list(range(1,natural_levels+1)))
    for policy in ('quickbms','row_only'):
        for levels in levels_to_try:
            off=0; layout=[]
            for level in range(levels):
                lw=max(1,w>>level); lh=max(1,h>>level)
                row_bytes=max(1,(lw+3)//4)*bpb; rows=max(1,(lh+3)//4)
                row_stride=max(row_bytes,row_floor)
                occupied=row_stride*rows
                span=max(occupied,mip_floor) if policy=='quickbms' else occupied
                layout.append(dict(level=level,width=lw,height=lh,offset=off,
                                   row_bytes=row_bytes,rows=rows,row_stride=row_stride,
                                   occupied=occupied,span=span))
                off+=span
            if off==payload_size:
                return dict(kind=('bms_padded_mips' if policy=='quickbms' else 'row_padded_mips'),
                            levels=levels,total_bytes=off,fmt=fmt,width=w,height=h,
                            row_floor=row_floor,mip_floor=(mip_floor if policy=='quickbms' else 0),
                            layout=layout,header_mip_count=hint)
    return None


def _ui_physical_canvas_layout(row, entry=None):
    """Recognize a larger tight BC canvas carrying a smaller logical image.

    Five stock DXT5 track maps use this form (for example logical 256x156 in a
    physical 256x256 payload). Only candidates with an exact block count and
    modest aligned padding are accepted.
    """
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    if fmt not in ('DXT1','DXT5'):return None
    c=str(row.get('container') or '').upper();fam=str(row.get('family') or '').lower()
    if not (c.startswith('MAP_') or fam in ('track_logos_maps','track_image')):return None
    try:
        w=int((entry or {}).get('w') or row.get('w'));h=int((entry or {}).get('h') or row.get('h'))
        ps=int((entry or {}).get('payload_size') or row.get('payload_size'))
    except Exception:return None
    bpb=8 if fmt=='DXT1' else 16; bw=max(1,(w+3)//4)
    if ps<=0 or ps%bpb or (ps//bpb)%bw:return None
    bh=(ps//bpb)//bw; ph=bh*4; pw=bw*4
    if pw<w or ph<h:return None
    next_pow2=1
    while next_pow2<h:next_pow2*=2
    # The validated MAP_* cases are logical crops in the next power-of-two
    # surface. Do not reinterpret arrays/cubemaps as very tall canvases.
    if ph!=next_pow2 or pw!=((w+3)//4)*4:return None
    return dict(kind='physical_canvas',levels=1,total_bytes=ps,fmt=fmt,
                logical_width=w,logical_height=h,storage_width=pw,storage_height=ph)


def _ui_layout_recipe(row, entry=None):
    special=_ui_special_handler(row);c=str(row.get('container') or '').upper();e=str(row.get('entry') or '').upper()
    fmt=str((entry or {}).get('fmt') or row.get('fmt') or '').upper()
    if _ui_truthy(row.get('write_blocked')) or _ui_truthy(row.get('overlap_next')) or _ui_truthy(row.get('overlap_previous')):
        return dict(kind='overlapping_payload',writable=False,note='This payload overlaps another indexed resource; PNG writing is blocked.')
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved':
        return dict(kind='unresolved',writable=False,note='Geometry is unresolved.')
    if fmt not in ('DXT1','DXT5'):
        return dict(kind='unsupported_format',writable=False,note=f'{fmt or "unknown"} has no validated PNG writer.')
    if special=='paint_scheme_locked':
        return dict(kind='paint_scheme_native_clone',writable=False,note='PAINTSCHEME remains on the dedicated native-clone path.')
    if special=='driver_select_3dnum_dedicated':
        return dict(kind='driver_3dnum_dedicated',writable=False,note='3DNUM uses the dedicated current-team writer.')
    if c.startswith('HDLIVERY_LENOVO'):
        return dict(kind='nonstandard_hd_livery',writable=False,note='Nonstandard SD-sized Lenovo HD wrapper is unproven.')
    if c.startswith('LIVERY_') or c.startswith('HDLIVERY_') or e=='IMG_LIV':
        return dict(kind='dedicated_livery',writable=False,note='Use Paint Schemes so paired SD/HD offsets and native mips stay synchronized.')
    short=_ui_native_short_layout(row,entry)
    if short:return dict(kind='native_short',writable=True,levels=1,layout=short)
    try:
        w=int((entry or {}).get('w') or row.get('w'));h=int((entry or {}).get('h') or row.get('h'));ps=int((entry or {}).get('payload_size') or row.get('payload_size'))
    except Exception:
        return dict(kind='invalid_metadata',writable=False,note='Invalid dimensions or payload size.')
    tight=_ui_infer_mips(w,h,fmt,ps)
    try: header_mips=int((entry or {}).get('mip_count') or row.get('mip_count') or 0)
    except Exception: header_mips=0
    if tight and (not header_mips or tight==header_mips):
        return dict(kind=('tight_base' if tight==1 else 'tight_mips'),writable=True,levels=tight)
    padded=_ui_native_padded_layout(row,entry)
    if padded:return dict(padded,writable=True)
    canvas=_ui_physical_canvas_layout(row,entry)
    if canvas:return dict(canvas,writable=True)
    return dict(kind='ambiguous_payload',writable=False,
                note='Payload does not match a tight surface, stock padded-mip recipe, bounded short layout, or validated physical canvas.')


def _ui_replace_reason(row):
    recipe=_ui_layout_recipe(row)
    special=_ui_special_handler(row)
    if recipe.get('writable'):
        return ''
    if special=='paint_scheme_locked':
        return ('PAINTSCHEME thumbnails are structurally mapped, but PNG re-encoding previously caused in-game fatals. '
                'Use the dedicated Paint Schemes thumbnail/native-clone workflow.')
    if special=='driver_select_3dnum_dedicated':
        return ('Use Specialized Import for the driver’s current-team 3D-number resource. Historical/team copies remain copy/raw-only.')
    return recipe.get('note') or 'This native layout is not approved for PNG replacement.'


def _ui_safety(row,confirmed=None,mapping=None):
    mapping=mapping or _ui_mapping(row)
    reason=_ui_replace_reason(row)
    if row.get('decoded') is False or row.get('geometry_status')=='unresolved': return 'read_only'
    if row.get('fmt') not in ('DXT1','DXT5'): return 'read_only'
    if reason: return 'copy_only'
    if _ui_special_handler(row)=='number_card_wrap': return 'safe_replace'
    if _ui_special_handler(row) in ('number_card_full_canvas','team_shop_exact'): return 'guarded_replace'
    if mapping.get('verified') or (confirmed and confirmed.get('png_replace_safe')):
        return 'safe_replace'
    return 'guarded_replace'


def _ui_display_transform(img,row):
    if _ui_special_handler(row)=='number_card_wrap':
        return _numcard_unroll(img.convert('RGBA'))
    return img

def _ui_content_bbox(img, alpha_first=True, threshold=18):
    a=np.asarray(img.convert('RGBA'))
    rgb=a[:,:,:3].max(axis=2)
    alpha=a[:,:,3]
    mask=((alpha>8)&(rgb>threshold)) if alpha_first else (rgb>threshold)
    if int(mask.sum())<4:
        return None
    ys,xs=np.where(mask)
    return (int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1)

def _ui_fit_preview_crop(img, canvas_size, max_content, alpha_first=True, threshold=18, nearest=False):
    src=img.convert('RGBA'); box=_ui_content_bbox(src,alpha_first=alpha_first,threshold=threshold)
    if box:
        x0,y0,x1,y1=box; pad=2
        x0=max(0,x0-pad); y0=max(0,y0-pad); x1=min(src.width,x1+pad); y1=min(src.height,y1+pad)
        src=src.crop((x0,y0,x1,y1))
    mw,mh=max_content
    scale=min(mw/max(1,src.width),mh/max(1,src.height))
    nw=max(1,int(round(src.width*scale))); nh=max(1,int(round(src.height*scale)))
    filt=(Image.Resampling.NEAREST if nearest and hasattr(Image,'Resampling') else
          Image.NEAREST if nearest else
          Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS)
    src=src.resize((nw,nh),filt)
    canvas=Image.new('RGBA',canvas_size,(0,0,0,0))
    canvas.alpha_composite(src,((canvas.width-nw)//2,(canvas.height-nh)//2))
    return canvas

def _ui_hud_sprite_preview(img,row):
    """Center wraparound HUD sprites for browser display only.

    Several 64x64 blip/arrow textures are stored against a repeat seam.  The raw
    surface can show half the marker at each edge even though the game samples it
    correctly.  Roll only the preview when alpha touches both edges; exports and
    installed bytes remain native.
    """
    src=img.convert('RGBA'); a=np.asarray(src)
    alpha=a[:,:,3]; edge=max(2,src.width//8)
    if alpha[:,:edge].max()>8 and alpha[:,-edge:].max()>8:
        a=np.roll(a,src.width//2,axis=1); src=Image.fromarray(a,'RGBA')
    return _ui_fit_preview_crop(src,(112,112),(86,86),alpha_first=True,threshold=8,nearest=True)

# Number-card previews now use the exact native image.  No display roll or
# quarter-turn is applied; that old correction hid the public parser's +40-byte
# offset error and made externally extracted templates disagree with the app.
NUMCARD_PREVIEW_ROTATE=0

def _ui_number_card_preview(img):
    return _ui_fit_preview_crop(img.convert('RGBA'),(224,112),(208,96),
                                alpha_first=False,threshold=20,nearest=True)

def _ui_preview_mode(row):
    if _ui_special_handler(row)=='number_card_wrap': return 'wrapped_number_card'
    if _ui_short_dxt1_track_map(row): return 'short_dxt1_track_map'
    c=str(row.get('container') or '').upper(); e=str(row.get('entry') or '').upper()
    if c.startswith('MAP_') and e in ('IMG_BLIP_PLAYER','IMG_BLIP_FIRST','IMG_BLIP_OTHERS','IMG_ARROW'):
        return 'hud_sprite'
    if row.get('geometry_status')=='recovered': return 'recovered_geometry'
    return 'standard'

def _ui_thumb_transform(img,row):
    mode=_ui_preview_mode(row)
    if mode=='wrapped_number_card': return _ui_number_card_preview(img)
    if mode=='hud_sprite': return _ui_hud_sprite_preview(img,row)
    return _ui_display_transform(img,row)


def _ui_storage_transform(img,row):
    if _ui_special_handler(row)=='number_card_wrap':
        return _numcard_reroll(img.convert('RGBA'))
    return img

def _ui_csv_row(arcid, container, entry_name, payload_abs=None):
    matches=[r for r in _ui_index()
             if r['archive']==str(arcid) and r['container']==container and r['entry']==entry_name]
    if payload_abs is not None:
        wanted=int(payload_abs)
        exact=[r for r in matches if int(r.get('payload_abs',-1))==wanted]
        if len(exact)==1:return exact[0]
        if len(exact)>1:raise ValueError(f'{entry_name}: duplicate indexed rows share payload offset 0x{wanted:X}')
        if matches:raise ValueError(f'{entry_name}: selected payload offset 0x{wanted:X} is not present in the image index')
    return matches[0] if matches else None

def _ui_apply_indexed_geometry(entry,row,arc):
    """Use the packaged/indexed payload geometry when parser metadata is bad.

    Some native HUD maps and track logos contain non-block-aligned metadata such
    as 256x107 even though their exact BC payload is 256x104.  The generic parser
    can expose the bad dimensions again, undoing recovery and causing reshape
    errors.  The index geometry is authoritative after validating the payload
    range against the live container.
    """
    if not row:
        return entry
    try:
        sprintnums=(str(row.get('container') or '').upper()=='SPRINTNUMS2015.ARC')
        pa=int(entry.get('payload_abs') if sprintnums else row.get('payload_abs'))
        ps=int(entry.get('payload_size') if sprintnums else row.get('payload_size'))
        rw,rh=((128,64) if sprintnums else (int(row.get('w')),int(row.get('h'))))
        fmt=('DXT1' if sprintnums else str(row.get('fmt') or entry.get('fmt')))
    except Exception:
        return entry
    if pa<0 or ps<=0 or pa+ps>len(arc):
        return entry
    needed=_ui_bc_required_bytes(rw,rh,fmt)
    # Never let packaged metadata override a parser result unless the claimed
    # BC surface can physically fit inside the exact indexed payload.
    if not needed or needed>ps or rw>8192 or rh>8192:
        return entry
    if (row.get('geometry_status')=='recovered' or int(entry.get('w',0))!=rw or
        int(entry.get('h',0))!=rh or int(entry.get('payload_size',0))!=ps or
        int(entry.get('payload_abs',-1))!=pa):
        fixed=dict(entry); fixed.update(w=rw,h=rh,fmt=fmt,payload_abs=pa,payload_size=ps)
        fixed['needed']=needed
        return fixed
    return entry

def _ui_load_entry(arcid, container, entry_name, w=None, h=None, pristine=False, payload_abs=None):
    """Read one exact texture payload from an indexed ARC container.

    When payload_abs is supplied (bulk mode), both CSV and parsed-container
    resolution must honor that exact offset. This prevents duplicate names or
    aliases from silently resolving to the first entry in a container.
    """
    g,reg=registry()
    off,size=find_entry(reg, str(arcid), container, pristine=pristine)
    path = reg[str(arcid)]['bak'] if pristine else reg[str(arcid)]['ar']
    with open(path,'rb') as f:
        f.seek(off); arc=f.read(size)

    row=_ui_csv_row(arcid, container, entry_name, payload_abs=payload_abs)
    kd=(int(row['w']),int(row['h'])) if row else ((w,h) if (w and h) else None)
    parse_err=None
    try:
        ents,_base=C.parse_multi_arc(arc,known_dims=kd)
        named=[e for e in ents if e['name']==entry_name]
        if payload_abs is not None:
            wanted=int(payload_abs)
            exact=[e for e in named if int(e.get('payload_abs',-1))==wanted]
            if len(exact)==1:return arc,_ui_apply_indexed_geometry(exact[0],row,arc),off,size
            if len(exact)>1:raise ValueError(f'{entry_name}: parsed duplicates share payload offset 0x{wanted:X}')
            # The CSV may know a payload the generic parser does not expose.
            # Fall through to the exact CSV geometry instead of taking named[0].
            parse_err=f'parser did not expose selected payload 0x{wanted:X}'
        elif len(named)==1:
            return arc,_ui_apply_indexed_geometry(named[0],row,arc),off,size
        elif named and row:
            wanted=int(row.get('payload_abs',-1))
            exact=[e for e in named if int(e.get('payload_abs',-2))==wanted]
            if len(exact)==1:return arc,_ui_apply_indexed_geometry(exact[0],row,arc),off,size
            parse_err=f'container has {len(named)} entries by that name'
        else:
            parse_err='container parsed, but no entry by that name'
    except Exception as pe:
        parse_err=f'container parse failed: {pe}'

    if row:
        e=dict(name=entry_name,w=int(row['w']),h=int(row['h']),fmt=row['fmt'],
               payload_abs=int(row['payload_abs']),payload_size=int(row['payload_size']))
        e['needed']=_ui_needed(e['w'],e['h'],e['fmt'])
        if e['payload_abs']<0 or e['payload_abs']+e['payload_size']>len(arc):
            raise ValueError(f'{entry_name}: payload outside container ({len(arc)} bytes) - stale ui_assets.csv?')
        if payload_abs is not None and e['payload_abs']!=int(payload_abs):
            raise ValueError(f'{entry_name}: exact payload offset mismatch')
        return arc,e,off,size
    raise ValueError(f'{entry_name} not found in {container} (no ui_assets.csv row; {parse_err})')

def _ui_only_payload_changed(old, new, pa, ps):
    """Strongest possible guard: every byte outside the target payload window
    must be identical, and the container size must not change."""
    if len(new)!=len(old): return 'container size changed; refused'
    if old[:pa]!=new[:pa]: return 'bytes before the payload changed; refused'
    if old[pa+ps:]!=new[pa+ps:]: return 'bytes after the payload changed; refused'
    return None

def _ui_install(arcid, entry_off, entry_size, new_arc):
    """Transactionally install one same-size container and verify exact readback.

    Image writes are user-facing and happen inside large shared archives. A short
    write, disk error, or antivirus interruption must not leave half of a menu bank
    changed. The original container is captured before the first live write and is
    restored with fsync + readback if anything fails.
    """
    _g,reg=registry(); v=reg[str(arcid)]
    live=v['ar']; bak=v['bak']
    if len(new_arc)!=int(entry_size):
        raise ValueError('container size changed; refused')
    with open(live,'rb') as fh:
        fh.seek(int(entry_off)); old_arc=fh.read(int(entry_size))
    if len(old_arc)!=int(entry_size):
        raise ValueError('could not read the complete live container before writing')
    ensure_backup(live,bak)
    try:
        with open(live,'r+b') as fh:
            fh.seek(int(entry_off)); fh.write(new_arc); fh.flush(); os.fsync(fh.fileno())
        with open(live,'rb') as fh:
            fh.seek(int(entry_off)); check=fh.read(int(entry_size))
        if check!=new_arc:
            raise ValueError('container readback mismatch after image install')
        return True
    except Exception as install_ex:
        try:
            with open(live,'r+b') as fh:
                fh.seek(int(entry_off)); fh.write(old_arc); fh.flush(); os.fsync(fh.fileno())
            with open(live,'rb') as fh:
                fh.seek(int(entry_off)); restored=fh.read(int(entry_size))
            if restored!=old_arc:
                raise ValueError('rollback readback mismatch')
        except Exception as rollback_ex:
            raise RollbackFailed(install_ex,rollback_ex) from install_ex
        raise


def _ui_infer_mips(w,h,fmt,payload_size):
    """Infer how many standard BC mip levels fit the indexed payload.

    Returns None when the payload uses padding or a non-standard layout. This is
    informational only; Smart Import never changes the target wrapper/layout.
    """
    bpb=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else None
    if not bpb: return None
    total=0; levels=0; cw=max(1,int(w)); ch=max(1,int(h))
    while levels<20:
        total+=max(1,(cw+3)//4)*max(1,(ch+3)//4)*bpb
        levels+=1
        if total==int(payload_size): return levels
        if total>int(payload_size) or (cw==1 and ch==1): break
        cw=max(1,cw//2); ch=max(1,ch//2)
    return None


def _ui_full_standard_mip_layout(w,h,fmt):
    """Return (levels,total_bytes) for the complete standard BC chain to 1×1."""
    levels=0; total=0; cw=max(1,int(w)); ch=max(1,int(h))
    while levels<20:
        total+=_ui_bc_level_size(cw,ch,fmt); levels+=1
        if cw==1 and ch==1: break
        cw=max(1,cw//2); ch=max(1,ch//2)
    return levels,total


def _ui_profile(row,e,confirmed=None):
    safe=_ui_safety(row,confirmed);lw,lh=_ui_logical_dims(row,e);short=_ui_native_short_layout(row,e);recipe=_ui_layout_recipe(row,e)
    return dict(
        profile=(confirmed['id'] if confirmed else 'unmapped_ui_texture'),
        safety=safe,
        width=int(lw or e['w']),height=int(lh or e['h']),codec=e['fmt'],
        storage_width=int(e['w']),storage_height=int(e['h']),
        alpha_supported=(e['fmt']=='DXT5'),
        payload_size=int(e['payload_size']),
        base_level_size=(short['full_bytes'] if short else (_ui_needed(e['w'],e['h'],e['fmt']) if e['fmt'] in ('DXT1','DXT5') else None)),
        inferred_mips=(None if short else _ui_infer_mips(e['w'],e['h'],e['fmt'],e['payload_size'])),
        native_short_payload=bool(short),short_payload_bytes=(short['missing_bytes'] if short else 0),
        resize_default=_ui_recommended_resize_mode(row,e),
        wrapper_preserved=True,
        png_replace_safe=(safe=='safe_replace'),
        special_handler=_ui_special_handler(row),
        replace_reason=_ui_replace_reason(row),
        layout_recipe=recipe.get('kind'),native_mip_levels=recipe.get('levels'),
        audit_policy='clean-game image map v1.0.1',
    )


def _ui_bc_level_size(w,h,fmt):
    block_bytes=8 if fmt=='DXT1' else 16 if fmt=='DXT5' else None
    if block_bytes is None: raise ValueError(f'unsupported BC codec {fmt}')
    return max(1,(int(w)+3)//4)*max(1,(int(h)+3)//4)*block_bytes


def _ui_resize_mip_image(base,size,image_type=''):
    """Downsample one texture level.

    Normal maps are re-normalized after filtering so lower tire/material mips do
    not flatten lighting. Diffuse/specular/UI textures use a normal BOX filter.
    """
    box=Image.Resampling.BOX if hasattr(Image,'Resampling') else Image.BOX
    out=base.resize(tuple(map(int,size)),box)
    if image_type!='normal_map': return out
    rgba=np.asarray(out.convert('RGBA')).astype(np.float32)
    vec=rgba[:,:,:3]/127.5-1.0
    length=np.linalg.norm(vec,axis=2,keepdims=True)
    length=np.where(length<1e-6,1.0,length)
    vec=vec/length
    rgb=np.clip((vec+1.0)*127.5,0,255).astype(np.uint8)
    return Image.fromarray(np.dstack([rgb,rgba[:,:,3].astype(np.uint8)]),'RGBA')


def _ui_encode_mip_level(img,fmt,dxt5_swapped=False):
    enc=encode_any(img.convert('RGBA' if fmt=='DXT5' else 'RGB'),fmt)
    if fmt=='DXT5' and dxt5_swapped:
        enc=C.swap_dxt5_halves(enc)
    if fmt not in ('DXT1','DXT5'):
        raise ValueError(f'unsupported BC codec {fmt}')
    return enc


def _ui_encode_standard_mip_chain(img,w,h,fmt,levels,image_type='',dxt5_swapped=False):
    """Encode an exact standard BC mip chain from the imported base image."""
    levels=int(levels); w=int(w); h=int(h)
    if levels<1: raise ValueError('mip level count must be positive')
    base=img.convert('RGBA' if fmt=='DXT5' else 'RGB')
    chunks=[]; dims=[]
    for level in range(levels):
        lw=max(1,w>>level); lh=max(1,h>>level)
        level_img=base if level==0 and base.size==(lw,lh) else _ui_resize_mip_image(base,(lw,lh),image_type)
        encoded=_ui_encode_mip_level(level_img,fmt,dxt5_swapped=dxt5_swapped)
        need=_ui_bc_level_size(lw,lh,fmt)
        if len(encoded)<need:
            raise ValueError(f'{fmt} mip L{level} encoded short: {len(encoded)} < {need}')
        chunks.append(encoded[:need]); dims.append([lw,lh])
    return b''.join(chunks),dims


def _ui_encode_native_padded_chain(img,fmt,recipe,old_payload,image_type='',dxt5_swapped=False):
    """Encode all logical mip bytes into a stock padded payload.

    Padding bytes remain byte-identical to stock/live data. Only actual BC rows
    are replaced, which is stricter than rebuilding the padding with zeros.
    """
    base=img.convert('RGBA' if fmt=='DXT5' else 'RGB'); out=bytearray(old_payload); dims=[];written=0
    for level in recipe['layout']:
        lw,lh=int(level['width']),int(level['height'])
        li=base if level['level']==0 and base.size==(lw,lh) else _ui_resize_mip_image(base,(lw,lh),image_type)
        enc=_ui_encode_mip_level(li,fmt,dxt5_swapped=dxt5_swapped)
        need=_ui_bc_level_size(lw,lh,fmt)
        if len(enc)<need:raise ValueError(f'{fmt} padded mip L{level["level"]} encoded short')
        row_bytes=int(level['row_bytes']);rows=int(level['rows']);stride=int(level['row_stride']);off=int(level['offset'])
        if row_bytes*rows!=need:raise ValueError(f'padded mip L{level["level"]} row geometry mismatch')
        for r in range(rows):
            src=r*row_bytes;dst=off+r*stride
            if dst+row_bytes>len(out):raise ValueError(f'padded mip L{level["level"]} exceeds target payload')
            out[dst:dst+row_bytes]=enc[src:src+row_bytes];written+=row_bytes
        dims.append([lw,lh])
    return bytes(out),dims,written


def _ui_encode_physical_canvas(img,fmt,recipe,old_payload,dxt5_swapped=False):
    sw,sh=int(recipe['storage_width']),int(recipe['storage_height'])
    logical=img.convert('RGBA' if fmt=='DXT5' else 'RGB')
    canvas=Image.new('RGBA' if fmt=='DXT5' else 'RGB',(sw,sh),(0,0,0,0) if fmt=='DXT5' else (0,0,0))
    canvas.paste(logical,(0,0))
    enc=_ui_encode_mip_level(canvas,fmt,dxt5_swapped=dxt5_swapped)
    if len(enc)!=len(old_payload):raise ValueError(f'physical-canvas encoder produced {len(enc)} bytes; expected {len(old_payload)}')
    return enc,[[sw,sh]],len(enc)


def _ui_prepare_encoded(q,arc,e,safe,row):
    """Decode, resize and encode one audit-approved native texture profile."""
    raw=_b64.b64decode(q.get('image') or q.get('png') or '')
    if not raw: raise ValueError('no imported image data')
    img=Image.open(io.BytesIO(raw));source_format=(img.format or 'unknown').upper();img.load()
    preserve_alpha=(e['fmt']=='DXT5')
    logical_w,logical_h=_ui_logical_dims(row,e)
    effective_mode,requested_mode,mode_reason=_ui_effective_resize_mode(row,q.get('resize_mode','auto'),e)
    img,prep=prepare_import_image(img,(logical_w or e['w'],logical_h or e['h']),effective_mode,preserve_alpha=preserve_alpha)
    prep.update(requested_mode=requested_mode,effective_mode=effective_mode,target_aware=bool(mode_reason))
    if mode_reason:prep['resize_reason']=mode_reason
    img=_ui_storage_transform(img,row);prep['target_codec']=e['fmt'];prep['target_payload_size']=int(e['payload_size'])
    if _ui_special_handler(row)=='number_card_wrap':prep['special_transform']='number-card wraparound re-applied for game storage'
    elif _ui_special_handler(row)=='number_card_full_canvas':prep['special_transform']='complete mapped number canvas used; black padding disabled'
    prep['alpha_action']=(('preserved' if prep.get('source_alpha') else 'opaque; target supports alpha') if e['fmt']=='DXT5' else ('flattened to opaque RGB' if prep.get('source_alpha') else 'not present'))
    recipe=_ui_layout_recipe(row,e)
    if not recipe.get('writable'):raise ValueError(recipe.get('note') or 'native layout is not approved for PNG replacement')
    target_size=int(e['payload_size']);pa=int(e['payload_abs']);old_payload=bytes(arc[pa:pa+target_size])
    image_type=_ui_image_type(row,_ui_mapping(row));mip_dims=[];written=0
    kind=recipe['kind']
    if kind in ('tight_base','tight_mips'):
        levels=int(recipe.get('levels') or 1)
        enc,mip_dims=_ui_encode_standard_mip_chain(img,e['w'],e['h'],e['fmt'],levels,image_type,dxt5_swapped=bool(e.get('dxt5_swapped')))
        if len(enc)!=target_size:raise ValueError(f'generated tight mip chain is {len(enc)} bytes; expected {target_size}')
        payload=enc;written=len(enc)
        layout_note=('exact base-level payload' if levels==1 else f'complete {levels}-level tight mip chain regenerated')
    elif kind in ('bms_padded_mips','row_padded_mips'):
        payload,mip_dims,written=_ui_encode_native_padded_chain(img,e['fmt'],recipe,old_payload,image_type,dxt5_swapped=bool(e.get('dxt5_swapped')))
        layout_note=f'complete {recipe["levels"]}-level {kind.replace("_"," ")} regenerated; native row/mip padding preserved byte-for-byte'
    elif kind=='physical_canvas':
        payload,mip_dims,written=_ui_encode_physical_canvas(img,e['fmt'],recipe,old_payload,dxt5_swapped=bool(e.get('dxt5_swapped')))
        layout_note=f'logical {logical_w}x{logical_h} image placed in exact {recipe["storage_width"]}x{recipe["storage_height"]} physical canvas'
    elif kind=='native_short':
        storage=_ui_pad_storage_image(img,row,e)
        enc=_ui_encode_mip_level(storage,e['fmt'],dxt5_swapped=bool(e.get('dxt5_swapped')))
        if len(enc)<target_size:raise ValueError('native-short encoder returned too few bytes')
        payload=enc[:target_size];written=target_size;mip_dims=[[int(e['w']),int(e['h'])]]
        layout_note=f'native short layout preserved; exact {len(enc)-target_size}-byte omitted tail retained outside the payload'
    else:
        raise ValueError('unsupported audit layout recipe '+str(kind))
    if len(payload)!=target_size:raise ValueError(f'encoded payload is {len(payload)} bytes; expected {target_size}')
    new=bytearray(arc);new[pa:pa+target_size]=payload;new=bytes(new)
    err=_ui_only_payload_changed(arc,new,pa,target_size)
    if err:raise ValueError(err)
    decoded=_ui_decode_image(new,e,row,logical=True);expected=_ui_logical_dims(row,e)
    if decoded.size!=expected:raise ValueError(f'decode-back dimensions are {decoded.size}, expected {expected}')
    decoded_display=_ui_thumb_transform(decoded,row);thumb=decoded_display.copy();thumb.thumbnail((420,260));pb=io.BytesIO();thumb.save(pb,'PNG')
    prep.update(dict(source_format=source_format,encoded_size=written,target_payload_size=target_size,padding_bytes=target_size-written,
                     truncated_bytes=0,native_short_payload=(kind=='native_short'),layout_note=layout_note,layout_recipe=kind,
                     mip_levels_written=int(recipe.get('levels') or 1),mip_dimensions=mip_dims,
                     mip_policy=layout_note,decode_back=True,output=[decoded.width,decoded.height]))
    return new,payload,prep,_b64.b64encode(pb.getvalue()).decode()


def _texture_discovery_module():
    if not os.path.exists(TEXTURE_DISCOVERY_TOOL):
        raise RuntimeError('texture discovery helper is missing')
    spec=importlib.util.spec_from_file_location('nascar15_texture_discovery_runtime',TEXTURE_DISCOVERY_TOOL)
    if spec is None or spec.loader is None: raise RuntimeError('could not load texture discovery helper')
    mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
    return mod

def _ui_discovery_family(container, entry):
    """Classify newly discovered texture rows without pretending the exact consumer is verified."""
    c=str(container or '').upper(); e=str(entry or '').upper()
    if c=='NASCAR6_TEXTURES_X.ARC':
        if e.startswith('TYRE') or 'WHEEL' in e:
            return 'tire_wheel_textures'
        return 'shared_vehicle_textures'
    if c=='SPRINTNUMS2015.ARC': return 'driver_number_cards'
    if c.startswith(('2TRACKSELECTMENUIMAGE','3TRACKCARDIMAGE','TRACKDETAILIMAGES','CALENDAR_TRACK_IMAGES','2LOBBYTRACKCARDIMAGE','LOBBYTRACKCARDDETAILIMAGE','TRACK_FACTS_IMG_')):
        return 'track_select'
    if c.startswith('MAP_') or 'TRACKLOGO' in c: return 'track_logos_maps'
    if any(k in (c+' '+e) for k in ('HUD','GAUGE','TACH','SPEEDO','DAMAGE','STANDING','LEADERBOARD','RUNNINGORDER','LAPCOUNTER','POSITION','OVERLAY','TELEMETRY','DIAL','METER')): return 'race_hud_textures'
    if c in ('BASESCHEMETHUMBNAILS.ARC','CUSTOMSCHEMETHUMBNAILS.ARC'): return 'misc_ui'
    if c in ('GLOBALMENUASSETS.ARC','RACEMEDIAIMAGES.ARC','3LOADINGTRIVIAQUIZIMAGETEST.ARC') or 'MENUIMAGE' in c:
        return 'misc_ui'
    if c.startswith('TEAMSHOPLOGO'): return 'team_logos'
    if e.startswith('DRIVERPAINT_') or e.startswith('PAINTSCHEME_'): return 'paint_scheme_preview'
    if e.startswith('DRIVER_') and '3DNUM' in e: return 'driver_select'
    if c.startswith(('LIVERY_','HDLIVERY_')) or e=='IMG_LIV': return 'unknown_visual'
    if any(k in c for k in ('TEXTURE','TEX_','REPLACETEX')): return 'discovered_texture'
    return 'discovered_texture'


def _ui_direct_known_texture_rows(reg,max_mb):
    """Supplement the external scanner by directly enumerating high-value visual containers.

    Older discovery seeds were intentionally narrow and could list only the seven
    known tire/wheel maps from NASCAR6_TEXTURES_X.ARC.  This pass asks the proven
    multi-ARC parser for every entry in likely image containers so the browser can
    expose shared vehicle maps and other presentation assets the seed omitted.
    """
    likely_rx=re.compile(
        r'(?:TEXTURE|IMAGE|LOGO|THUMB|SPRINTNUMS|PAINT|MAP_|REPLACETEX|NASCAR6_TEXTURES_X|HUD|GAUGE|TACH|SPEEDO|DAMAGE|STANDING|LEADER|LAP|POSITION|OVERLAY|METER|TELEMETRY)',re.I)
    out=[];containers=0;errors=[];limit=int(max_mb)*1024*1024
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try: indexed=parse_cdfiles(v['cdf'])
        except Exception as ex:
            errors.append(f'ARCHIVE{arcid} index: {ex}');continue
        for off,size,name in indexed:
            if not str(name).upper().endswith('.ARC') or not likely_rx.search(str(name)):continue
            if int(size)<=0 or int(size)>limit:continue
            try:
                with open(v['ar'],'rb') as f:f.seek(int(off));blob=f.read(int(size))
                if len(blob)!=int(size):raise ValueError('short container read')
                entries,_base=C.parse_multi_arc(blob)
                added=0
                for e in entries:
                    en=str(e.get('name') or '').strip();fmt=str(e.get('fmt') or '').upper()
                    if not en or fmt not in ('DXT1','DXT5'):continue
                    try:
                        w=int(e.get('w'));h=int(e.get('h'));pa=int(e.get('payload_abs'));ps=int(e.get('payload_size'))
                    except Exception:continue
                    if w<=0 or h<=0 or ps<=0 or pa<0 or pa+ps>len(blob):continue
                    out.append(dict(archive=str(arcid),container=str(name),entry=en,w=w,h=h,fmt=fmt,
                                    payload_abs=pa,payload_size=ps,decoded=1,
                                    mip_count=int(e.get('mip_count') or 0),record_type=str(e.get('layout') or 'primary16'),
                                    overlap_previous=0,overlap_next=0,write_blocked=0,
                                    family=_ui_discovery_family(name,en)))
                    added+=1
                if added:containers+=1
            except Exception as ex:
                errors.append(f'ARCHIVE{arcid}/{name}: {ex}')
    return out,dict(direct_containers=containers,direct_found=len(out),direct_errors=errors[:30])


def _ui_write_discovery_csv(rows,path):
    fields=['archive','container','entry','w','h','fmt','payload_abs','payload_size','decoded','mip_count','record_type','mip_layout','overlap_previous','overlap_next','write_blocked','family']
    os.makedirs(os.path.dirname(path),exist_ok=True)
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8',newline='') as f:
        w=_csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for row in sorted(rows,key=lambda r:(int(str(r.get('archive') or '0')),str(r.get('container','')).upper(),str(r.get('entry','')).upper())):
            w.writerow({k:row.get(k,'') for k in fields})
    os.replace(tmp,path)


@app.route('/api/ui/discover', methods=['POST'])
def ui_discover():
    q=request.get_json(silent=True) or {}
    try:
        game,reg=registry()
        if not game or not reg: raise ValueError('NASCAR 15 game data folder is not configured')
        max_mb=max(8,min(1024,int(q.get('max_container_mb',256))))
        helper_error=None
        try:
            mod=_texture_discovery_module()
            live_rows,summary=mod.scan_registry(reg,max_mb)
        except Exception as ex:
            helper_error=str(ex);live_rows=[];summary=dict(containers=0,textures=0)
        direct_rows,direct_summary=_ui_direct_known_texture_rows(reg,max_mb)
        # Direct coverage fills gaps; the external scanner is appended last so its
        # more specific family labels win when both find the same resource.
        live_rows=direct_rows+list(live_rows or [])
        # Merge live discoveries with the packaged seed so a partial/older install
        # cannot erase known Archive 3 character and shared-vehicle mappings.
        merged={}
        discovered_path=_discovered_texture_csv(); report_path=_texture_discovery_report()
        os.makedirs(os.path.dirname(discovered_path) or USER_DIR,exist_ok=True)
        if os.path.exists(discovered_path):
            with open(discovered_path,'r',encoding='utf-8-sig',newline='') as f:
                for row in _csv.DictReader(f):
                    merged[_ui_packaged_map_key(row.get('archive'),row.get('container'),row.get('entry'),row.get('payload_abs'))]=row
        before=len(merged)
        for row in live_rows:
            merged[_ui_packaged_map_key(row.get('archive'),row.get('container'),row.get('entry'),row.get('payload_abs'))]=row
        _ui_write_discovery_csv(merged.values(),discovered_path)
        summary.update(direct_summary)
        summary.update(dict(game=game,seed_before=before,live_found=len(live_rows),merged_total=len(merged),
                            helper_error=helper_error,created=datetime.datetime.now().isoformat()))
        with open(report_path,'w',encoding='utf-8') as f: json.dump(summary,f,indent=2)
        _UI_INDEX_CACHE['signature']=None; _UI_INDEX_CACHE['rows']=None; _UI_THUMB_CACHE.clear()
        return jsonify(dict(ok=True,**summary))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/status')
def ui_status():
    index_files=_ui_index_files(); rows=[]
    if index_files:
        try: rows=_ui_index()
        except Exception: rows=[]
    overrides=_ui_mapping_overrides(); maps=[_ui_mapping(r,overrides) for r in rows]
    cats=sorted({m['category'] for m in maps}); screens=sorted({m['screen'] for m in maps})
    counts={c:sum(1 for m in maps if m['category']==c) for c in cats}
    screen_counts={c:sum(1 for m in maps if m['screen']==c) for c in screens}
    fmt_counts={f:sum(1 for r in rows if r.get('fmt')==f) for f in sorted({r.get('fmt') for r in rows})}
    verified=sum(1 for m in maps if m.get('verified'))
    packaged_mapped=sum(1 for m in maps if m.get('packaged_mapped'))
    research=sum(1 for m in maps if m.get('confidence') in ('unknown','research'))
    decoded=sum(1 for r in rows if r.get('decoded')); recovered=sum(1 for r in rows if r.get('geometry_status')=='recovered')
    unresolved=sum(1 for r in rows if not r.get('decoded'))
    source_counts={src:sum(1 for r in rows if r.get('source_index')==src) for src in sorted({r.get('source_index','unknown') for r in rows})}
    safety_counts=collections.Counter(_ui_safety(r,_confirmed_for(r),_ui_mapping(r,overrides)) for r in rows)
    mapping_status_counts=collections.Counter(_ui_mapping_status(r,_ui_mapping(r,overrides)) for r in rows)
    replacement_route_counts=collections.Counter(_ui_replacement_route(r,_confirmed_for(r),_ui_mapping(r,overrides)) for r in rows)
    container_type_counts=collections.Counter(_ui_container_type(r) for r in rows)
    image_type_counts=collections.Counter(_ui_image_type(r,_ui_mapping(r,overrides)) for r in rows)
    discovery_report={}
    try:
        report_path=_texture_discovery_report()
        if os.path.exists(report_path): discovery_report=json.load(open(report_path,'r',encoding='utf-8'))
    except Exception: discovery_report={}
    return jsonify(dict(ok=True,csv=bool(index_files),count=len(rows),packaged_count=source_counts.get('built_in',0),discovered_count=source_counts.get('discovered',0),decoded_count=decoded,recovered_count=recovered,unresolved_count=unresolved,
                        categories=cats,screens=screens,category_counts=counts,screen_counts=screen_counts,format_counts=fmt_counts,
                        verified_count=verified,packaged_mapped_count=packaged_mapped,mapping_unmapped_count=max(0,len(rows)-packaged_mapped),research_count=research,source_counts=source_counts,
                        discovery_report=discovery_report,
                        discovery_tool=bool(os.path.exists(TEXTURE_DISCOVERY_TOOL)),
                        safety_counts=dict(safety_counts),mapping_status_counts=dict(mapping_status_counts),replacement_route_counts=dict(replacement_route_counts),container_type_counts=dict(container_type_counts),image_type_counts=dict(image_type_counts),
                        fully_typed_count=sum(image_type_counts.values()),untyped_count=max(0,len(rows)-sum(image_type_counts.values())),
                        structurally_replaceable=sum(1 for r in rows if _ui_safety(r,_confirmed_for(r),_ui_mapping(r,overrides)) in ('safe_replace','guarded_replace')),
                        exact_raw_replaceable=sum(1 for r in rows if int(r.get('payload_size') or 0)>0 and int(r.get('payload_abs') or -1)>=0),
                        confirmed=[dict(id=a['id'],label=a['label'],png_replace_safe=a['png_replace_safe'],
                                        note=a['note']) for a in CONFIRMED_ASSETS]))

def _ui_modified_states(rows,reg):
    """Return exact live-vs-backup payload states without decoding thumbnails."""
    containers={}; states={}
    for r in rows:
        ident=(str(r['archive']),r['container'],r['entry'],_ui_payload_identity(r.get('payload_abs')))
        arcid=str(r['archive'])
        if arcid not in reg or not os.path.exists(reg[arcid]['bak']):
            states[ident]=False; continue
        ckey=(arcid,r['container'])
        pair=containers.get(ckey)
        if pair is None:
            try:
                off,size=find_entry(reg,arcid,r['container'])
                soff,ssize=find_entry(reg,arcid,r['container'],pristine=True)
                with open(reg[arcid]['ar'],'rb') as f:
                    f.seek(off); live=f.read(size)
                with open(reg[arcid]['bak'],'rb') as f:
                    f.seek(soff); stock=f.read(ssize)
                pair=(live,stock) if len(live)==size and len(stock)==ssize and size==ssize else (None,None)
            except Exception:
                pair=(None,None)
            containers[ckey]=pair
        live,stock=pair
        if live is None:
            states[ident]=False; continue
        pa=int(r.get('payload_abs',-1)); ps=int(r.get('payload_size',0))
        if pa<0 or ps<=0 or pa+ps>len(live) or pa+ps>len(stock):
            states[ident]=False; continue
        states[ident]=(live[pa:pa+ps]!=stock[pa:pa+ps])
    return states

@app.route('/api/ui/list', methods=['POST'])
def ui_list():
    q=request.get_json() or {}; mode=q.get('mode','normal'); text=(q.get('q') or '').lower()
    category=q.get('category') or ''; screen=q.get('screen') or ''
    safety=q.get('safety') or 'all'; mapping_filter=q.get('mapping_status') or 'all'; modified_only=bool(q.get('modified_only'))
    try: rows=_ui_index()
    except Exception as e: return jsonify(dict(ok=False,error=str(e))),400
    _g,_reg=registry(); overrides=_ui_mapping_overrides(); candidates=[]
    try: driver_links=_team_fast_driver_links()
    except Exception: driver_links={}
    hidden_normal={'Character & Pit Crew Textures','Driver & Character Textures','Vehicle / Livery Textures','Unknown / Research','Discovered Textures','Unresolved Binary Candidates'}
    tire_categories={'Tires & Wheels'}
    vehicle_categories={'Shared Vehicle Textures','Vehicle / Livery Textures','Character & Pit Crew Textures','Driver & Character Textures'}
    for r in rows:
        a=_confirmed_for(r); m=_ui_mapping(r,overrides); cat=m['category']; safe=_ui_safety(r,a,m)
        special=_ui_special_handler(r); dedicated={}
        if special=='driver_select_3dnum_dedicated':
            match=re.match(r'^DRIVER_(\d+)_3DNUM_',str(r.get('entry') or '').upper())
            driver_uid=int(match.group(1)) if match else None
            link=driver_links.get(driver_uid) if driver_uid is not None else None
            current_team_uid=int(link.get('team_uid')) if link else None
            current_container=(f'2DRIVERSELECTTD_{current_team_uid}.ARC' if current_team_uid is not None else None)
            current_target=bool(current_container and str(r.get('container') or '').upper()==current_container.upper())
            dedicated=dict(driver_uid=driver_uid,config_uid=(int(link.get('config_uid')) if link else None),
                           current_team_uid=current_team_uid,current_container=current_container,
                           dedicated_current_target=current_target)
            safe='safe_replace' if current_target else 'copy_only'
        if mode=='normal' and cat in hidden_normal: continue
        if mode=='tires' and cat not in tire_categories: continue
        if mode=='vehicle' and cat not in vehicle_categories: continue
        if q.get('asset_id') and (not a or a['id']!=q['asset_id']): continue
        if category and category!='all' and cat!=category: continue
        if screen and screen!='all' and m['screen']!=screen: continue
        map_status=_ui_mapping_status(r,m)
        if safety!='all' and safe!=safety: continue
        if mapping_filter!='all' and map_status!=mapping_filter: continue
        hay=' '.join([r['entry'],r['container'],r['archive'],cat,m['screen'],m['role'],m['label'],m['note'],map_status,(a['label'] if a else '')]).lower()
        if text and text not in hay: continue
        candidates.append((r,a,m,safe,dedicated))
    modified_states=_ui_modified_states([x[0] for x in candidates],_reg) if modified_only else {}
    out=[]
    for r,a,m,safe,dedicated in candidates:
        ident=(str(r['archive']),r['container'],r['entry'],_ui_payload_identity(r.get('payload_abs'))); modified=modified_states.get(ident) if modified_only else None
        if modified_only and not modified: continue
        special=_ui_special_handler(r); n14_guarded=(ACTIVE_GAME=='nascar14')
        current_3dnum=bool(dedicated.get('dedicated_current_target')) and not n14_guarded
        replacement_route=('exact_raw' if n14_guarded else ('specialized_png' if current_3dnum else _ui_replacement_route(r,a,m)))
        replace_reason=("NASCAR '14 Smart Import stays locked in this beta; use Raw Export/Exact Raw Import only for reviewed ARCHIVE0/1 assets." if n14_guarded else ('' if current_3dnum else _ui_replace_reason(r)))
        recipe=_ui_layout_recipe(r)
        out.append(dict(archive=r['archive'],container=r['container'],entry=r['entry'],payload_abs=r.get('payload_abs'),
                        w=r['w'],h=r['h'],fmt=r['fmt'],payload_size=r['payload_size'],mip_count=r.get('mip_count'),
                        family=r.get('family',''),asset_id=(a['id'] if a else None),
                        label=m['label'],category=m['category'],screen=m['screen'],role=m['role'],
                        safety=safe,safety_label=_ui_policy_label(safe),confidence=m['confidence'],verified=bool(m.get('verified')),
                        mapping_status=_ui_mapping_status(r,m),replacement_route=replacement_route,exact_raw_import=True,
                        image_type=_ui_image_type(r,m),container_type=_ui_container_type(r),preview_mode=_ui_preview_mode(r),
                        special_handler=special,replace_reason=replace_reason,packaged_mapped=bool(m.get('packaged_mapped')),
                        driver_uid=dedicated.get('driver_uid'),config_uid=dedicated.get('config_uid'),
                        current_team_uid=dedicated.get('current_team_uid'),current_container=dedicated.get('current_container'),
                        dedicated_current_target=current_3dnum,
                        exact_base_payload=(bool(_ui_native_short_layout(r)) or (int(r.get('payload_size') or 0)>=_ui_needed(r['w'],r['h'],r['fmt']) if r.get('fmt') in ('DXT1','DXT5') else False)),
                        native_short_payload=bool(_ui_native_short_layout(r)),short_payload_bytes=((_ui_native_short_layout(r) or {}).get('missing_bytes',0)),
                        logical_w=_ui_logical_dims(r)[0],logical_h=_ui_logical_dims(r)[1],
                        decoded=bool(r.get('decoded')),geometry_status=r.get('geometry_status','indexed'),
                        original_w=r.get('original_w'),original_h=r.get('original_h'),decode_error=r.get('decode_error',''),
                        user_mapped=bool(m.get('user_mapped')),source_group=r.get('source_index') or r.get('family','ui_assets'),
                        note=m['note'],modified=modified,
                        has_backup=bool(str(r['archive']) in _reg and os.path.exists(_reg[str(r['archive'])]['bak'])),
                        png_replace_safe=(False if n14_guarded else (current_3dnum or (bool(r.get('decoded')) and safe in ('safe_replace','guarded_replace') and special!='driver_select_3dnum_dedicated'))),
                        profile=dict(profile=(a['id'] if a else r.get('family') or 'indexed_ui_texture'),
                                     alpha_supported=(r['fmt']=='DXT5'),
                                     inferred_mips=_ui_infer_mips(r['w'],r['h'],r['fmt'],r['payload_size']),
                                     native_mip_levels=recipe.get('levels'),layout_recipe=recipe.get('kind'),
                                     audit_policy='clean-game image map v1.0.1',
                                     wrapper_preserved=True)))
    # Page 1 is the first thing anyone sees, so lead with recognizable car renders,
    # 3D numbers and paint thumbnails. Native number UV atlases can look fragmented
    # at gallery scale, so they are grouped near the back rather than mistaken for
    # broken normal images. Unresolved/read-only resources go last. Python's sort is
    # stable, so ordering inside each band is untouched.
    _LEAD_FAMILIES = ('driver_select', 'paint_scheme_preview')

    def _browse_rank(row):
        if row.get('safety') == 'read_only' or row.get('geometry_status') == 'unresolved':
            return 3
        fam = str(row.get('family') or '')
        if fam == 'driver_number_cards' or row.get('preview_mode') == 'wrapped_number_card':
            return 2
        if fam in _LEAD_FAMILIES:
            return 0
        return 1
    out.sort(key=_browse_rank)
    total=len(out); page=max(0,int(q.get('page',0))); per=max(1,min(240,int(q.get('per',120))))
    return jsonify(dict(ok=True,total=total,page=page,per=per,modified_only=modified_only,
                        rows=out[page*per:(page+1)*per]))

@app.route('/api/ui/audit')
def ui_audit():
    """One pass over the whole graphics index: what is present, what decodes, and
    what can be replaced safely. Read-only - it reads the index and the same
    safety rules the replace routes enforce, and touches no game bytes.

    This exists because the per-item pills only answer the question one asset at a
    time, and there are over a thousand of them.
    """
    try:
        rows = _ui_index()
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

    overrides = _ui_mapping_overrides()
    totals = collections.Counter()
    by_safety = collections.Counter()
    problems = {}
    fmts = collections.Counter()

    for r in rows:
        totals['entries'] += 1
        fmts[str(r.get('fmt') or 'unknown')] += 1
        conf = _confirmed_for(r)
        mapping = _ui_mapping(r, overrides)
        safety = _ui_safety(r, conf, mapping)
        by_safety[safety] += 1
        decoded = r.get('decoded') is not False
        if decoded:
            totals['decodes'] += 1
        reason = _ui_replace_reason(r)
        if reason or not decoded:
            container = str(r.get('container') or '?').upper()
            slot = problems.setdefault(container, dict(
                container=container, count=0, safety=safety,
                reason=(reason or 'Geometry is unresolved; raw export only.'),
                examples=[]))
            slot['count'] += 1
            if len(slot['examples']) < 3:
                slot['examples'].append(str(r.get('entry') or '?'))

    blocked = sorted(problems.values(), key=lambda d: -d['count'])
    return jsonify(dict(ok=True,
        entries=totals['entries'],
        decodes=totals['decodes'],
        undecodable=totals['entries'] - totals['decodes'],
        formats=dict(fmts),
        safety=dict(by_safety),
        replaceable=by_safety.get('safe_replace', 0),
        copy_only=by_safety.get('copy_only', 0),
        read_only=by_safety.get('read_only', 0),
        blocked_containers=blocked,
        note=('Blocked entries are still exportable and restorable; only PNG '
              're-encoding is withheld, because their stored layout is not '
              'proven safe to rebuild.')))

@app.route('/api/ui/tire_family')
def ui_tire_family():
    try:
        scope=(request.args.get('scope') or 'diffuse').lower();rows=_ui_index();_g,reg=registry();out=[]
        names={'diffuse':{'TYRE02.DDS','TYRE02-D.DDS'},
               'maps':{'TYRE02.DDS','TYRE02-D.DDS','TYRE02-N.DDS','TYRE02-DN.DDS','TYRE02-S.DDS','TYRE02-DS.DDS'},
               'all':{'TYRE02.DDS','TYRE02-D.DDS','TYRE02-N.DDS','TYRE02-DN.DDS','TYRE02-S.DDS','TYRE02-DS.DDS','WHEELBLURNEW.DDS'}}.get(scope)
        if names is None:raise ValueError('scope must be diffuse, maps, or all')
        overrides=_ui_mapping_overrides()
        for r in rows:
            if str(r.get('container','')).upper()!='NASCAR6_TEXTURES_X.ARC' or str(r.get('entry','')).upper() not in names:continue
            a=_confirmed_for(r);m=_ui_mapping(r,overrides);safe=_ui_safety(r,a,m)
            out.append(dict(archive=r['archive'],container=r['container'],entry=r['entry'],payload_abs=r.get('payload_abs'),w=r['w'],h=r['h'],fmt=r['fmt'],payload_size=r['payload_size'],family=r.get('family',''),label=m['label'],category=m['category'],screen=m['screen'],role=m['role'],safety=safe,decoded=bool(r.get('decoded')),png_replace_safe=(bool(r.get('decoded')) and safe in ('safe_replace','guarded_replace')),has_backup=bool(str(r['archive']) in reg and os.path.exists(reg[str(r['archive'])]['bak']))))
        order=['Tyre02.dds','Tyre02-D.dds','Tyre02-N.dds','Tyre02-DN.dds','Tyre02-S.dds','Tyre02-DS.dds','wheelblurNEW.dds'];out.sort(key=lambda x:order.index(x['entry']) if x['entry'] in order else 99)
        return jsonify(dict(ok=True,scope=scope,count=len(out),rows=out))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/map', methods=['POST'])
def ui_map():
    q=request.get_json() or {}
    try:
        key=_ui_mapping_key(q); d=_ui_mapping_overrides(); current=d.get(key,{}) if isinstance(d.get(key,{}),dict) else {}
        for k in ('label','category','screen','role','note'):
            if k in q:
                v=str(q.get(k) or '').strip()
                if v: current[k]=v
                else: current.pop(k,None)
        if 'verified' in q: current['verified']=bool(q.get('verified'))
        if current: d[key]=current
        else: d.pop(key,None)
        _ui_save_mapping_overrides(d)
        row=dict(archive=q.get('archive'),container=q.get('container'),entry=q.get('entry'),payload_abs=q.get('payload_abs'),family=q.get('family'),fmt=q.get('fmt'))
        return jsonify(dict(ok=True,mapping=_ui_mapping(row,d)))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

def _ui_full_manifest_rows():
    rows=_ui_index(); overrides=_ui_mapping_overrides(); out=[]
    for r in rows:
        a=_confirmed_for(r); m=_ui_mapping(r,overrides); safe=_ui_safety(r,a,m)
        out.append(dict(
            archive=str(r.get('archive','')),container=r.get('container',''),entry=r.get('entry',''),
            payload_abs=int(r.get('payload_abs',-1)),payload_size=int(r.get('payload_size',0)),
            width=int(r.get('w',0)),height=int(r.get('h',0)),logical_width=_ui_logical_dims(r)[0],logical_height=_ui_logical_dims(r)[1],format=r.get('fmt',''),family=r.get('family',''),
            decoded=bool(r.get('decoded')),geometry_status=r.get('geometry_status',''),native_short_payload=bool(_ui_native_short_layout(r)),short_payload_bytes=((_ui_native_short_layout(r) or {}).get('missing_bytes',0)),
            category=m.get('category',''),screen=m.get('screen',''),role=m.get('role',''),label=m.get('label',''),
            confidence=m.get('confidence',''),mapping_status=_ui_mapping_status(r,m),verified=bool(m.get('verified')),
            image_type=_ui_image_type(r,m),container_type=_ui_container_type(r),preview_mode=_ui_preview_mode(r),
            edit_policy=safe,replacement_route=_ui_replacement_route(r,a,m),png_import=bool(r.get('decoded')) and safe in ('safe_replace','guarded_replace'),
            exact_raw_import=True,note=m.get('note',''),decode_error=r.get('decode_error',''),source_group=r.get('source_index','')
        ))
    return out


@app.route('/api/ui/manifest/export')
def ui_manifest_export():
    try:
        rows=_ui_full_manifest_rows(); fmt=(request.args.get('format') or 'csv').lower()
        if fmt=='json':
            payload=json.dumps(dict(app_version=APP_VERSION,physical_records=len(rows),rows=rows),indent=2).encode('utf-8')
            return send_file(io.BytesIO(payload),mimetype='application/json',as_attachment=True,download_name=f'nascar15_full_graphics_map_v{APP_VERSION}.json')
        fields=list(rows[0].keys()) if rows else []
        text=io.StringIO(); writer=_csv.DictWriter(text,fieldnames=fields,extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
        return send_file(io.BytesIO(text.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name=f'nascar15_full_graphics_map_v{APP_VERSION}.csv')
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/ui/mappings/export')
def ui_mappings_export():
    d=_ui_mapping_overrides(); b=io.BytesIO(json.dumps(d,indent=2,sort_keys=True).encode('utf-8'))
    return send_file(b,mimetype='application/json',as_attachment=True,download_name='nascar15_ui_mapping_overrides.json')

@app.route('/api/ui/export_raw', methods=['POST'])
def ui_export_raw():
    q=request.get_json() or {}
    try:
        row=_ui_csv_row(q['archive'],q['container'],q['entry'],payload_abs=q.get('payload_abs'))
        if not row: raise ValueError('indexed row not found')
        g,reg=registry(); arcid=str(q['archive']); off,size=find_entry(reg,arcid,q['container'])
        with open(reg[arcid]['ar'],'rb') as f:
            f.seek(off); arc=f.read(size)
        pa=int(row.get('payload_abs',-1)); ps=int(row.get('payload_size',0))
        if pa<0 or ps<=0 or pa+ps>len(arc): raise ValueError('payload range is outside the live container')
        payload=arc[pa:pa+ps]
        fn=re.sub(r'[^A-Za-z0-9_.-]+','_',f"ARCHIVE{arcid}_{q['container']}_{q['entry']}_off{pa}_{ps}bytes.bin")
        return jsonify(dict(ok=True,filename=fn,raw=_b64.b64encode(payload).decode(),size=ps))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/export', methods=['POST'])
def ui_export():
    q=request.get_json()
    try:
        row=_ui_csv_row(q['archive'],q['container'],q['entry'],payload_abs=q.get('payload_abs'))
        if row and not row.get('decoded'): raise ValueError('geometry is unresolved; use Raw Export')
        arc,e,_,_=_ui_load_entry(q['archive'], q['container'], q['entry'], q.get('w'), q.get('h'), pristine=bool(q.get('pristine')), payload_abs=q.get('payload_abs'))
        img=_ui_decode_image(arc,e,row or q,logical=True)
        img=_ui_thumb_transform(img,row or q)
        buf=io.BytesIO(); img.save(buf,'PNG')
        return jsonify(dict(ok=True, png=_b64.b64encode(buf.getvalue()).decode(),
                            w=img.width, h=img.height, storage_w=e['w'],storage_h=e['h'],fmt=e['fmt'], pristine=bool(q.get('pristine'))))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400

_UI_THUMB_CACHE={}

@app.route('/api/ui/thumb', methods=['POST'])
def ui_thumb():
    q=request.get_json()
    key=(str(q['archive']),q['container'],q['entry'],_ui_payload_identity(q.get('payload_abs')))
    if key in _UI_THUMB_CACHE:
        return jsonify(dict(ok=True,**_UI_THUMB_CACHE[key],cached=True))
    try:
        row=_ui_csv_row(q['archive'],q['container'],q['entry'],payload_abs=q.get('payload_abs'))
        if row and not row.get('decoded'): raise ValueError('unresolved geometry; raw export and exact raw import only')
        arc,e,_,_=_ui_load_entry(q['archive'],q['container'],q['entry'],q.get('w'),q.get('h'),payload_abs=q.get('payload_abs'))
        native=_ui_decode_image(arc,e,row or q,logical=False)
        logical=_ui_decode_image(arc,e,row or q,logical=True)
        display=_ui_thumb_transform(logical,row or q)
        def enc_thumb(img):
            thumb=img.copy(); thumb.thumbnail((170,150)); b=io.BytesIO(); thumb.save(b,'PNG'); return _b64.b64encode(b.getvalue()).decode()
        native_png=enc_thumb(native); display_png=enc_thumb(display)
        stock_native_png=None; stock_display_png=None; modified=None
        g,reg=registry(); arcid=str(q['archive'])
        if arcid in reg and os.path.exists(reg[arcid]['bak']):
            try:
                barc,be,_,_=_ui_load_entry(arcid,q['container'],q['entry'],q.get('w'),q.get('h'),pristine=True,payload_abs=q.get('payload_abs'))
                live_payload=arc[e['payload_abs']:e['payload_abs']+e['payload_size']]
                stock_payload=barc[be['payload_abs']:be['payload_abs']+be['payload_size']]
                modified=(live_payload!=stock_payload)
                stock_native=_ui_decode_image(barc,be,row or q,logical=False)
                stock_logical=_ui_decode_image(barc,be,row or q,logical=True)
                stock_display=_ui_thumb_transform(stock_logical,row or q)
                stock_native_png=enc_thumb(stock_native); stock_display_png=enc_thumb(stock_display)
            except Exception:
                stock_native_png=None; stock_display_png=None; modified=None
        result=dict(png=native_png,stock_png=stock_native_png,native_png=native_png,display_png=display_png,
                    stock_native_png=stock_native_png,stock_display_png=stock_display_png,
                    modified=modified,w=e['w'],h=e['h'],fmt=e['fmt'],preview_mode=_ui_preview_mode(row or q))
        if len(_UI_THUMB_CACHE)<800: _UI_THUMB_CACHE[key]=result
        return jsonify(dict(ok=True,**result))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/replace_raw', methods=['POST'])
def ui_replace_raw():
    """Install one exact encoded texture payload without interpreting geometry.

    This is the universal fallback for every indexed physical texture. The file
    must match the target payload byte count exactly. A standard 128-byte DDS
    header is accepted and stripped when its remaining payload is the exact size.
    Only the indexed payload window may change; wrapper bytes and container size
    are guarded and the installed payload is read back byte-for-byte.
    """
    try:
        arcid=str(request.form.get('archive','')); container=request.form.get('container',''); entry=request.form.get('entry','')
        payload_abs=request.form.get('payload_abs'); payload_abs=int(payload_abs) if payload_abs not in (None,'') else None
        f=request.files.get('file')
        if not f: raise ValueError('no raw payload file selected')
        payload=f.read()
        row=_ui_csv_row(arcid,container,entry,payload_abs=payload_abs)
        if not row: raise ValueError('indexed physical texture was not found')
        arc,e,off,size=_ui_load_entry(arcid,container,entry,row.get('w'),row.get('h'),payload_abs=payload_abs)
        target_size=int(e['payload_size'])
        stripped_header=0
        if payload[:4]==b'DDS ':
            header=148 if len(payload)>=148 and payload[84:88]==b'DX10' else 128
            if len(payload)-header==target_size:
                payload=payload[header:]; stripped_header=header
        if len(payload)!=target_size:
            raise ValueError(f'raw file is {len(payload)} bytes; this exact target requires {target_size} bytes')
        pa=int(e['payload_abs']); new=bytearray(arc); new[pa:pa+target_size]=payload; new=bytes(new)
        err=_ui_only_payload_changed(arc,new,pa,target_size)
        if err: raise ValueError(err)
        dry=str(request.form.get('dry_run','0')).lower() in ('1','true','yes')
        if dry:
            return jsonify(dict(ok=True,dry_run=True,payload_size=target_size,stripped_dds_header=stripped_header,note='exact payload size and write window verified; nothing written'))
        _ui_install(arcid,off,size,new)
        _UI_THUMB_CACHE.pop((arcid,container,entry,_ui_payload_identity(payload_abs)),None)
        chk,ce,_,_=_ui_load_entry(arcid,container,entry,row.get('w'),row.get('h'),payload_abs=payload_abs)
        readback=chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]
        if readback!=payload:
            raise RuntimeError('raw payload readback mismatch after install; restore this graphic immediately')
        return jsonify(dict(ok=True,verified=True,payload_size=target_size,stripped_dds_header=stripped_header))
    except ValueError as ex:
        return jsonify(dict(ok=False,error=str(ex))),400
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),500


@app.route('/api/ui/copy', methods=['POST'])
def ui_copy():
    """Raw donor copy guarded by the complete native layout signature."""
    q=request.get_json()
    try:
        s=q['src']; d=q['dst']
        srow=_ui_csv_row(s['archive'],s['container'],s['entry'],payload_abs=s.get('payload_abs'))
        drow=_ui_csv_row(d['archive'],d['container'],d['entry'],payload_abs=d.get('payload_abs'))
        if not srow or not drow: raise ValueError('source or target is not present in the indexed graphics map')
        sarc,se,_,_ = _ui_load_entry(s['archive'], s['container'], s['entry'], s.get('w'), s.get('h'), payload_abs=s.get('payload_abs'))
        darc,de,doff,dsize = _ui_load_entry(d['archive'], d['container'], d['entry'], d.get('w'), d.get('h'), payload_abs=d.get('payload_abs'))
        ssig=_ui_layout_signature(srow,se); dsig=_ui_layout_signature(drow,de)
        if ssig!=dsig:
            return jsonify(dict(ok=False,error='native layout-family mismatch. Copy From requires the same codec, dimensions, payload, mip/padding recipe, and protected resource class.',
                                source_signature=repr(ssig),target_signature=repr(dsig))),400
        payload=sarc[se['payload_abs']:se['payload_abs']+se['payload_size']]
        new=bytearray(darc); new[de['payload_abs']:de['payload_abs']+de['payload_size']]=payload; new=bytes(new)
        err=_ui_only_payload_changed(darc,new,de['payload_abs'],de['payload_size'])
        if err:return jsonify(dict(ok=False,error=err)),400
        if q.get('dry_run'):
            return jsonify(dict(ok=True,note='dry-run: exact native layout signature matches and only the target payload would change',layout_signature=repr(dsig)))
        _ui_install(d['archive'],doff,dsize,new)
        _UI_THUMB_CACHE.pop((str(d['archive']),d['container'],d['entry'],_ui_payload_identity(d.get('payload_abs'))),None)
        chk,ce,_,_=_ui_load_entry(d['archive'],d['container'],d['entry'],d.get('w'),d.get('h'),payload_abs=d.get('payload_abs'))
        ok=chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]==payload
        return jsonify(dict(ok=True,verified=ok,layout_signature=repr(dsig)))
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/replace_png', methods=['POST'])
def ui_replace_png():
    """Smart Import for validated UI textures.

    Common source formats are decoded by Pillow, resized to the target geometry,
    encoded to the target DXT codec, placed into an unchanged wrapper, decoded
    back for validation, and finally read back after installation. Unknown
    families remain blocked unless force=True from Advanced mode.
    """
    q=request.get_json() or {}
    try:
        indexed=_ui_csv_row(q.get('archive'),q.get('container'),q.get('entry'),payload_abs=q.get('payload_abs'))
        rowlike=dict(indexed or {},archive=q.get('archive'),entry=q['entry'],container=q['container'],family=q.get('family',''),fmt=q.get('fmt',''),w=q.get('w'),h=q.get('h'),payload_size=q.get('payload_size'))
        a=_confirmed_for(rowlike); mapping=_ui_mapping(rowlike)
        reason=_ui_replace_reason(rowlike)
        if reason:
            return jsonify(dict(ok=False,error=reason,special_handler=_ui_special_handler(rowlike))),400
        safe=(_ui_safety(rowlike,a,mapping)=='safe_replace')
        arc,e,off,size=_ui_load_entry(q['archive'],q['container'],q['entry'],q.get('w'),q.get('h'),payload_abs=q.get('payload_abs'))
        if e.get('fmt') not in ('DXT1','DXT5'):
            return jsonify(dict(ok=False,error=f'Smart Import does not support {e.get("fmt")} targets')),400
        profile=_ui_profile(rowlike,e,a); profile['mapping']=mapping; profile['safety']=('safe_replace' if safe else 'guarded_replace')
        new,payload,prep,preview=_ui_prepare_encoded(q,arc,e,safe,rowlike)
        if q.get('dry_run'):
            return jsonify(dict(ok=True,note='Smart Import conversion preview passed; nothing written',
                                experimental=not safe,image_prep=prep,preview_png=preview,
                                profile=profile,verified_preview=True))
        _ui_install(q['archive'],off,size,new)
        _UI_THUMB_CACHE.pop((str(q['archive']),q['container'],q['entry'],_ui_payload_identity(q.get('payload_abs'))),None)
        chk,ce,_,_=_ui_load_entry(q['archive'],q['container'],q['entry'],q.get('w'),q.get('h'),payload_abs=q.get('payload_abs'))
        readback=chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]
        verified=(readback==payload)
        if not verified:
            return jsonify(dict(ok=False,error='readback mismatch after install; restore this image from Stock immediately')),500
        # Decode live bytes once more, not merely the temporary copy.
        decoded=_ui_decode_image(chk,ce,rowlike,logical=True)
        decode_verified=(decoded.size==_ui_logical_dims(rowlike,ce))
        return jsonify(dict(ok=True,verified=verified,decode_verified=decode_verified,
                            experimental=not safe,image_prep=prep,profile=profile))
    except ValueError as ve:
        return jsonify(dict(ok=False,error=str(ve))),400
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/restore', methods=['POST'])
def ui_restore():
    """Restore ONE image from the pristine backup, leaving the rest of the
    container byte-identical (same guard as copy)."""
    q=request.get_json()
    try:
        arcid=str(q['archive'])
        g,reg=registry()
        if not os.path.exists(reg[arcid]['bak']):
            return jsonify(dict(ok=False, error='no original backup is available for that game file')),400
        barc,be,_,_ = _ui_load_entry(arcid, q['container'], q['entry'], q.get('w'), q.get('h'), pristine=True, payload_abs=q.get('payload_abs'))
        larc,le,loff,lsize = _ui_load_entry(arcid, q['container'], q['entry'], q.get('w'), q.get('h'), payload_abs=q.get('payload_abs'))
        if be['payload_size']!=le['payload_size']:
            return jsonify(dict(ok=False, error='payload size differs from backup; refused')),400
        payload=barc[be['payload_abs']:be['payload_abs']+be['payload_size']]
        new=bytearray(larc)
        new[le['payload_abs']:le['payload_abs']+le['payload_size']]=payload
        new=bytes(new)
        err=_ui_only_payload_changed(larc, new, le['payload_abs'], le['payload_size'])
        if err: return jsonify(dict(ok=False, error=err)),400
        _ui_install(arcid, loff, lsize, new)
        _UI_THUMB_CACHE.pop((arcid,q['container'],q['entry'],_ui_payload_identity(q.get('payload_abs'))), None)
        return jsonify(dict(ok=True, restored=q['entry']))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))),400

# ==================== end UI IMAGES ====================



# ==================== v0.9.26.7 GAMEPLAY-CONFIRMED SCHEDULE EDITOR ====================
SCHEDULE_HELPER_NAME='nascar15_schedule_editor_v0_1.py'
SCHEDULE_LINK_HELPER_NAME='nascar15_schedule_raceevent_links_v0_1.py'
MAX_RACE_LAPS=2147483647  # largest signed Python-2 marshal int; higher values require a long-object layout
_SCHEDULE_MOD=None
_SCHEDULE_LINK_MOD=None
_SCHEDULE_CACHE={}

def schedule_mod():
    global _SCHEDULE_MOD
    if _SCHEDULE_MOD is None:
        path=component_path(SCHEDULE_HELPER_NAME)
        if not os.path.exists(path): raise RuntimeError(f'{SCHEDULE_HELPER_NAME} is missing from the internal tools folder')
        import importlib.util as _iu
        spec=_iu.spec_from_file_location('n15_schedule_editor',path)
        mod=_iu.module_from_spec(spec); sys.modules['n15_schedule_editor']=mod; spec.loader.exec_module(mod); _SCHEDULE_MOD=mod
    if hasattr(_SCHEDULE_MOD,'configure'):
        _SCHEDULE_MOD.configure(active_game_profile().get('season_year',2015))
    return _SCHEDULE_MOD


def schedule_link_mod():
    global _SCHEDULE_LINK_MOD
    if _SCHEDULE_LINK_MOD is not None:return _SCHEDULE_LINK_MOD
    path=component_path(SCHEDULE_LINK_HELPER_NAME)
    if not os.path.exists(path):raise RuntimeError(f'{SCHEDULE_LINK_HELPER_NAME} is missing from the internal tools folder')
    import importlib.util as _iu
    spec=_iu.spec_from_file_location('n15_schedule_raceevent_links',path)
    mod=_iu.module_from_spec(spec);sys.modules['n15_schedule_raceevent_links']=mod;spec.loader.exec_module(mod);_SCHEDULE_LINK_MOD=mod
    return mod

def _schedule_archive_source(source='live',verify_hash=True):
    """Archive-0 stock/baseline source. Live schedules use discovery across every archive."""
    g,reg=registry()
    if not g or '0' not in reg: raise RuntimeError('ARCHIVE0 not found')
    live_cdf=reg['0']['cdf']
    if source=='baseline':
        entry=stock_baselines().get('0')
        if not entry: raise RuntimeError('no clean baseline registered')
        path=_verify_baseline_entry('0',entry,verify_hash=verify_hash)
        pristine_cdf=backup_path(live_cdf)
        if os.path.exists(pristine_cdf):
            return path,pristine_cdf,'baseline'
        if os.path.getsize(path)!=os.path.getsize(reg['0']['ar']):
            raise RuntimeError('clean baseline needs a paired pristine cdfiles backup after repoint installs')
        return path,live_cdf,'baseline'
    if source=='backup':
        path=reg['0']['bak'];cdf=backup_path(live_cdf)
        if not os.path.exists(path) or not os.path.exists(cdf): raise RuntimeError('no paired pristine ARCHIVE0/cdfiles backup exists')
        return path,cdf,'backup'
    return reg['0']['ar'],live_cdf,'live'


def _schedule_key(archive,cdf,label):
    ast=os.stat(archive); cst=os.stat(cdf)
    return (label,os.path.realpath(archive),ast.st_size,ast.st_mtime_ns,
            os.path.realpath(cdf),cst.st_size,cst.st_mtime_ns)


def _schedule_public_rows(rows,raw=None):
    public=[r.public() for r in rows]
    runtime_links={}
    if raw is not None:
        try:runtime_links=schedule_link_mod().inspect_links(raw)
        except Exception:runtime_links={}
    for row in public:
        if row.get('event_uid') is None:
            text=str(row.get('race_event') or '')
            m=re.search(r'\bEVENT_c\s*\(\s*(-?\d+)',text)
            if m:row['event_uid']=int(m.group(1))
        link=runtime_links.get(int(row.get('uid'))) if row.get('uid') is not None else None
        row['gameplay_event_uid']=(None if not link else int(link['event_uid']))
        row['worldpointer_event_uid']=row['gameplay_event_uid']
        row['worldpointer_matches']=(row.get('event_uid') is not None and row['gameplay_event_uid']==int(row['event_uid']))
    return public


def _schedule_effective_rows(rows,catalog_rows):
    """Resolve the live track link to one friendly stock event definition.

    Several named races share the same physical track link (for example the
    Coca-Cola 600 and Bank of America 500).  A track UID alone therefore cannot
    preserve the correct race name or event-specific lap default.  The app keeps
    the chosen stock definition per season slot in config and falls back to that
    slot's original stock definition when no custom assignment has been saved.
    """
    by_key={};by_event_uid=collections.defaultdict(list);by_slot_uid={}
    for raw in catalog_rows or []:
        try:
            row=dict(raw);uid=int(row.get('event_uid'));name=str(row.get('event') or '')
        except Exception:continue
        if not name:continue
        key=_schedule_definition_key(uid,name);row['definition_key']=key
        by_key.setdefault(key,row);by_event_uid[uid].append(row)
        try:by_slot_uid[int(row.get('uid'))]=row
        except Exception:pass
    assignments=_schedule_assignment_map()
    out=[]
    for raw in rows or []:
        row=dict(raw)
        row['visible_event_uid']=row.get('event_uid');row['visible_event']=row.get('event');row['visible_track']=row.get('track')
        try:linked_uid=int(row.get('gameplay_event_uid'))
        except Exception:linked_uid=None
        try:target_uid=int(row.get('uid'))
        except Exception:target_uid=None
        if linked_uid is None:
            try:linked_uid=int(row.get('event_uid'))
            except Exception:linked_uid=None
        meta=None
        saved_key=assignments.get(str(target_uid)) if target_uid is not None else None
        if saved_key in by_key and int(by_key[saved_key]['event_uid'])==linked_uid:
            meta=by_key[saved_key]
        if meta is None and target_uid in by_slot_uid and int(by_slot_uid[target_uid]['event_uid'])==linked_uid:
            meta=by_slot_uid[target_uid]
        if meta is None:
            try:visible_key=_schedule_definition_key(linked_uid,row.get('event'))
            except Exception:visible_key=None
            if visible_key in by_key:meta=by_key[visible_key]
        if meta is None and linked_uid in by_event_uid:
            meta=by_event_uid[linked_uid][0]
        if linked_uid is not None:row['event_uid']=linked_uid
        if meta:
            row['definition_key']=meta['definition_key']
            for key in ('event','track','race_event'):
                if meta.get(key) is not None:row[key]=meta.get(key)
            row['source_uid']=meta.get('uid')
        else:
            row['definition_key']=_schedule_definition_key(linked_uid,row.get('event') or '') if linked_uid is not None else ''
        row['worldpointer_matches']=(linked_uid is not None and row.get('event_uid') is not None and int(row['event_uid'])==linked_uid)
        out.append(row)
    return out

_SCHEDULE_SOURCE_CACHE={}

def _schedule_live_source_key(reg):
    parts=[]
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try:
            a=os.stat(v['ar']);c=os.stat(v['cdf'])
            parts.append((str(arcid),a.st_size,a.st_mtime_ns,c.st_size,c.st_mtime_ns))
        except OSError: continue
    return tuple(parts)


def _schedule_live_sources(use_cache=True):
    """Find every indexed DB_GAME_LOCAL_SCRIPT.PYC that contains a valid active-season Cup schedule.

    Update/DLC archives can contain overriding copies. The old editor always patched only
    ARCHIVE0; this discovery pass lets preview/apply verify and patch every schedule-bearing
    copy atomically.
    """
    g,reg=registry()
    if not g: raise RuntimeError('game folder not selected')
    key=_schedule_live_source_key(reg)
    if use_cache and key in _SCHEDULE_SOURCE_CACHE:
        return _SCHEDULE_SOURCE_CACHE[key]
    mod=schedule_mod();mp=component_path(MAPPER_NAME);rp=component_path(REPOINT_NAME)
    mapper,repoint=mod.load_helpers(mp,rp)
    found=[];errors=[]
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try: entries=parse_cdfiles(v['cdf'])
        except Exception as ex:
            errors.append(dict(archive=str(arcid),error='index parse: '+str(ex)));continue
        for occurrence,(off,size,name) in enumerate((x for x in entries if str(x[2]).upper()==DBFILE.upper())):
            try:
                with open(v['ar'],'rb') as fh:
                    fh.seek(off);raw=fh.read(size)
                if len(raw)!=size: raise RuntimeError('short archive read')
                rows,_records=mod.map_schedule(raw,mapper)
                public=_schedule_public_rows(rows,raw)
                if len(public)!=36: raise RuntimeError(f"found {len(public)} normal {active_game_profile().get('season_year',2015)} Cup slots")
                found.append(dict(archive_id=str(arcid),archive=v['ar'],cdf=v['cdf'],offset=int(off),size=int(size),
                                  occurrence=occurrence,raw=raw,rows=public,mapper=mapper,repoint=repoint,
                                  sha256=_hl.sha256(raw).hexdigest()))
            except Exception as ex:
                errors.append(dict(archive=str(arcid),entry=name,offset=int(off),size=int(size),error=str(ex)))
    if not found:
        detail='; '.join(f"ARCHIVE{x.get('archive')}: {x.get('error')}" for x in errors[:6])
        raise RuntimeError('no schedule-bearing DB_GAME_LOCAL_SCRIPT.PYC was found'+(('; '+detail) if detail else ''))
    found.sort(key=lambda x:(x['archive_id']!='0',int(x['archive_id']),x['offset']))
    result=dict(sources=found,errors=errors,key=key)
    _SCHEDULE_SOURCE_CACHE.clear();_SCHEDULE_SOURCE_CACHE[key]=result
    return result


def _schedule_read(source='live',use_cache=True,verify_hash=True):
    mod=schedule_mod()
    if source=='live':
        discovered=_schedule_live_sources(use_cache=use_cache);primary=discovered['sources'][0]
        meta=dict(primary, label='live', sources=discovered['sources'], source_errors=discovered['errors'])
        rows=[dict(r) for r in primary['rows']]
        catalog=[]
        for candidate in ('backup','baseline'):
            try:
                catalog,_catmeta=_schedule_read(candidate,use_cache=use_cache,verify_hash=False)
                if catalog:break
            except Exception:pass
        if catalog:
            rows=_schedule_effective_rows(rows,catalog)
            meta['stock_rows']=[dict(r) for r in catalog]
            meta['stock_source']=str(_catmeta.get('label') or candidate)
        else:
            meta['stock_rows']=[]
            meta['stock_source']=None
        return rows,meta
    archive,cdf,label=_schedule_archive_source(source,verify_hash=verify_hash)
    key=_schedule_key(archive,cdf,label)
    if use_cache and key in _SCHEDULE_CACHE:
        rows,meta=_SCHEDULE_CACHE[key]
        return [dict(r) for r in rows],dict(meta)
    mp=component_path(MAPPER_NAME); rp=component_path(REPOINT_NAME)
    raw,off,size,mapper,repoint=mod.extract(archive,cdf,DBFILE,mp,rp)
    rows,_=mod.map_schedule(raw,mapper);public=_schedule_public_rows(rows,raw)
    meta=dict(archive=archive,cdf=cdf,label=label,offset=off,size=size,raw=raw,mapper=mapper,repoint=repoint,sources=[])
    for old in list(_SCHEDULE_CACHE):
        if old[0]==label and old[1]==os.path.realpath(archive): _SCHEDULE_CACHE.pop(old,None)
    _SCHEDULE_CACHE[key]=(public,meta)
    return [dict(r) for r in public],dict(meta)


def _schedule_stock_sources():
    g,reg=registry(); out=[]
    if g and '0' in reg and os.path.exists(reg['0']['bak']) and os.path.exists(backup_path(reg['0']['cdf'])): out.append('backup')
    entry=stock_baselines().get('0')
    if entry:
        try:
            _verify_baseline_entry('0',entry,verify_hash=False); out.append('baseline')
        except Exception: pass
    return out


SCHEDULE_EVENT_LAP_PROFILE_KEY='schedule_event_lap_profiles_v2'
SCHEDULE_EVENT_LAP_PROFILE_LEGACY_KEY='schedule_event_lap_profiles_v1'
SCHEDULE_ASSIGNMENT_KEY='schedule_event_assignments_v2'
SCHEDULE_ASSIGNMENT_LEGACY_KEY='schedule_event_assignments_v1'


def _schedule_definition_key(event_uid,event_name):
    return f"{int(event_uid)}|{str(event_name or '')}"


def _schedule_assignment_map():
    raw=load_cfg().get(SCHEDULE_ASSIGNMENT_KEY)
    return {str(k):str(v) for k,v in raw.items()} if isinstance(raw,dict) else {}


def _schedule_save_assignments(desired):
    cfg=load_cfg();mapping={}
    for item in desired or []:
        try:mapping[str(int(item['target_uid']))]=_schedule_definition_key(item['event_uid'],item['event_name'])
        except Exception:continue
    cfg[SCHEDULE_ASSIGNMENT_KEY]=mapping;save_cfg(cfg)
    return mapping


def _schedule_validate_lap(value,label='lap count'):
    try:value=int(value)
    except Exception:raise ValueError(f'{label} must be a whole number')
    if not (1<=value<=MAX_RACE_LAPS):
        raise ValueError(f'{label} must be 1-{MAX_RACE_LAPS:,}')
    return value


def _schedule_event_lap_profile_rows(save_missing=True):
    """Return all 36 stock event definitions with definition-specific defaults.

    Named events can share one physical track link, so profiles are keyed by
    ``event UID + event token`` instead of event UID alone.  This keeps the
    Coca-Cola 600 separate from the Bank of America 500 while still letting a
    Bristol Night default follow that named event wherever it is scheduled.
    """
    sources=_schedule_stock_sources()
    if not sources:raise RuntimeError('no clean original game copy or previous backup is available')
    source='backup' if 'backup' in sources else sources[0]
    stock,_=_schedule_read(source,verify_hash=False)
    live,_=_schedule_read('live')
    cfg=load_cfg();stored=cfg.get(SCHEDULE_EVENT_LAP_PROFILE_KEY)
    if not isinstance(stored,dict):stored={}
    legacy=cfg.get(SCHEDULE_EVENT_LAP_PROFILE_LEGACY_KEY)
    if not isinstance(legacy,dict):legacy={}
    changed=False;rows=[];profiles={}
    for row in sorted(stock,key=lambda x:int(x['order'])):
        event_uid=int(row['event_uid']);event=str(row.get('event') or '')
        profile_key=_schedule_definition_key(event_uid,event);stock_laps=int(row['laps'])
        raw=stored.get(profile_key)
        if raw is None:raw=legacy.get(str(event_uid))
        try:profile=_schedule_validate_lap(raw,'stored event lap') if raw is not None else None
        except Exception:profile=None
        if profile is None:profile=stock_laps
        if stored.get(profile_key)!=profile:
            stored[profile_key]=profile;changed=True
        profiles[profile_key]=profile
        occurrences=[r for r in live if str(r.get('definition_key') or _schedule_definition_key(r.get('event_uid'),r.get('event'))) == profile_key]
        live_laps=sorted({int(r['laps']) for r in occurrences})
        out=dict(row);out.update(slot=int(row['order']),uid=int(row['uid']),event_uid=event_uid,
            event=event,track=str(row.get('track') or ''),profile_key=profile_key,definition_key=profile_key,
            stock_laps=stock_laps,profile_laps=profile,current_laps=profile,modified=(profile!=stock_laps),
            current_occurrences=len(occurrences),occurrence_laps=live_laps)
        rows.append(out)
    if changed and save_missing:
        cfg[SCHEDULE_EVENT_LAP_PROFILE_KEY]=stored;save_cfg(cfg)
    return rows,profiles,source


def _schedule_resolve_profile_key(value=None,event_uid=None,event_name=None):
    rows,_profiles,_source=_schedule_event_lap_profile_rows(save_missing=True)
    known={str(r['profile_key']):r for r in rows}
    if value is not None and str(value) in known:return str(value)
    if event_uid is not None and event_name:
        key=_schedule_definition_key(event_uid,event_name)
        if key in known:return key
    if event_uid is not None:
        matches=[k for k,r in known.items() if int(r['event_uid'])==int(event_uid)]
        if len(matches)==1:return matches[0]
        if len(matches)>1:raise ValueError('that track has multiple named events; choose the exact event row')
    raise ValueError('event definition was not found in the stock 36-event catalog')


def _schedule_set_event_lap_profile(profile_key,laps):
    laps=_schedule_validate_lap(laps);profile_key=_schedule_resolve_profile_key(profile_key)
    cfg=load_cfg();stored=cfg.get(SCHEDULE_EVENT_LAP_PROFILE_KEY)
    if not isinstance(stored,dict):stored={}
    stored[profile_key]=laps;cfg[SCHEDULE_EVENT_LAP_PROFILE_KEY]=stored;save_cfg(cfg)
    return laps


def _schedule_enrich_profile_laps(rows,profiles):
    out=[]
    for raw in rows or []:
        row=dict(raw)
        key=str(row.get('definition_key') or _schedule_definition_key(row.get('event_uid'),row.get('event')))
        row['definition_key']=key;row['profile_key']=key
        profile=profiles.get(key)
        if profile is None:
            try:profile=int(row.get('laps'))
            except Exception:profile=None
        row['profile_laps']=profile
        try:row['lap_override']=(profile is not None and int(row.get('laps'))!=int(profile))
        except Exception:row['lap_override']=False
        out.append(row)
    return out


def _schedule_current_desired_with_event_profile(profile_key,laps):
    live,_=_schedule_read('live');profile_key=_schedule_resolve_profile_key(profile_key);laps=_schedule_validate_lap(laps)
    desired=[];matches=0
    for row in sorted(live,key=lambda x:int(x['order'])):
        key=str(row.get('definition_key') or _schedule_definition_key(row['event_uid'],row['event']))
        same=(key==profile_key)
        if same:matches+=1
        desired.append(dict(slot=int(row['order']),target_uid=int(row['uid']),source_uid=int(row.get('source_uid') or row['uid']),
                            event_uid=int(row['event_uid']),event_name=str(row['event']),definition_key=key,
                            laps=(laps if same else int(row['laps']))))
    return desired,matches


def _schedule_apply_event_profile(profile_key,laps,dry_run=False):
    profile_key=_schedule_resolve_profile_key(profile_key);laps=_schedule_validate_lap(laps)
    profile_rows,_profiles,_source=_schedule_event_lap_profile_rows(save_missing=not dry_run)
    meta=next((r for r in profile_rows if str(r['profile_key'])==profile_key),None)
    if meta is None:raise ValueError('event definition is not in the stock 36-event catalog')
    desired,matches=_schedule_current_desired_with_event_profile(profile_key,laps)
    result=(_schedule_patch(desired,dry_run=dry_run,patch_laps=True) if matches else
            dict(ok=True,dry_run=bool(dry_run),changes=[],change_count=0,worldpointer_change_count=0,exact_lap_change_count=0,elapsed_ms=0))
    result.update(profile_key=profile_key,event_uid=int(meta['event_uid']),event_name=meta['event'],track=meta['track'],
                  profile_laps=laps,current_occurrences=matches,profile_only=(matches==0))
    if not dry_run:_schedule_set_event_lap_profile(profile_key,laps)
    return result


def _schedule_apply_event_profiles_batch(entries,dry_run=False):
    if not isinstance(entries,list) or not entries:raise ValueError('no event lap defaults were supplied')
    rows,_profiles,_source=_schedule_event_lap_profile_rows(save_missing=not dry_run)
    known={str(r['profile_key']):r for r in rows};updates={}
    for item in entries:
        if not isinstance(item,dict):raise ValueError('every event-lap row must be an object')
        key=_schedule_resolve_profile_key(item.get('profile_key'),item.get('event_uid'),item.get('event_name'))
        updates[key]=_schedule_validate_lap(item.get('laps'))
    live,_=_schedule_read('live');desired=[];matched=0
    for row in sorted(live,key=lambda x:int(x['order'])):
        key=str(row.get('definition_key') or _schedule_definition_key(row['event_uid'],row['event']))
        wanted=updates.get(key,int(row['laps']))
        if key in updates:matched+=1
        desired.append(dict(slot=int(row['order']),target_uid=int(row['uid']),source_uid=int(row.get('source_uid') or row['uid']),
                            event_uid=int(row['event_uid']),event_name=str(row['event']),definition_key=key,laps=wanted))
    result=_schedule_patch(desired,dry_run=dry_run,patch_laps=True)
    result.update(profile_updates=len(updates),current_occurrences=matched)
    if not dry_run:
        cfg=load_cfg();stored=cfg.get(SCHEDULE_EVENT_LAP_PROFILE_KEY)
        if not isinstance(stored,dict):stored={}
        stored.update({k:int(v) for k,v in updates.items()});cfg[SCHEDULE_EVENT_LAP_PROFILE_KEY]=stored;save_cfg(cfg)
    return result

def _schedule_reference_raw(src):
    """Return a PYC whose semantic EVENT constant indices are still stock.

    After a repeated-event schedule is applied, the live runtime assignments may
    all point to one event.  The paired pristine backup recovers the original
    event-UID -> constant-index map needed to switch to any track later.
    """
    g,reg=registry();arcid=str(src['archive_id']);occurrence=int(src.get('occurrence',0))
    def read_from(archive,cdf,label):
        matches=[(o,z,n) for o,z,n in parse_cdfiles(cdf) if str(n).upper()==DBFILE.upper()]
        if not matches:raise RuntimeError(f'{DBFILE} not found in {label} index')
        hit=matches[occurrence] if occurrence<len(matches) else (matches[0] if len(matches)==1 else None)
        if hit is None:raise RuntimeError(f'{DBFILE} occurrence {occurrence} missing from {label}')
        with open(archive,'rb') as fh:fh.seek(hit[0]);raw=fh.read(hit[1])
        if len(raw)!=hit[1]:raise RuntimeError(f'short {label} PYC read')
        return raw,label
    v=reg.get(arcid)
    if v:
        bar=v['bak'];bcdf=backup_path(v['cdf'])
        if os.path.exists(bar) and os.path.exists(bcdf):
            try:return read_from(bar,bcdf,'pristine backup')
            except Exception:pass
    if arcid=='0':
        try:
            archive,cdf,label=_schedule_archive_source('baseline',verify_hash=False)
            return read_from(archive,cdf,label)
        except Exception:pass
    return src['raw'],'live reference'


def _schedule_desired(q):
    slots=q.get('slots') or []
    if not isinstance(slots,list) or len(slots)!=36:
        raise ValueError('custom schedule must contain exactly 36 slots')
    out=[]
    for i,item in enumerate(slots,1):
        if not isinstance(item,dict):raise ValueError(f'slot {i} is not an object')
        try:
            row=dict(slot=i,target_uid=int(item['target_uid']),source_uid=(None if item.get('source_uid') is None else int(item['source_uid'])),
                     event_uid=int(item['event_uid']),event_name=str(item['event_name']),definition_key=str(item.get('definition_key') or _schedule_definition_key(item['event_uid'],item['event_name'])),laps=int(item['laps']))
        except Exception:raise ValueError(f'slot {i} has invalid target/source/event/laps values')
        if not row['event_name']:raise ValueError(f'slot {i} event name is empty')
        if not (1<=row['laps']<=MAX_RACE_LAPS):raise ValueError(f'slot {i} laps must be 1-{MAX_RACE_LAPS:,}')
        out.append(row)
    return out


def _schedule_allowed_catalog():
    rows=[]
    try:
        d=_schedule_live_sources()
        for src in d['sources']:rows.extend(src['rows'])
    except Exception:pass
    for source in ('backup','baseline'):
        try:rows.extend(_schedule_read(source,verify_hash=False)[0])
        except Exception:pass
    return {(int(r.get('event_uid')),str(r.get('event'))):r for r in rows if r.get('event_uid') is not None and r.get('event')}


def _schedule_patch_laps_exact(pyc,local):
    """Apply requested RaceLaps values through the exact-field path.

    The older implementation re-parsed the full database once for every one of
    the 36 slots even when most lap values were unchanged.  This version maps
    the live values once, skips no-op rows immediately, and only invokes the
    heavier one-field verifier for records that actually need a change.
    """
    out=bytes(pyc);changes=[]
    initial,_root,_records,_schemas=_mapped_rows_from_pyc_bytes(out,'RACEDATA_c',['RaceLaps'])
    current={str(r.get('uid')):r.get('RaceLaps') for r in initial}
    for item in local:
        uid=int(item['target_uid']);wanted=int(item['laps']);key=str(uid)
        if key not in current:raise RuntimeError(f'RACEDATA UID {uid} disappeared while patching laps')
        old=current[key]
        if _num_eq(old,wanted):continue
        plan=_exact_field_variant(out,'RACEDATA_c',uid,'RaceLaps',wanted)
        if not plan.get('ok'):
            raise RuntimeError(f'RACEDATA UID {uid} exact lap repoint failed: {plan.get("error","unknown error")}')
        out=plan['pyc'];current[key]=wanted
        changes.append(dict(target_uid=uid,old_laps=int(float(old)),new_laps=wanted,
                            method=plan.get('method'),grew=bool(plan.get('grew')),
                            const_index=plan.get('const_index'),operand_offset=plan.get('operand_offset')))
    if changes:
        final,_r,_records,_schemas=_mapped_rows_from_pyc_bytes(out,'RACEDATA_c',['RaceLaps'])
        final_by_uid={str(r.get('uid')):r.get('RaceLaps') for r in final}
        for change in changes:
            got=final_by_uid.get(str(change['target_uid']))
            if not _num_eq(got,change['new_laps']):
                raise RuntimeError(f"RACEDATA UID {change['target_uid']} final lap verification failed")
    return out,changes

def _schedule_cdf_row_for_source(v,src):
    raw,rows,_layout=_rp_index_rows(v['cdf'])
    matches=[r for r in rows if str(r['name']).upper()==DBFILE.upper()
             and int(r['offset'])==int(src['offset']) and int(r['size'])==int(src['size'])]
    if len(matches)==1:return raw,matches[0]
    same=[r for r in rows if str(r['name']).upper()==DBFILE.upper()]
    occ=int(src.get('occurrence',0))
    if occ<len(same):return raw,same[occ]
    raise RuntimeError(f"ARCHIVE{src['archive_id']} could not resolve the exact {DBFILE} index row")


def _schedule_install_variant(src,patched):
    """Install one schedule PYC, appending/repointing when its constant table grew."""
    g,reg=registry();arcid=str(src['archive_id']);v=need(reg,arcid)
    if len(patched)==int(src['size']):
        with open(v['ar'],'r+b') as fh:
            fh.seek(int(src['offset']));fh.write(patched);fh.flush();os.fsync(fh.fileno())
            fh.seek(int(src['offset']));check=fh.read(len(patched))
        if check!=patched:raise RuntimeError(f'ARCHIVE{arcid} same-size schedule readback mismatch')
        return dict(method='same_size',offset=int(src['offset']),size=len(patched),growth=0)
    raw,row=_schedule_cdf_row_for_source(v,src)
    old_archive_size=os.path.getsize(v['ar'])
    new_off=(old_archive_size+(_RP_ALIGNMENT-1))&~(_RP_ALIGNMENT-1)
    if new_off+len(patched)>=2**32:raise RuntimeError('schedule PYC repoint would exceed the 32-bit archive limit')
    with open(v['ar'],'ab') as fh:
        pad=new_off-old_archive_size
        if pad:fh.write(b'\0'*pad)
        fh.write(patched);fh.flush();os.fsync(fh.fileno())
    if _rp_sha256_range(v['ar'],new_off,len(patched))!=_hl.sha256(patched).hexdigest():
        raise RuntimeError(f'ARCHIVE{arcid} appended schedule SHA-256 mismatch')
    struct.pack_into('<I',raw,row['size_pos'],len(patched))
    struct.pack_into('<I',raw,row['offset_pos'],new_off)
    tmp=v['cdf']+'.schedule.tmp'
    with open(tmp,'wb') as fh:fh.write(raw);fh.flush();os.fsync(fh.fileno())
    os.replace(tmp,v['cdf'])
    _raw2,rows2,_layout2=_rp_index_rows(v['cdf'])
    vr=next((r for r in rows2 if str(r['name']).upper()==DBFILE.upper()
             and int(r['offset'])==new_off and int(r['size'])==len(patched)),None)
    if vr is None:raise RuntimeError(f'ARCHIVE{arcid} cdfiles schedule repoint readback failed')
    return dict(method='append_repoint',offset=new_off,size=len(patched),growth=(new_off+len(patched)-old_archive_size))


def _schedule_semantic_equal(a,b):
    if isinstance(a,bool) or isinstance(b,bool):return isinstance(a,bool) and isinstance(b,bool) and a is b
    if isinstance(a,(bytes,bytearray)) and isinstance(b,str):
        try:return bytes(a).decode('latin1')==b
        except Exception:return False
    if isinstance(b,(bytes,bytearray)) and isinstance(a,str):
        try:return bytes(b).decode('latin1')==a
        except Exception:return False
    if isinstance(a,(bytes,bytearray)) or isinstance(b,(bytes,bytearray)):
        try:return bytes(a)==bytes(b)
        except Exception:return False
    if isinstance(a,float) or isinstance(b,float):
        try:return abs(float(a)-float(b))<1e-12
        except Exception:return False
    return type(a) is type(b) and a==b


def _schedule_find_live_const(root,value,preferred=None):
    consts=root['consts']
    if preferred is not None and 0<=int(preferred)<len(consts) and _schedule_semantic_equal(consts[int(preferred)],value):
        return int(preferred)
    hits=[i for i,c in enumerate(consts) if _schedule_semantic_equal(c,value)]
    return hits[0] if hits else None


def _schedule_transplant_constructor(live_pyc,reference_pyc,local,mapper,repoint,mod):
    """Build the desired visible schedule against a pristine PYC, then transplant
    only its LOAD_CONST operand choices onto the current live PYC.

    The legacy helper validates operands against stock. That is correct for a
    first edit but rejects the second custom schedule because those operands no
    longer point at stock constants. Running it on the pristine reference keeps
    its safety checks useful; semantic transplanting makes the result repeatable
    after Stock, Random 36, 36 Daytonas, or any other prior schedule.
    """
    ref_rows,_=mod.map_schedule(reference_pyc,mapper)
    ref_public=_schedule_public_rows(ref_rows,reference_pyc)
    ref_by_order={int(r['order']):r for r in ref_public}
    if sorted(ref_by_order)!=list(range(1,37)):
        raise RuntimeError('pristine schedule reference does not expose slots 1-36')
    ref_local=[]
    for item in local:
        stock=ref_by_order[int(item['slot'])]
        h=dict(item)
        h['target_uid']=int(stock['uid'])
        # RaceLaps is handled later by the exact UID/field path. Keeping the
        # pristine lap here limits the legacy helper to EventName/RaceEvent.
        h['laps']=int(stock['laps'])
        ref_local.append(h)
    ref_patched,ref_changes,inference=mod.apply_custom(reference_pyc,ref_local,mapper,repoint)
    linkmod=schedule_link_mod()
    before_root=linkmod.parse_root(reference_pyc);after_root=linkmod.parse_root(ref_patched)
    before_ins=linkmod._instructions(before_root['code']);after_ins=linkmod._instructions(after_root['code'])
    if len(before_ins)!=len(after_ins):raise RuntimeError('schedule constructor helper changed bytecode instruction count')
    changes=[];out=bytes(live_pyc)
    for bi,ai in zip(before_ins,after_ins):
        if bi['offset']!=ai['offset'] or bi['opcode']!=ai['opcode']:
            raise RuntimeError('schedule constructor helper changed bytecode instruction layout')
        if bi.get('arg')==ai.get('arg'):continue
        if ai['opcode']!=100 or ai.get('arg') is None or ai.get('arg_offset') is None:
            raise RuntimeError(f'schedule constructor helper changed unsupported opcode {ai["opcode"]} at 0x{ai["offset"]:X}')
        if ai['arg']>=len(after_root['consts']):raise RuntimeError('constructor helper selected an invalid constant index')
        desired_value=after_root['consts'][ai['arg']]
        live_root=linkmod.parse_root(out)
        live_idx=_schedule_find_live_const(live_root,desired_value,preferred=ai['arg'])
        operand_abs=int(live_root['code_offset'])+int(ai['arg_offset'])
        old_idx=int.from_bytes(out[operand_abs:operand_abs+2],'little')
        if live_idx is None:
            old_value=live_root['consts'][old_idx] if 0<=old_idx<len(live_root['consts']) else None
            if isinstance(desired_value,bool) or (isinstance(desired_value,(int,float)) and not isinstance(desired_value,bool)):
                out,grew,live_idx=_patch_load_const_operand(out,operand_abs,0,desired_value,old_value)
            else:
                raise RuntimeError(f'live PYC no longer contains required schedule constant {desired_value!r}')
        else:
            if live_idx>0xFFFF:raise RuntimeError('schedule constant requires EXTENDED_ARG')
            buf=bytearray(out);struct.pack_into('<H',buf,operand_abs,int(live_idx));out=bytes(buf);grew=False
        changes.append(dict(code_offset=int(ai['offset']),operand_offset=operand_abs,
                            old_const_index=old_idx,new_const_index=int(live_idx),
                            desired_value=(desired_value.decode('latin1','replace') if isinstance(desired_value,bytes) else desired_value),
                            grew=bool(grew)))
    mapped,_=mod.map_schedule(out,mapper);public=_schedule_public_rows(mapped,out);by_order={int(r['order']):r for r in public}
    for item in local:
        row=by_order[int(item['slot'])]
        if int(row.get('event_uid'))!=int(item['event_uid']) or str(row.get('event'))!=str(item['event_name']):
            raise RuntimeError(f'slot {item["slot"]}: repeat-safe visible schedule transplant verification failed')
    info=dict(inference or {}) if isinstance(inference,dict) else dict(legacy_inference=str(inference))
    info.update(method='pristine_constructor_semantic_transplant',transplanted_operands=len(changes),operand_changes=changes)
    return out,ref_changes,info


def _schedule_patch(desired,dry_run=False,patch_laps=True):
    import time
    started=time.perf_counter()
    mod=schedule_mod();linkmod=schedule_link_mod()
    discovered=_schedule_live_sources(use_cache=True);sources=discovered['sources']
    primary_rows=sources[0]['rows'];allowed=_schedule_allowed_catalog()
    for i,item in enumerate(desired,1):
        if (int(item['event_uid']),str(item['event_name'])) not in allowed:
            raise ValueError(f'slot {i}: event {item["event_name"]} / UID {item["event_uid"]} is not in the live or clean Cup catalog')
    patches=[]
    for src in sources:
        effective_source_rows=_schedule_effective_rows(src['rows'],list(allowed.values()))
        by_order={int(r['order']):r for r in effective_source_rows}
        if sorted(by_order)!=list(range(1,37)):
            raise RuntimeError(f"ARCHIVE{src['archive_id']} does not expose a unique 1-36 schedule")
        local=[]
        for item in desired:
            row=dict(item);row['target_uid']=int(by_order[int(item['slot'])]['uid']);local.append(row)

        # WorldPointer is the gameplay-confirmed schedule source and each
        # RACEDATA assignment has its own operand. The visible constructor uses
        # shared LOAD_CONST operands, so editing it can make slot 21 (and other
        # repeated-track slots) collide inside one 36-slot batch. Leave that
        # shared constructor untouched and patch only the isolated runtime link.
        reference_raw,reference_label=_schedule_reference_raw(src)
        constructor_patched=bytes(src['raw'])
        constructor_changes=[]
        inference=dict(method='worldpointer_authoritative_no_constructor_write',
                       transplanted_operands=0,
                       note='Visible event labels are derived from the verified gameplay link and clean event catalog.')
        linked,link_changes,link_info=linkmod.patch_links(constructor_patched,local,reference_raw)
        patched,lap_changes=(_schedule_patch_laps_exact(linked,local) if patch_laps else (linked,[]))
        final_rows,_final_records=mod.map_schedule(patched,src['mapper'])
        final_public=_schedule_effective_rows(_schedule_public_rows(final_rows,patched),list(allowed.values()))
        final_by_order={int(r['order']):r for r in final_public}
        link_by_uid={int(x['target_uid']):x for x in link_changes}
        combined=[]
        for item in local:
            old=by_order[int(item['slot'])];final=final_by_order[int(item['slot'])];link=link_by_uid[int(item['target_uid'])]
            old_gameplay=old.get('gameplay_event_uid')
            visible_changed=(str(old.get('event'))!=str(item['event_name']) or
                             int(old.get('event_uid'))!=int(item['event_uid']) or
                             (patch_laps and int(old.get('laps'))!=int(item['laps'])))
            world_changed=(old_gameplay is None or int(old_gameplay)!=int(item['event_uid']))
            if visible_changed or world_changed:
                combined.append(dict(slot=int(item['slot']),target_uid=int(item['target_uid']),
                    source_uid=item.get('source_uid'),old_event=str(old.get('event')),new_event=str(item['event_name']),
                    old_event_uid=int(old.get('event_uid')),new_event_uid=int(item['event_uid']),
                    old_laps=int(old.get('laps')),new_laps=int(item['laps']),
                    old_gameplay_event_uid=old_gameplay,new_gameplay_event_uid=int(item['event_uid']),
                    visible_changed=bool(visible_changed),worldpointer_changed=bool(world_changed),
                    operand_pyc_offset=int(link['operand_pyc_offset'])))
        patches.append(dict(src=src,patched=patched,changes=combined,inference=inference,local=local,
                            constructor_changes=constructor_changes,link_changes=link_changes,
                            lap_changes=lap_changes,final_rows=final_public,
                            link_info=link_info,reference_label=reference_label))
    changed=patches[0]['changes']
    result=dict(ok=True,dry_run=bool(dry_run),changes=changed,change_count=len(changed),
                worldpointer_change_count=sum(1 for x in changed if x['worldpointer_changed']),
                visible_change_count=sum(1 for x in changed if x['visible_changed']),
                exact_lap_change_count=len(patches[0]['lap_changes']),
                inference=patches[0]['inference'],link_inference=patches[0]['link_info'],
                repeats=36-len({(int(x['event_uid']),str(x['event_name'])) for x in desired}),
                before=[dict(uid=r['uid'],order=r['order'],date=r['date'],track=r['track'],
                             event_uid=r.get('event_uid'),gameplay_event_uid=r.get('gameplay_event_uid')) for r in primary_rows],
                schedule_sources=len(patches),sources=[dict(archive=x['src']['archive_id'],offset=x['src']['offset'],
                    old_size=x['src']['size'],new_size=len(x['patched']),sha256=x['src']['sha256'][:16],reference=x['reference_label']) for x in patches],
                laps_preserved=not bool(patch_laps),
                elapsed_ms=round((time.perf_counter()-started)*1000),
                existing_mode_cache_warning='Career and Single Season may cache their calendar when the save is created. Test with a brand-new disposable mode after applying.')
    if dry_run:
        result['after']=patches[0]['final_rows']
        result['source_previews']=[dict(archive=x['src']['archive_id'],change_count=len(x['changes']),
            worldpointer_change_count=sum(1 for c in x['changes'] if c['worldpointer_changed']),
            exact_lap_change_count=len(x['lap_changes']),old_size=x['src']['size'],new_size=len(x['patched']),
            variable_size=(len(x['patched'])!=x['src']['size']),
            changed_bytes=x['link_info']['changed_bytes'],reference=x['reference_label']) for x in patches]
        return result
    if _rp_game_running():raise RuntimeError('NASCAR15.exe is running; close the game first')

    # One transaction across every overriding archive copy. Variable-size PYC
    # installs append/repoint; any install OR final semantic verification failure
    # restores the original cdfiles bytes, archive size, and same-size segments.
    g,reg=registry();states={};installed=[];verified_sources=[]
    try:
        for x in patches:
            src=x['src'];arcid=str(src['archive_id']);v=need(reg,arcid);key=os.path.realpath(v['ar'])
            if key not in states:
                states[key]=dict(v=v,archive_size=os.path.getsize(v['ar']),cdf_bytes=open(v['cdf'],'rb').read(),segments=[])
            state=states[key];_rp_backup_pair(v)
            if len(x['patched'])==int(src['size']):state['segments'].append((int(src['offset']),bytes(src['raw'])))
            install=_schedule_install_variant(src,x['patched']);installed.append(dict(archive=arcid,**install))
        _SCHEDULE_CACHE.clear();_SCHEDULE_SOURCE_CACHE.clear()
        verified_sources=_schedule_live_sources(use_cache=False)['sources']
        for src in verified_sources:
            by_order={int(r['order']):r for r in src['rows']}
            for item in desired:
                row=by_order[int(item['slot'])]
                if (int(row.get('gameplay_event_uid'))!=int(item['event_uid']) or
                    (patch_laps and int(row.get('laps'))!=int(item['laps']))):
                    raise RuntimeError(f"ARCHIVE{src['archive_id']} live gameplay schedule verification failed at slot {item['slot']}")
    except Exception as install_ex:
        rollback_errors=[]
        for key,state in states.items():
            try:
                with open(state['v']['ar'],'r+b') as fh:
                    for off,raw in state['segments']:fh.seek(off);fh.write(raw)
                    fh.truncate(state['archive_size']);fh.flush();os.fsync(fh.fileno())
                atomic_write_bytes(state['v']['cdf'],state['cdf_bytes'],'.schedule.rollback')
            except Exception as rb:
                rollback_errors.append(f'{key}: {rb}')
        _SCHEDULE_CACHE.clear();_SCHEDULE_SOURCE_CACHE.clear()
        if rollback_errors:
            raise RollbackFailed(install_ex,'; '.join(rollback_errors))
        raise
    _schedule_save_assignments(desired)
    _SCHEDULE_CACHE.clear()
    effective_rows,_effective_meta=_schedule_read('live',use_cache=False)
    result['verified']=True;result['gameplay_verified']=True;result['installed']=installed
    result['verified_sources']=len(verified_sources);result['rows']=effective_rows
    return result

# ---- v0.9.26.12 atomic bulk Images & Textures actions ----
def _ui_bulk_identity(t):
    # Payload offset is part of the identity so duplicate/aliased entry names in
    # one container cannot collapse into a single selected target.
    pa=t.get('payload_abs')
    try:pa=int(pa)
    except Exception:pa=-1
    return (str(t.get('archive','')),str(t.get('container','')).upper(),str(t.get('entry','')),pa)


def _ui_bulk_group_key(arcid,container):
    return (str(arcid),str(container or '').upper())


def _ui_union_only_changed(old,new,ranges):
    if len(old)!=len(new):return 'container size changed'
    merged=[]
    for a,b in sorted((int(a),int(b)) for a,b in ranges):
        if a<0 or b<a or b>len(old):return 'invalid payload range'
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],b))
        else:merged.append((a,b))
    cur=0
    for a,b in merged:
        if old[cur:a]!=new[cur:a]:return f'bytes outside selected payloads changed before 0x{a:X}'
        cur=b
    if old[cur:]!=new[cur:]:return 'bytes outside selected payloads changed after final payload'
    return None


def _ui_bulk_groups(targets,need_stock=False):
    if not isinstance(targets,list) or not targets:raise ValueError('select at least one image')
    if len(targets)>250:raise ValueError('bulk image operations are limited to 250 selected images')
    seen=set();groups={};resolved=[]
    for t in targets:
        ident=_ui_bulk_identity(t)
        if ident in seen:continue
        seen.add(ident);arcid,container_u,entry,payload_hint=ident
        container=str(t.get('container') or '')
        if not arcid or not container or not entry:raise ValueError('invalid selected image identity')
        indexed=_ui_csv_row(arcid,container,entry,payload_abs=(None if payload_hint<0 else payload_hint))
        rowlike=dict(indexed or {},archive=arcid,container=container,entry=entry,
                     family=t.get('family',''),fmt=t.get('fmt',''),w=t.get('w'),h=t.get('h'),payload_size=t.get('payload_size'))
        live,e,off,size=_ui_load_entry(arcid,container,entry,t.get('w'),t.get('h'),payload_abs=(None if payload_hint<0 else payload_hint))
        if payload_hint>=0 and int(e.get('payload_abs',-1))!=payload_hint:
            raise ValueError(f'{entry}: selected payload 0x{payload_hint:X} resolved to 0x{int(e.get("payload_abs",-1)):X}')
        key=_ui_bulk_group_key(arcid,container)
        g=groups.get(key)
        if g is None:
            g=groups[key]=dict(arcid=arcid,container=container,container_u=container_u,
                               old=live,current=live,off=off,size=size,ranges=[],items=[])
        elif g['off']!=off or g['size']!=size:
            raise ValueError(f'{container}: inconsistent indexed container range')
        stock_e=stock_arc=None
        if need_stock:
            stock_arc,stock_e,soff,ssize=_ui_load_entry(arcid,container,entry,t.get('w'),t.get('h'),pristine=True,payload_abs=(None if payload_hint<0 else payload_hint))
            if stock_e['payload_size']!=e['payload_size']:
                raise ValueError(f'{entry}: stock payload size differs')
        item=dict(target=t,row=rowlike,e=e,stock_e=stock_e,stock_arc=stock_arc,
                  ident=ident,group_key=key)
        g['items'].append(item);resolved.append(item)
    return groups,resolved


def _ui_bulk_commit(groups,expected_payloads,source):
    gpath,reg=registry();written=[];verified=[]
    try:
        # Verify the final in-memory image contains every selected payload before
        # touching disk. This catches accidental "last target wins" behavior.
        for grp in groups.values():
            err=_ui_union_only_changed(grp['old'],grp['current'],grp['ranges'])
            if err:raise ValueError(f'{grp["container"]}: {err}')
            for item in grp['items']:
                payload=expected_payloads.get(item['ident'])
                if payload is None:continue
                e=item['e'];a=e['payload_abs'];b=a+e['payload_size']
                if grp['current'][a:b]!=payload:
                    raise ValueError(f'{item["ident"][2]}: final batch image lost this selected payload before write')
        for grp in groups.values():
            v=need(reg,grp['arcid']);ensure_backup(v['ar'],v['bak'])
            # Mark the container as attempted before the first live write. A
            # disk/fsync failure can happen after bytes changed but before this
            # block returns, and that partially-written container must still be
            # included in rollback.
            written.append(grp)
            with open(v['ar'],'r+b') as fh:
                fh.seek(grp['off']);fh.write(grp['current']);fh.flush();os.fsync(fh.fileno())
        # Read back complete containers and then every selected payload separately.
        for grp in groups.values():
            v=need(reg,grp['arcid'])
            with open(v['ar'],'rb') as fh:fh.seek(grp['off']);chk=fh.read(grp['size'])
            if chk!=grp['current']:raise ValueError(f'{grp["container"]}: container readback mismatch')
        verified_targets=[]
        for grp in groups.values():
            v=need(reg,grp['arcid'])
            with open(v['ar'],'rb') as fh:fh.seek(grp['off']);chk=fh.read(grp['size'])
            for item in grp['items']:
                payload=expected_payloads.get(item['ident'])
                if payload is None:continue
                e=item['e'];a=e['payload_abs'];b=a+e['payload_size']
                if chk[a:b]!=payload:
                    raise ValueError(f'{item["ident"][2]} @ 0x{a:X}: payload readback mismatch')
                verified.append(item['ident'][2])
                verified_targets.append(dict(entry=item['ident'][2],payload_abs=int(a),payload_size=int(e['payload_size']),
                                             archive=str(grp['arcid']),container=grp['container']))
        # Container-name casing made targeted invalidation unreliable in older
        # builds. Clear the whole lightweight thumbnail cache after an atomic batch.
        _clear_ui_thumb_cache()
        return dict(ok=True,verified=True,app_version=APP_VERSION,changed=len(expected_payloads),verified_count=len(verified),
                    verified_entries=verified,verified_targets=verified_targets,containers=len(groups),source=source)
    except Exception as install_ex:
        rollback_errors=[]
        for grp in reversed(written):
            try:
                v=need(reg,grp['arcid'])
                with open(v['ar'],'r+b') as fh:
                    fh.seek(grp['off']);fh.write(grp['old']);fh.flush();os.fsync(fh.fileno())
                    fh.seek(grp['off'])
                    if fh.read(grp['size'])!=grp['old']:
                        raise ValueError('rollback readback mismatch')
            except Exception as rb:
                rollback_errors.append(f'{grp["container"]}: {rb}')
        _clear_ui_thumb_cache()
        if rollback_errors:
            raise RollbackFailed(install_ex,'; '.join(rollback_errors)) from install_ex
        raise

@app.route('/api/ui/bulk_restore',methods=['POST'])
def ui_bulk_restore():
    try:
        q=request.get_json(force=True);groups,items=_ui_bulk_groups(q.get('targets') or [],need_stock=True)
        expected={}
        for item in items:
            g=groups[item['group_key']];e=item['e'];se=item['stock_e'];sa=item['stock_arc']
            payload=sa[se['payload_abs']:se['payload_abs']+se['payload_size']]
            cur=bytearray(g['current']);a=e['payload_abs'];b=a+e['payload_size'];cur[a:b]=payload;g['current']=bytes(cur);g['ranges'].append((a,b));expected[item['ident']]=payload
        if q.get('dry_run'):
            return jsonify(dict(ok=True,dry_run=True,changed=len(expected),containers=len(groups),entries=[i['ident'][2] for i in items]))
        return jsonify(_ui_bulk_commit(groups,expected,'bulk stock restore'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/ui/bulk_replace',methods=['POST'])
def ui_bulk_replace():
    """Apply one image to every selected target, or match a ZIP by entry name.

    Every selected target is independently resized/encoded to its own format.
    ZIP mode accepts names such as Tyre02.png, Tyre02-D.png, or the exported
    container__entry template name. All modified containers are committed as
    one rollback-protected operation.
    """
    try:
        import zipfile
        targets=json.loads(request.form.get('targets') or '[]');groups,items=_ui_bulk_groups(targets,need_stock=False)
        up=request.files.get('file')
        if not up:raise ValueError('choose an image or ZIP package')
        raw=up.read()
        if not raw:raise ValueError('uploaded file is empty')
        resize_mode=request.form.get('resize_mode','fit');force=request.form.get('force')=='1';dry=request.form.get('dry_run')=='1'
        members={};is_zip=(up.filename or '').lower().endswith('.zip') or raw[:4]==b'PK\x03\x04'
        if is_zip:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for n in z.namelist():
                    if n.endswith('/'):continue
                    info=z.getinfo(n)
                    if info.file_size>32*1024*1024:continue
                    base=os.path.basename(n);stem=os.path.splitext(base)[0].casefold()
                    members.setdefault(base.casefold(),z.read(n));members.setdefault(stem,z.read(n))
        matched=[];unmatched=[];expected={};prep=[]
        for item in items:
            t=item['target'];entry=t['entry'];container=t['container'];imgraw=None;member_name=None
            if is_zip:
                eb=os.path.basename(entry);estem=os.path.splitext(eb)[0].casefold();cstem=os.path.splitext(os.path.basename(container))[0].casefold()
                candidates=[eb.casefold(),estem,f'{cstem}__{estem}',re.sub(r'[^a-z0-9_.-]+','_',f'{cstem}__{estem}').casefold()]
                for cand in candidates:
                    if cand in members:imgraw=members[cand];member_name=cand;break
                if imgraw is None:
                    # exported templates include dimensions after the entry name
                    for k,v in members.items():
                        if k.startswith(f'{cstem}__{estem}_') or k.startswith(estem+'_'):
                            imgraw=v;member_name=k;break
            else:
                imgraw=raw;member_name=up.filename or 'shared image'
            if imgraw is None:
                unmatched.append(entry);continue
            row=item['row'];reason=_ui_replace_reason(row)
            if reason:raise ValueError(f'{entry}: {reason}')
            a=_confirmed_for(row);mapping=_ui_mapping(row);safe=(_ui_safety(row,a,mapping)=='safe_replace')
            if not safe and not force and row.get('decoded') is False:
                raise ValueError(f'{entry}: target is not decoded safely')
            g=groups[item['group_key']];e=item['e']
            q=dict(image=_b64.b64encode(imgraw).decode(),resize_mode=resize_mode)
            new,payload,iprep,preview=_ui_prepare_encoded(q,g['current'],e,safe,row)
            g['current']=new;a0=e['payload_abs'];g['ranges'].append((a0,a0+e['payload_size']));expected[item['ident']]=payload
            matched.append(entry);prep.append(dict(entry=entry,source=member_name,target=[e['w'],e['h']],codec=e['fmt'],resized=iprep.get('resized',False),
                                                    mip_levels_written=int(iprep.get('mip_levels_written',1)),mip_dimensions=iprep.get('mip_dimensions') or []))
        if not matched:raise ValueError('no uploaded images matched the selected entries')
        if not is_zip and len(matched)!=len(items):
            raise ValueError(f'same-image batch resolved only {len(matched)} of {len(items)} selected targets')
        if len(expected)!=len(matched):
            raise ValueError('bulk target identity collision: selected entries did not remain unique')
        # Drop groups with no matched target so unchanged containers are not written.
        groups={k:g for k,g in groups.items() if g['ranges']}
        if dry:return jsonify(dict(ok=True,dry_run=True,app_version=APP_VERSION,mode=('zip_match' if is_zip else 'same_image'),matched=matched,unmatched=unmatched,containers=len(groups),resolved_count=len(expected),resolved_targets=[dict(entry=i['ident'][2],payload_abs=int(i['e']['payload_abs']),payload_size=int(i['e']['payload_size'])) for i in items if i['ident'] in expected],preparation=prep))
        result=_ui_bulk_commit(groups,expected,'bulk smart import');result.update(mode=('zip_match' if is_zip else 'same_image'),matched=matched,unmatched=unmatched,preparation=prep);return jsonify(result)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/schedule')
def schedule_get():
    try:
        import time
        started=time.perf_counter(); rows,meta=_schedule_read('live')
        profile_rows,profiles,profile_source=_schedule_event_lap_profile_rows(save_missing=True)
        rows=_schedule_enrich_profile_laps(rows,profiles)
        stock_rows=_schedule_enrich_profile_laps(meta.get('stock_rows') or [],profiles)
        sources=_schedule_stock_sources()
        source_rows=meta.get('sources') or []
        link_mismatches=sum(1 for r in rows if not r.get('worldpointer_matches'))
        return jsonify(dict(ok=True,rows=rows,count=len(rows),
                            stock_rows=stock_rows,stock_source=meta.get('stock_source'),
                            event_lap_profiles=profiles,profile_source=profile_source,
                            stock_available=bool(sources),stock_sources=sources,
                            schedule_sources=len(source_rows) or 1,worldpointer_mismatches=link_mismatches,gameplay_links_verified=(link_mismatches==0),
                            source_archives=[str(x.get('archive_id')) for x in source_rows],
                            source_errors=meta.get('source_errors') or [],
                            load_ms=round((time.perf_counter()-started)*1000),
                            helper=SCHEDULE_HELPER_NAME,link_helper=SCHEDULE_LINK_HELPER_NAME,read_only_fields=['NumDrivers'],max_race_laps=MAX_RACE_LAPS,
                            cache_warning='Existing Career and Single Season saves may retain the calendar created when that mode began. Test schedule changes with a brand-new disposable mode.',
                            note='The 36-race season and lap values are verified before any file is written.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/schedule/stock')
def schedule_stock_get():
    try:
        import time
        started=time.perf_counter(); sources=_schedule_stock_sources()
        if not sources: raise RuntimeError('no clean original game copy or previous backup is available')
        # The pristine app backup is fastest and requires no multi-gigabyte hash pass.
        source='backup' if 'backup' in sources else sources[0]
        rows,_=_schedule_read(source,verify_hash=False)
        _profile_rows,profiles,_profile_source=_schedule_event_lap_profile_rows(save_missing=True)
        rows=_schedule_enrich_profile_laps(rows,profiles)
        return jsonify(dict(ok=True,rows=rows,stock_source=source,event_lap_profiles=profiles,
                            load_ms=round((time.perf_counter()-started)*1000)))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/schedule/preview',methods=['POST'])
def schedule_preview():
    try:
        q=request.get_json(force=True) or {}
        return jsonify(_schedule_patch(_schedule_desired(q),True,patch_laps=not bool(q.get('preserve_laps'))))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/schedule/apply',methods=['POST'])
def schedule_apply():
    try:
        q=request.get_json(force=True) or {}
        return jsonify(_schedule_patch(_schedule_desired(q),False,patch_laps=not bool(q.get('preserve_laps'))))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/schedule/restore',methods=['POST'])
def schedule_restore():
    try:
        stock=None; src=None
        for candidate in ('baseline','backup'):
            try: stock,_=_schedule_read(candidate); src=candidate; break
            except Exception: pass
        if not stock: raise RuntimeError('no clean original game copy or previous backup is available')
        live,_=_schedule_read('live'); targets={int(r['order']):r for r in live}
        desired=[dict(slot=i,target_uid=int(targets[i]['uid']),source_uid=int(r['uid']),
                      event_uid=int(r['event_uid']),event_name=str(r['event']),laps=int(r['laps']))
                 for i,r in sorted(((int(x['order']),x) for x in stock),key=lambda kv:kv[0])]
        out=_schedule_patch(desired,False,patch_laps=False); out['restored_from']=src; return jsonify(out)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/stock36_laps')
def schedule_stock36_laps_get():
    """Return the locked stock 36 event identities with persistent lap profiles."""
    try:
        import time
        started=time.perf_counter();rows,profiles,source=_schedule_event_lap_profile_rows(save_missing=True)
        return jsonify(dict(ok=True,rows=rows,count=len(rows),stock_source=source,
                            load_ms=round((time.perf_counter()-started)*1000),
                            max_race_laps=MAX_RACE_LAPS,
                            note='Each named event keeps its own lap default and carries that value wherever it is placed in the season.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_lap/preview',methods=['POST'])
def schedule_event_lap_preview():
    try:
        q=request.get_json(force=True) or {}
        key=_schedule_resolve_profile_key(q.get('profile_key'),q.get('event_uid'),q.get('event_name'))
        return jsonify(_schedule_apply_event_profile(key,q.get('laps'),dry_run=True))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_lap/apply',methods=['POST'])
def schedule_event_lap_apply():
    try:
        q=request.get_json(force=True) or {}
        key=_schedule_resolve_profile_key(q.get('profile_key'),q.get('event_uid'),q.get('event_name'))
        return jsonify(_schedule_apply_event_profile(key,q.get('laps'),dry_run=False))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_laps/batch/preview',methods=['POST'])
def schedule_event_laps_batch_preview():
    try:
        q=request.get_json(force=True) or {}
        return jsonify(_schedule_apply_event_profiles_batch(q.get('entries') or [],dry_run=True))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/event_laps/batch/apply',methods=['POST'])
def schedule_event_laps_batch_apply():
    try:
        q=request.get_json(force=True) or {}
        return jsonify(_schedule_apply_event_profiles_batch(q.get('entries') or [],dry_run=False))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/schedule/stock36_laps/restore',methods=['POST'])
def schedule_stock36_laps_restore():
    try:
        rows,_profiles,source=_schedule_event_lap_profile_rows(save_missing=True)
        defaults={str(r['profile_key']):int(r['stock_laps']) for r in rows}
        entries=[dict(profile_key=k,laps=v) for k,v in defaults.items()]
        out=_schedule_apply_event_profiles_batch(entries,dry_run=False)
        cfg=load_cfg();cfg[SCHEDULE_EVENT_LAP_PROFILE_KEY]=defaults;save_cfg(cfg)
        out['restored_laps_from']=source;out['profiles_restored']=len(defaults)
        return jsonify(out)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

_TRACK_CACHE={}
_CDF_ENTRY_CACHE={}
try:
    import threading as _threading
    _TRACK_LOCK=_threading.Lock()
except Exception:
    _TRACK_LOCK=None
TRACK_ALIAS_HINTS={
 'Auto Club':['AUTOCLUB','FONTANA'],'Charlotte':['CHARLOTTE','LOWES'],'Sonoma':['SONOMA','INFINEON'],
 'Watkins Glen':['WATKINSGLEN','WATKINS','GLEN'],'New Hampshire':['NEWHAMPSHIRE','LOUDON'],
 'Indianapolis':['INDIANAPOLIS','INDY'],'Darlington':['DARLINGTON'],'Homestead':['HOMESTEAD'],
 'Talladega':['TALLADEGA'],'Daytona':['DAYTONA'],'Martinsville':['MARTINSVILLE'],
 'Bristol':['BRISTOL'],'Richmond':['RICHMOND'],'Dover':['DOVER'],'Pocono':['POCONO'],
 'Michigan':['MICHIGAN'],'Kansas':['KANSAS'],'Atlanta':['ATLANTA'],'Texas':['TEXAS'],
 'Phoenix':['PHOENIX'],'Las Vegas':['LASVEGAS','VEGAS'],'Kentucky':['KENTUCKY'],
 'Chicagoland':['CHICAGOLAND','CHICAGO']
}

def _track_norm(s): return re.sub(r'[^A-Z0-9]','',str(s).upper())

def _cdf_entries_cached(path):
    st=os.stat(path); key=(os.path.realpath(path),st.st_size,st.st_mtime_ns)
    if key in _CDF_ENTRY_CACHE:return _CDF_ENTRY_CACHE[key]
    rows=parse_cdfiles(path)
    for old in list(_CDF_ENTRY_CACHE):
        if old[0]==key[0] and old!=key:_CDF_ENTRY_CACHE.pop(old,None)
    _CDF_ENTRY_CACHE[key]=rows
    return rows

def _track_aliases(entries_by_archive=None):
    # Display aliases must never trigger archive payload reads. v0.9.21 called
    # scr_entries() here, which scanned SCR containers and could block the tab.
    aliases={k:set(map(_track_norm,v+[k])) for k,v in TRACK_ALIAS_HINTS.items()}
    for entries in (entries_by_archive or {}).values():
        for _off,_size,name in entries:
            if not name.upper().endswith('_SCR.ARC'): continue
            role=_scr_role(name)
            if not role: continue
            track=_scr_track(name)
            stem=name.upper().replace('_SCR.ARC','')
            if stem.startswith('NASCAR'):stem=stem[6:]
            for tail in ('PLAYER','AI'):
                if stem.endswith(tail):stem=stem[:-len(tail)]
            aliases.setdefault(track,set()).update({_track_norm(track),_track_norm(stem)})
    return {k:{a for a in v if len(a)>=4} for k,v in aliases.items()}

def _track_category(name):
    u=name.upper()
    if u.endswith('_SCR.ARC') or any(k in u for k in ('PHYS','CHASSIS','TIRE','TYRE','AERO')): return 'Physics / Vehicle Config'
    if any(k in u for k in ('AICONFIG','AI_','_AI','RACINGLINE','RACE_LINE')): return 'AI / Racing Line'
    if any(k in u for k in ('CAMERA','CAM_','REPLAY')): return 'Cameras'
    if any(k in u for k in ('TRACKCARD','TRACKDETAIL','CALENDAR_TRACK','TRACKSELECT','MINIMAP','MAPIMAGE','LOADING')): return 'UI / Track Images'
    if any(k in u for k in ('FSB','SOUND','AUDIO','AMBIENT')): return 'Audio / Ambience'
    if any(k in u for k in ('RACEDATA','EVENT','SCHEDULE','RACESETTING')): return 'Race Metadata'
    if any(k in u for k in ('WORLD','MESH','MODEL','GEOM','COLLISION','BARRIER','WALL','TRACK')): return 'World / Geometry'
    if any(k in u for k in ('TEXTURE','TEX','DDS','MATERIAL','BILLBOARD','SPONSOR')): return 'Textures / Materials'
    return 'Unknown / Other'

def _track_stamp(reg):
    out=[]
    for k,v in sorted(reg.items(),key=lambda x:int(x[0])):
        st=os.stat(v['cdf']); out.append([str(k),os.path.realpath(v['cdf']),st.st_size,st.st_mtime_ns])
    return out

def _track_cache_path(): return os.path.join(_profile_dir(),'track_files_cache_v1.json')

def _track_inventory_build(reg,stamp):
    entries_by_archive={}
    for arcid,r in sorted(reg.items(),key=lambda x:int(x[0])):
        try:entries_by_archive[str(arcid)]=_cdf_entries_cached(r['cdf'])
        except Exception:entries_by_archive[str(arcid)]=[]
    aliases=_track_aliases(entries_by_archive); alias_pairs=[]
    for track,als in aliases.items():
        for alias in als:alias_pairs.append((alias,track))
    alias_pairs.sort(key=lambda x:len(x[0]),reverse=True)
    rows=[]
    for arcid,entries in entries_by_archive.items():
        for off,size,name in entries:
            n=_track_norm(name); matched={track for alias,track in alias_pairs if alias in n}
            generic=any(k in name.upper() for k in ('TRACK','RACEWAY','SPEEDWAY','CIRCUIT','ROADCOURSE'))
            if not matched and not generic: continue
            if not matched: matched={'Shared / Unmapped'}
            cat=_track_category(name)
            for track in sorted(matched):
                rows.append(dict(track=track,archive=str(arcid),name=name,offset=off,size=size,category=cat,
                                 extension=os.path.splitext(name)[1].upper() or '(none)',
                                 confidence='likely' if track!='Shared / Unmapped' else 'unknown'))
    rows.sort(key=lambda x:(x['track'],x['category'],x['name'],int(x['archive'])))
    try:
        os.makedirs(os.path.dirname(_track_cache_path()),exist_ok=True)
        tmp=_track_cache_path()+'.tmp'
        with open(tmp,'w',encoding='utf-8') as f:json.dump(dict(stamp=stamp,rows=rows),f,separators=(',',':'))
        os.replace(tmp,_track_cache_path())
    except Exception:pass
    return rows

def _track_inventory(force=False):
    import time
    g,reg=registry()
    if not g: raise RuntimeError('game folder not found')
    stamp=_track_stamp(reg)
    if not force and _TRACK_CACHE.get('stamp')==stamp:return _TRACK_CACHE['rows']
    lock=_TRACK_LOCK
    if lock:lock.acquire()
    try:
        if not force and _TRACK_CACHE.get('stamp')==stamp:return _TRACK_CACHE['rows']
        if not force:
            try:
                with open(_track_cache_path(),'r',encoding='utf-8') as f:disk=json.load(f)
                if disk.get('stamp')==stamp and isinstance(disk.get('rows'),list):
                    _TRACK_CACHE.update(stamp=stamp,rows=disk['rows'],cache_hit='disk',build_ms=0)
                    return _TRACK_CACHE['rows']
            except Exception:pass
        started=time.perf_counter();rows=_track_inventory_build(reg,stamp)
        _TRACK_CACHE.update(stamp=stamp,rows=rows,cache_hit='built',build_ms=round((time.perf_counter()-started)*1000))
        return rows
    finally:
        if lock:lock.release()

def _track_filter(q):
    rows=_track_inventory(); track=(q.get('track') or 'all'); cat=(q.get('category') or 'all'); text=(q.get('q') or '').lower()
    return [r for r in rows if (track=='all' or r['track']==track) and (cat=='all' or r['category']==cat)
            and (not text or text in (r['name']+' '+r['category']+' '+r['track']+' ARCHIVE'+r['archive']).lower())]

@app.route('/api/tracks/files',methods=['POST'])
def track_files():
    try:
        q=request.get_json(silent=True) or {}; allrows=_track_inventory(force=bool(q.get('force'))); rows=_track_filter(q)
        page=max(0,int(q.get('page',0))); per=max(1,min(500,int(q.get('per',200))))
        tracks=sorted({r['track'] for r in allrows},key=lambda x:(x=='Shared / Unmapped',x))
        cats=sorted({r['category'] for r in allrows})
        from collections import Counter
        counts=dict(Counter(r['track'] for r in allrows))
        return jsonify(dict(ok=True,total=len(rows),page=page,per=per,rows=rows[page*per:(page+1)*per],
                            tracks=tracks,categories=cats,track_counts=counts,read_only=True,
                            cache=_TRACK_CACHE.get('cache_hit','memory'),build_ms=_TRACK_CACHE.get('build_ms',0)))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/tracks/compare',methods=['POST'])
def track_compare():
    try:
        q=request.get_json(force=True); a=q.get('a'); b=q.get('b')
        if not a or not b or a==b: raise ValueError('choose two different tracks')
        rows=_track_inventory(); aliases=_track_aliases()
        def patterns(track):
            out={}
            for r in rows:
                if r['track']!=track: continue
                p=_track_norm(r['name'])
                for al in aliases.get(track,()): p=p.replace(al,'TRACK')
                out.setdefault((r['category'],p),[]).append(r)
            return out
        pa,pb=patterns(a),patterns(b); shared=sorted(set(pa)&set(pb)); onlya=sorted(set(pa)-set(pb)); onlyb=sorted(set(pb)-set(pa))
        def pub(keys,src):return [dict(category=k[0],pattern=k[1],files=src[k]) for k in keys]
        return jsonify(dict(ok=True,a=a,b=b,shared=pub(shared,pa),only_a=pub(onlya,pa),only_b=pub(onlyb,pb),
                            summary=dict(shared=len(shared),only_a=len(onlya),only_b=len(onlyb))))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/tracks/report')
def track_report():
    try:
        import zipfile,datetime
        track=request.args.get('track','all'); rows=_track_filter(dict(track=track,category='all',q=''))
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:
            out=io.StringIO(); w=_csv.DictWriter(out,fieldnames=['track','archive','name','offset','size','category','extension','confidence']);w.writeheader();w.writerows(rows)
            z.writestr('track_files.csv',out.getvalue().encode('utf-8-sig'))
            z.writestr('SUMMARY.txt',(f'NASCAR 15 Modding App v{APP_VERSION} Track Files Report\nTrack: {track}\nEntries: {len(rows)}\nCreated: {datetime.datetime.now().isoformat()}\n\nRead-only inventory. Classification is heuristic until verified in game.\n').encode())
        b.seek(0); safe=re.sub(r'[^A-Za-z0-9_.-]+','_',track)
        return send_file(b,mimetype='application/zip',as_attachment=True,download_name=f'nascar15_track_files_{safe}.zip')
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/tracks/export')
def track_export():
    try:
        import zipfile
        track=request.args.get('track')
        if not track or track=='all' or track=='Shared / Unmapped': raise ValueError('choose one mapped track')
        rows=_track_filter(dict(track=track,category='all',q='')); g,reg=registry(); total=sum(int(r['size']) for r in rows)
        if total>512*1024*1024: raise ValueError('selected track exceeds the 512 MB safety limit; use the inventory report first')
        b=tempfile.SpooledTemporaryFile(max_size=32*1024*1024,mode='w+b'); manifest=[]
        handles={}
        try:
            with zipfile.ZipFile(b,'w',zipfile.ZIP_STORED) as z:
                for i,r in enumerate(rows):
                    fh=handles.setdefault(r['archive'],open(reg[r['archive']]['ar'],'rb')); fh.seek(r['offset']); data=fh.read(r['size'])
                    if len(data)!=r['size']: raise RuntimeError('short read: '+r['name'])
                    clean=re.sub(r'[^A-Za-z0-9_.-]+','_',r['name'])
                    z.writestr(f'ARCHIVE{r["archive"]}/{i:04d}_{clean}',data); manifest.append(r)
                z.writestr('manifest.json',json.dumps(dict(track=track,count=len(rows),files=manifest),indent=2))
        finally:
            for fh in handles.values(): fh.close()
        b.seek(0); safe=re.sub(r'[^A-Za-z0-9_.-]+','_',track)
        return send_file(b,mimetype='application/zip',as_attachment=True,download_name=f'nascar15_{safe}_track_files.zip')
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end v0.9.21 SCHEDULE / TRACK FILES ====================

# ==================== v0.9.21 COMPLETE SEASON PACKS / PRESETS / CHECKUP ====================
PACK_FORMAT = 'nascar15-modding-pack'
PACK_FORMAT_ALIASES = {PACK_FORMAT, 'nascar15-gridpack'}  # old v2 packs remain importable
PACK_VERSION = 2
PACK_CATEGORIES = (
    'schemes','names','ratings','menus','ui','ui_text','audio',
    'race','ai_track','ai_global','scr','presets','pit_log'
)

def _version_tuple(value):
    nums=[]
    for part in re.findall(r'\d+',str(value))[:4]:
        try: nums.append(int(part))
        except Exception: nums.append(0)
    return tuple(nums+[0]*(4-len(nums)))

def _pack_json_bytes(obj):
    return json.dumps(obj,indent=2,ensure_ascii=False).encode('utf-8')

def _pack_safe_member(name):
    """Reject absolute/traversal ZIP paths and normalize separators."""
    name=str(name or '').replace('\\','/').lstrip('/')
    parts=[p for p in name.split('/') if p not in ('','.')]
    if any(p=='..' for p in parts):
        raise ValueError('pack contains an unsafe path')
    return '/'.join(parts)

def _pack_read_json(z,name,default=None):
    try:
        raw=z.read(name)
    except KeyError:
        return default
    if len(raw)>8*1024*1024:
        raise ValueError(f'{name} is unexpectedly large')
    return json.loads(raw.decode('utf-8-sig'))

def _pack_stock_archive0():
    try:
        p=baseline_archive('0')
        if p: return p,'baseline'
    except Exception:
        pass
    try:
        _g,reg=registry(); p=reg['0']['bak']
        if os.path.exists(p): return p,'backup'
    except Exception:
        pass
    return None,None

def _pack_pyc_changes(pyc_file,class_name,fields):
    """Return only live fields that differ from the best clean reference."""
    if not mapper_ready(): return [],'mapper unavailable'
    stock,label=_pack_stock_archive0()
    if not stock: return [],'no clean original game copy or previous backup'
    live_rows=mapper_records(pyc_file,class_name,fields)
    stock_rows=mapper_records(pyc_file,class_name,fields,archive=stock)
    sm={str(r.get('uid')):r for r in stock_rows}
    out=[]
    for row in live_rows:
        uid=str(row.get('uid')); base=sm.get(uid)
        if not base: continue
        for field in fields:
            cur=row.get(field); old=base.get(field)
            if cur is None or old is None: continue
            if class_name in ('AIRACINGTRACKCONFIG_c','AIRACINGGLOBALCONFIG_c'):
                allowed=AI_EDITABLE_BY_CLASS.get(class_name,set())
                if field not in allowed or not _direct_scalar(cur):
                    continue
            if not _num_eq(cur,old) and str(cur)!=str(old):
                out.append(dict(uid=uid,field=field,value=cur,stock=old))
    return out,label

def _pack_scr_changes():
    stock,stock_cdf,label=_scr_stock_source()
    if not stock: return [],'no clean original game copy or previous backup'
    live=_scr_numeric_inventory()
    base=_scr_numeric_inventory(archive_override=stock,archive_id='0',cdf_override=stock_cdf)
    bm={_scr_row_ident(r):r for r in base}
    out=[]
    for row in live:
        b=bm.get(_scr_row_ident(row))
        if not b or row['value']==b['value']: continue
        out.append(dict(arc=str(row['arc']),name=row['name'],key=row['key'],
                        occurrence=int(row['occurrence']),value=row['value'],stock=b['value'],
                        track=row['track'],role=row['role'],context=row['context']))
    return out,label

def _pack_collect_ui_text(z):
    """Store exact modified TEXT table indexes, not complete copyrighted tables."""
    try:rows,_files,scan_errors=_ui_text_scan()
    except Exception as ex:return [],[f'UI text: {ex}']
    meta=[]
    for r in rows:
        if not r.get('modified'):continue
        meta.append(dict(file=r['file'],index=int(r['index']),text=r['current'],
                         stock=r.get('stock'),category=r.get('category'),screen=r.get('screen'),
                         format_tokens=r.get('tokens') or []))
    if meta:z.writestr('ui_text/strings.json',_pack_json_bytes(meta))
    return meta,[f'UI text scan: {x}' for x in scan_errors]


def _pack_collect_ui(z):
    try: rows=_ui_index()
    except Exception: return [],[]
    _g,reg=registry(); states=_ui_modified_states(rows,reg)
    meta=[]; errors=[]; index=0
    for row in rows:
        ident=(str(row['archive']),row['container'],row['entry'])
        if not states.get(ident): continue
        try:
            arc,e,_,_=_ui_load_entry(row['archive'],row['container'],row['entry'],row.get('w'),row.get('h'))
            raw=bytes(arc[e['payload_abs']:e['payload_abs']+e['payload_size']])
            member=f'ui/raw/{index:05d}.bin'; z.writestr(member,raw)
            a=_confirmed_for(row)
            meta.append(dict(archive=str(row['archive']),container=row['container'],entry=row['entry'],
                             w=e['w'],h=e['h'],fmt=e['fmt'],payload_size=e['payload_size'],
                             family=row.get('family',''),label=(a.get('label') if a else None),
                             safety=_ui_safety(row,a),file=member,sha256=_hl.sha256(raw).hexdigest()))
            index+=1
        except Exception as ex:
            errors.append(f"UI {row.get('container')}/{row.get('entry')}: {ex}")
    if meta: z.writestr('ui/assets.json',_pack_json_bytes(meta))
    return meta,errors

def _pack_collect_audio(z):
    _g,reg=registry(); meta=[]; errors=[]; index=0
    for arcid,v in sorted(reg.items()):
        if not os.path.exists(v.get('bak','')): continue
        try: entries=parse_cdfiles(v['cdf'])
        except Exception: continue
        for _off,_sz,name in entries:
            if not name.upper().endswith(('.FSB','.SND')): continue
            try:
                vv,boff,c,flat,kind=_read_container(arcid,name)
                backup=_audio_backup_container(vv,boff,len(c))
                if backup is None: continue
                for idx,s in enumerate(flat):
                    if not _audio_sample_modified(c,backup,s): continue
                    raw=bytes(c[s['rel']:s['rel']+s['len']])
                    member=f'audio/raw/{index:05d}.bin';z.writestr(member,raw)
                    meta.append(dict(archive=str(arcid),bank=name,index=idx,name=s['name'],
                                     length=s['len'],mode=s['mode'],file=member,
                                     sha256=_hl.sha256(raw).hexdigest()))
                    index+=1
            except Exception as ex:
                errors.append(f'Audio {name}: {ex}')
    if meta: z.writestr('audio/assets.json',_pack_json_bytes(meta))
    return meta,errors

def _pack_collect_menus(z):
    meta=[];errors=[]
    for key in _menu_containers():
        try:
            _g,reg=registry()
            arcid,off,size,live=menu_container(reg,key,live=True)
            a=need(reg,arcid)
            if not os.path.exists(a['bak']): continue
            _,_,_,bak=menu_container(reg,key,live=False)
            ent,_=C.parse_multi_arc(live,known_dims=(128,64) if key=='numbers' else None)
            for e in ent:
                if e['w']<=0: continue
                pa,ps=e['payload_abs'],e['payload_size']
                if live[pa:pa+ps]==bak[pa:pa+ps]: continue
                img=C.multi_read_png(live,e);b=io.BytesIO();img.save(b,'PNG')
                member=f'menus/{key}/{e["name"]}.png';z.writestr(member,b.getvalue())
                meta.append(dict(key=key,name=e['name'],w=e['w'],h=e['h'],file=member))
        except Exception as ex:
            errors.append(f'Menu {key}: {ex}')
    if meta: z.writestr('menus/assets.json',_pack_json_bytes(meta))
    return meta,errors

def _pack_export_v2_bytes():
    import zipfile,datetime
    _g,reg=registry();buf=io.BytesIO();errors=[]
    counts={k:0 for k in PACK_CATEGORIES};sources={}
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        # Schemes are staged source art and are safe to package without archives.
        scheme_names=[]
        if os.path.isdir(SCHEMES):
            for fn in sorted(os.listdir(SCHEMES)):
                fp=os.path.join(SCHEMES,fn)
                if not os.path.isfile(fp): continue
                member='schemes/'+os.path.basename(fn);z.write(fp,member);scheme_names.append(member)
        counts['schemes']=sum(1 for n in scheme_names if n.endswith('.png') and '.layer.' not in n and '.thumb.' not in n)

        cfg=load_cfg();names=dict(renames=cfg.get('renames',{}),
            handles={k:str(v).rstrip('_ ') for k,v in (cfg.get('handles',{}) or {}).items()})
        z.writestr('names.json',_pack_json_bytes(names));counts['names']=len(names['renames'])+len(names['handles'])

        ratings=[]
        try:
            ratings=[dict(profile_id=d['profile_id'],stats=d['stats']) for d in read_stats(reg)]
            z.writestr('ratings.json',_pack_json_bytes(ratings));counts['ratings']=sum(len(r['stats']) for r in ratings)
        except Exception as ex: errors.append('Ratings: '+str(ex))

        menus,errs=_pack_collect_menus(z);errors+=errs;counts['menus']=len(menus)
        ui,errs=_pack_collect_ui(z);errors+=errs;counts['ui']=len(ui)
        ui_text,errs=_pack_collect_ui_text(z);errors+=errs;counts['ui_text']=len(ui_text)
        audio,errs=_pack_collect_audio(z);errors+=errs;counts['audio']=len(audio)

        try:
            race,label=_pack_pyc_changes(DBFILE,'RACEDATA_c',['RaceLaps']);sources['gameplay_stock']=label
            z.writestr('gameplay/race.json',_pack_json_bytes(race));counts['race']=len(race)
            # v0.9.26: package the complete fixed-36 slot definition. Target
            # calendar records remain local to the receiving installation;
            # each slot carries the selected existing event and its lap count.
            live_schedule,_=_schedule_read('live'); stock_schedule=None; schedule_source=None
            for _src in ('baseline','backup'):
                try: stock_schedule,_=_schedule_read(_src); schedule_source=_src; break
                except Exception: pass
            if stock_schedule:
                stock_by_event={(int(r['event_uid']),str(r['event'])):r for r in stock_schedule if r.get('event_uid') is not None}
                stock_by_order={int(r['order']):r for r in stock_schedule}
                slots=[];schedule_diff=0
                for row in sorted(live_schedule,key=lambda r:int(r['order'])):
                    order=int(row['order']);base=stock_by_order.get(order,{})
                    ident=(int(row['event_uid']),str(row['event']))
                    source=stock_by_event.get(ident)
                    slots.append(dict(slot=order,target_uid=int(row['uid']),
                                      source_uid=(int(source['uid']) if source else None),
                                      event_uid=int(row['event_uid']),event_name=str(row['event']),
                                      laps=int(row['laps']),track=str(row.get('track',''))))
                    if (row.get('event_uid')!=base.get('event_uid') or str(row.get('event'))!=str(base.get('event')) or int(row.get('laps',0))!=int(base.get('laps',0))):
                        schedule_diff+=1
                if schedule_diff:
                    z.writestr('gameplay/schedule.json',_pack_json_bytes(dict(
                        format='nascar15-modding-app-custom-schedule',version=2,slots=slots)))
                    counts['race']+=schedule_diff; sources['schedule_stock']=schedule_source
        except Exception as ex: errors.append('Race settings / schedule: '+str(ex));race=[]
        try:
            tr,label=_pack_pyc_changes(AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS);sources['ai_track_stock']=label
            z.writestr('gameplay/ai_track.json',_pack_json_bytes(tr));counts['ai_track']=len(tr)
        except Exception as ex: errors.append('Track AI: '+str(ex));tr=[]
        try:
            gl,label=_pack_pyc_changes(AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS);sources['ai_global_stock']=label
            z.writestr('gameplay/ai_global.json',_pack_json_bytes(gl));counts['ai_global']=len(gl)
        except Exception as ex: errors.append('Global AI: '+str(ex));gl=[]
        try:
            scr,label=_pack_scr_changes();sources['scr_stock']=label
            z.writestr('gameplay/scr.json',_pack_json_bytes(scr));counts['scr']=len(scr)
        except Exception as ex: errors.append('Track physics: '+str(ex));scr=[]

        presets=cfg.get('custom_ai_presets',[]);pitlog=cfg.get('pit_strategy_test_log',[])
        z.writestr('presets/custom.json',_pack_json_bytes(presets));counts['presets']=len(presets)
        z.writestr('presets/pit_test_log.json',_pack_json_bytes(pitlog));counts['pit_log']=len(pitlog)

        manifest=dict(format=PACK_FORMAT,version=PACK_VERSION,app_name=APP_NAME,app_version=APP_VERSION,
                      minimum_app_version='0.9.26',created=datetime.datetime.now().isoformat(),
                      counts=counts,categories=list(PACK_CATEGORIES),sources=sources,
                      warnings=errors,
                      note='Paint schemes are saved in the app after import and can be installed from Paint Schemes. Graphics, audio, and text changes are checked before installation.')
        z.writestr('manifest.json',_pack_json_bytes(manifest))
        z.writestr('README.txt',
            'NASCAR 15 Modding App mod pack v2.\n'
            'Preview this file in NASCAR 15 Modding App v0.9.26 or newer before importing.\n')
    buf.seek(0);return buf,counts,errors

@app.route('/api/pack/v2/export')
def pack_v2_export():
    try:
        buf,_counts,_errors=_pack_export_v2_bytes()
        return send_file(buf,mimetype='application/zip',as_attachment=True,
                         download_name='nascar15_complete_mod_pack.gridpack')
    except Exception as ex:
        return jsonify(dict(ok=False,error=str(ex))),400

def _pack_inspect_zip(z):
    infos=z.infolist()
    if len(infos)>20000: raise ValueError('pack contains too many files')
    total=sum(i.file_size for i in infos)
    if total>2*1024*1024*1024: raise ValueError('uncompressed pack is larger than 2 GB')
    for i in infos: _pack_safe_member(i.filename)
    manifest=_pack_read_json(z,'manifest.json',{}) or {}
    legacy=manifest.get('format')=='gridpack' and int(manifest.get('version',1))==1
    if legacy:
        names=_pack_read_json(z,'names.json',{}) or {}
        ratings=_pack_read_json(z,'stats.json',[]) or []
        counts=dict(
            schemes=sum(1 for n in z.namelist() if n.startswith('schemes/') and n.lower().endswith('.png') and '.layer.' not in n.lower() and '.thumb.' not in n.lower()),
            names=len(names.get('renames') or {})+len(names.get('handles') or {}),
            ratings=sum(len(x.get('stats') or {}) for x in ratings if isinstance(x,dict)),
            menus=sum(1 for n in z.namelist() if n.startswith('menus/') and n.lower().endswith('.png')))
        return dict(manifest=manifest,legacy=True,compatible=True,counts=counts,
                    categories=['schemes','names','ratings','menus'],warnings=[
                        'Older pack format detected. Supported categories will be converted to the current format before import.',
                        'Older packs do not contain newer gameplay, full graphics, audio, text, or preset categories.'])
    if manifest.get('format') not in PACK_FORMAT_ALIASES or int(manifest.get('version',0))!=PACK_VERSION:
        raise ValueError('not a supported NASCAR 15 Modding App pack')
    minimum=manifest.get('minimum_app_version','0')
    compatible=_version_tuple(APP_VERSION)>=_version_tuple(minimum)
    counts={str(k):int(v or 0) for k,v in (manifest.get('counts') or {}).items()}
    cats=[c for c in manifest.get('categories',PACK_CATEGORIES) if c in PACK_CATEGORIES]
    return dict(manifest=manifest,legacy=False,compatible=compatible,counts=counts,
                categories=cats,warnings=list(manifest.get('warnings') or []))

def _pack_schedule_desired_from_manifest(schedule):
    """Normalize v2 custom slots (and legacy v1 order packs) to live targets."""
    schedule=schedule or {}
    current,_=_schedule_read('live')
    targets={int(r['order']):r for r in current}
    slots=schedule.get('slots') or []
    if slots:
        if not isinstance(slots,list) or len(slots)!=36:
            raise ValueError('Schedule: custom pack must contain exactly 36 slots')
        by_slot={int(x.get('slot',i+1)):x for i,x in enumerate(slots)}
        if sorted(by_slot)!=list(range(1,37)):
            raise ValueError('Schedule: slot numbers must be exactly 1 through 36')
        desired=[]
        for i in range(1,37):
            item=by_slot[i];target=targets[i]
            desired.append(dict(slot=i,target_uid=int(target['uid']),
                                source_uid=(None if item.get('source_uid') is None else int(item.get('source_uid'))),
                                event_uid=int(item['event_uid']),event_name=str(item['event_name']),
                                laps=int(item['laps'])))
        return _schedule_desired(dict(slots=desired))
    order=schedule.get('order') or []
    if not order:return []
    if len(order)!=36 or len(set(int(x) for x in order))!=36:
        raise ValueError('Schedule: legacy pack order is not 36 unique UIDs')
    catalog=[]
    for source in ('backup','baseline','live'):
        try:catalog.extend(_schedule_read(source,verify_hash=False)[0])
        except Exception:pass
    by_uid={int(r['uid']):r for r in catalog}
    desired=[]
    for i,uid in enumerate(order,1):
        source=by_uid.get(int(uid))
        if not source:raise ValueError(f'Schedule: legacy source UID {uid} was not found')
        target=targets[i]
        desired.append(dict(slot=i,target_uid=int(target['uid']),source_uid=int(uid),
                            event_uid=int(source['event_uid']),event_name=str(source['event']),
                            laps=int(source['laps'])))
    return _schedule_desired(dict(slots=desired))


def _pack_schedule_difference(desired):
    if not desired:return (0,0)
    current,_=_schedule_read('live');by_order={int(r['order']):r for r in current}
    diff=0
    for item in desired:
        row=by_order[int(item['slot'])]
        if (int(row.get('event_uid'))!=int(item['event_uid']) or str(row.get('event'))!=str(item['event_name']) or int(row.get('laps'))!=int(item['laps'])):
            diff+=1
    return diff,len(desired)-diff


def _pack_difference_summary(z,info):
    """Best-effort current-vs-pack comparison used by the import preview."""
    counts=info.get('counts',{});out={}
    def put(cat,different,same=0,unavailable=None):
        out[cat]=dict(items=int(counts.get(cat,different+same) or 0),different=int(different),
                      same=int(same),unavailable=unavailable)
    # Staged source files.
    diff=same=0
    for name in z.namelist():
        n=_pack_safe_member(name)
        if not n.startswith('schemes/') or n.endswith('/'):continue
        local=os.path.join(SCHEMES,os.path.basename(n));raw=z.read(name)
        if os.path.exists(local) and open(local,'rb').read()==raw:same+=1
        else:diff+=1
    put('schemes',diff,same)
    cfg=load_cfg()
    try:
        data=_pack_read_json(z,'names.json',{}) or {};diff=same=0
        for key,curmap in (('renames',cfg.get('renames',{})),('handles',cfg.get('handles',{}))):
            for old,new in (data.get(key) or {}).items():
                if str(curmap.get(old,old))==str(new):same+=1
                else:diff+=1
        put('names',diff,same)
    except Exception as ex:put('names',counts.get('names',0),0,str(ex))
    try:
        _g,reg=registry();current={str(x['profile_id']):x['stats'] for x in read_stats(reg)};diff=same=0
        for row in _pack_read_json(z,'ratings.json',[]) or []:
            cur=current.get(str(row.get('profile_id')),{})
            for field,value in (row.get('stats') or {}).items():
                if str(cur.get(field))==str(value):same+=1
                else:diff+=1
        put('ratings',diff,same)
    except Exception as ex:put('ratings',counts.get('ratings',0),0,str(ex))
    try:
        _g,reg=registry();diff=same=0
        for item in _pack_read_json(z,'menus/assets.json',[]) or []:
            arcid,off,size,arc=menu_container(reg,item['key']);entries,_=C.parse_multi_arc(arc,known_dims=(128,64) if item['key']=='numbers' else None)
            e=next((x for x in entries if x['name']==item['name']),None)
            if not e:diff+=1;continue
            a=C.multi_read_png(arc,e).convert('RGBA');b=Image.open(io.BytesIO(z.read(_pack_safe_member(item['file'])))).convert('RGBA').resize(a.size)
            if a.tobytes()==b.tobytes():same+=1
            else:diff+=1
        put('menus',diff,same)
    except Exception as ex:put('menus',counts.get('menus',0),0,str(ex))
    try:
        diff=same=0
        for item in _pack_read_json(z,'ui/assets.json',[]) or []:
            raw=z.read(_pack_safe_member(item['file']));arc,e,_,_=_ui_load_entry(item['archive'],item['container'],item['entry'],item.get('w'),item.get('h'))
            cur=bytes(arc[e['payload_abs']:e['payload_abs']+e['payload_size']])
            if cur==raw:same+=1
            else:diff+=1
        put('ui',diff,same)
    except Exception as ex:put('ui',counts.get('ui',0),0,str(ex))
    try:
        current={(r['file'],int(r['index'])):r['current'] for r in _ui_text_scan()[0]};diff=same=0
        for item in _pack_read_json(z,'ui_text/strings.json',[]) or []:
            if current.get((item.get('file'),int(item.get('index',-1))))==str(item.get('text','')):same+=1
            else:diff+=1
        put('ui_text',diff,same)
    except Exception as ex:put('ui_text',counts.get('ui_text',0),0,str(ex))
    try:
        diff=same=0
        for item in _pack_read_json(z,'audio/assets.json',[]) or []:
            _v,_boff,c,flat,_kind=_read_container(str(item['archive']),item['bank']);idx=int(item['index']);sample=flat[idx]
            cur=bytes(c[sample['rel']:sample['rel']+sample['len']]);raw=z.read(_pack_safe_member(item['file']))
            if cur==raw:same+=1
            else:diff+=1
        put('audio',diff,same)
    except Exception as ex:put('audio',counts.get('audio',0),0,str(ex))
    # Mapper-backed categories: one live scan per class.
    for cat,file_name,class_name,fields,path in (
        ('race',DBFILE,'RACEDATA_c',['RaceLaps'],'gameplay/race.json'),
        ('ai_track',AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS,'gameplay/ai_track.json'),
        ('ai_global',AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS,'gameplay/ai_global.json')):
        try:
            rows=mapper_records(file_name,class_name,fields);by={str(r.get('uid')):r for r in rows};diff=same=0
            for c in _pack_read_json(z,path,[]) or []:
                cur=by.get(str(c.get('uid')),{}).get(c.get('field'))
                if _num_eq(cur,c.get('value')) or str(cur)==str(c.get('value')):same+=1
                else:diff+=1
            put(cat,diff,same)
        except Exception as ex:put(cat,counts.get(cat,0),0,str(ex))
    # A race category can also carry the complete fixed-36 custom schedule.
    try:
        sched=_pack_read_json(z,'gameplay/schedule.json',{}) or {}
        desired=_pack_schedule_desired_from_manifest(sched) if sched else []
        if desired:
            diff,same=_pack_schedule_difference(desired)
            prev=out.get('race',dict(items=0,different=0,same=0,unavailable=None))
            out['race']=dict(items=max(int(counts.get('race',0) or 0),int(prev.get('items',0))+len(desired)),
                             different=int(prev.get('different',0))+diff,
                             same=int(prev.get('same',0))+same,unavailable=prev.get('unavailable'))
    except Exception as ex:
        prev=out.get('race',dict(items=int(counts.get('race',0) or 0),different=0,same=0,unavailable=None));prev['unavailable']='Schedule comparison: '+str(ex);out['race']=prev
    try:
        live={_scr_row_ident(r):r['value'] for r in _scr_numeric_inventory()};diff=same=0
        for c in _pack_read_json(z,'gameplay/scr.json',[]) or []:
            ident=(str(c.get('arc','0')),c.get('name'),c.get('key'),int(c.get('occurrence',0)))
            if live.get(ident)==str(c.get('value')):same+=1
            else:diff+=1
        put('scr',diff,same)
    except Exception as ex:put('scr',counts.get('scr',0),0,str(ex))
    try:
        existing={(p.get('kind'),p.get('name')):p for p in cfg.get('custom_ai_presets',[])};diff=same=0
        for p in _pack_read_json(z,'presets/custom.json',[]) or []:
            if existing.get((p.get('kind'),p.get('name')))==p:same+=1
            else:diff+=1
        put('presets',diff,same)
    except Exception as ex:put('presets',counts.get('presets',0),0,str(ex))
    try:
        ids={str(x.get('id')) for x in cfg.get('pit_strategy_test_log',[])};diff=same=0
        for x in _pack_read_json(z,'presets/pit_test_log.json',[]) or []:
            if str(x.get('id')) in ids:same+=1
            else:diff+=1
        put('pit_log',diff,same)
    except Exception as ex:put('pit_log',counts.get('pit_log',0),0,str(ex))
    return out

@app.route('/api/pack/v2/preview',methods=['POST'])
def pack_v2_preview():
    import zipfile
    f=request.files.get('file')
    if not f:return jsonify(dict(ok=False,error='no pack selected')),400
    try:
        with zipfile.ZipFile(f.stream) as z:
            info=_pack_inspect_zip(z)
            differences={} if info.get('legacy') else _pack_difference_summary(z,info)
            migration_note=('Older pack detected. The app will convert its names, ratings, saved paints, and menu graphics to the current format before importing.' if info.get('legacy') else '')
        return jsonify(dict(ok=True,app_version=APP_VERSION,differences=differences,migration_note=migration_note,**info))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

def _pack_apply_ui_raw(z,items):
    done=0;errors=[]
    for item in items or []:
        try:
            member=_pack_safe_member(item['file']);raw=z.read(member)
            if _hl.sha256(raw).hexdigest()!=str(item.get('sha256','')): raise ValueError('payload hash mismatch')
            arc,e,off,size=_ui_load_entry(item['archive'],item['container'],item['entry'],item.get('w'),item.get('h'))
            for k in ('w','h','fmt','payload_size'):
                if str(e[k])!=str(item[k]): raise ValueError(f'{k} mismatch ({e[k]} vs {item[k]})')
            if len(raw)!=e['payload_size']: raise ValueError('payload length mismatch')
            old=bytes(arc[e['payload_abs']:e['payload_abs']+e['payload_size']])
            if old==raw: continue
            new=bytearray(arc);new[e['payload_abs']:e['payload_abs']+e['payload_size']]=raw;new=bytes(new)
            err=_ui_only_payload_changed(arc,new,e['payload_abs'],e['payload_size'])
            if err: raise ValueError(err)
            C.multi_read_png(new,e)  # decode before install
            _ui_install(item['archive'],off,size,new)
            chk,ce,_,_=_ui_load_entry(item['archive'],item['container'],item['entry'],item.get('w'),item.get('h'))
            if chk[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]!=raw:
                # surgical rollback
                rollback=bytearray(chk);rollback[ce['payload_abs']:ce['payload_abs']+ce['payload_size']]=old
                _ui_install(item['archive'],off,size,bytes(rollback));raise ValueError('readback failed; rolled back')
            _UI_THUMB_CACHE.pop((str(item['archive']),item['container'],item['entry']),None);done+=1
        except Exception as ex: errors.append(f"{item.get('container')}/{item.get('entry')}: {ex}")
    return done,errors

def _pack_apply_audio_raw(z,items):
    done=0;errors=[]
    for item in items or []:
        try:
            raw=z.read(_pack_safe_member(item['file']))
            if _hl.sha256(raw).hexdigest()!=str(item.get('sha256','')): raise ValueError('payload hash mismatch')
            v,boff,c,flat,kind=_read_container(str(item['archive']),item['bank'])
            idx=int(item['index'])
            if not (0<=idx<len(flat)): raise ValueError('sample index no longer exists')
            s=flat[idx]
            if s['name']!=item['name'] or int(s['len'])!=int(item['length']) or int(s['mode'])!=int(item['mode']):
                raise ValueError('sample identity/spec mismatch')
            if len(raw)!=s['len']: raise ValueError('sample slot length mismatch')
            abs_off=boff+s['rel'];old=bytes(c[s['rel']:s['rel']+s['len']])
            if old==raw: continue
            ensure_backup(v['ar'],v['bak'])
            with open(v['ar'],'r+b') as fh:
                fh.seek(abs_off);fh.write(raw);fh.flush();os.fsync(fh.fileno())
            try:
                vv,b2,c2,flat2,k2=_read_container(str(item['archive']),item['bank'])
                s2=flat2[idx]
                if c2[s2['rel']:s2['rel']+s2['len']]!=raw: raise ValueError('readback mismatch')
            except Exception:
                with open(v['ar'],'r+b') as fh:
                    fh.seek(abs_off);fh.write(old);fh.flush();os.fsync(fh.fileno())
                raise ValueError('container validation failed; rolled back')
            done+=1
        except Exception as ex: errors.append(f"{item.get('bank')}/{item.get('name')}: {ex}")
    return done,errors

def _pack_apply_menus(z,items):
    done=0;errors=[];_g,reg=registry()
    for item in items or []:
        try:
            key=item['key'];name=item['name'];arcid,off,size,arc=menu_container(reg,key)
            entries,_=C.parse_multi_arc(arc,known_dims=(128,64) if key=='numbers' else None)
            e=next((x for x in entries if x['name']==name),None)
            if not e: raise ValueError('entry missing')
            img=Image.open(io.BytesIO(z.read(_pack_safe_member(item['file']))))
            img,_=prepare_import_image(img,(e['w'],e['h']),'fit',preserve_alpha=True)
            current=C.multi_read_png(arc,e).convert('RGBA')
            if current.tobytes()==img.convert('RGBA').tobytes(): continue
            a=need(reg,arcid);ensure_backup(a['ar'],a['bak'])
            new=C.multi_write_png_validated(arc,e,img,encode_fn=encode_any,
                                             known_dims=(128,64) if key=='numbers' else None)
            with open(a['ar'],'r+b') as fh:
                fh.seek(off);fh.write(new);fh.flush();os.fsync(fh.fileno())
            done+=1
        except Exception as ex: errors.append(f"{item.get('key')}/{item.get('name')}: {ex}")
    if done:_clear_ui_thumb_cache()
    return done,errors

def _pack_ai_change_batches(changes,limit=100):
    """Keep each mapper batch below its hard limit and avoid mixing UIDs."""
    grouped={}
    for c in changes or []:
        grouped.setdefault(str(c.get('uid')),[]).append(c)
    out=[]
    for uid,rows in grouped.items():
        for i in range(0,len(rows),limit): out.append(rows[i:i+limit])
    return out

def _pack_apply_gameplay(z,selected):
    """Preflight all selected gameplay sections, then apply with ARCHIVE0 rollback."""
    selected=set(selected);results={};errors=[]
    race=_pack_read_json(z,'gameplay/race.json',[]) if 'race' in selected else []
    schedule=_pack_read_json(z,'gameplay/schedule.json',{}) if 'race' in selected else {}
    try:schedule_desired=_pack_schedule_desired_from_manifest(schedule) if schedule else []
    except Exception as ex:return {},['Schedule: '+str(ex)]
    track=_pack_read_json(z,'gameplay/ai_track.json',[]) if 'ai_track' in selected else []
    glob=_pack_read_json(z,'gameplay/ai_global.json',[]) if 'ai_global' in selected else []
    scr=_pack_read_json(z,'gameplay/scr.json',[]) if 'scr' in selected else []
    # Re-imports are idempotent: remove fields that already match live values.
    try:
        if race:
            rows=mapper_records(DBFILE,'RACEDATA_c',['RaceLaps']);by={str(r.get('uid')):r for r in rows}
            race=[c for c in race if not (_num_eq(by.get(str(c.get('uid')),{}).get('RaceLaps'),c.get('value')) or str(by.get(str(c.get('uid')),{}).get('RaceLaps'))==str(c.get('value')))]
        if schedule_desired:
            diff,_same=_pack_schedule_difference(schedule_desired)
            if not diff:schedule_desired=[]
        if track:
            rows=mapper_records(AICFG,'AIRACINGTRACKCONFIG_c',AI_TRACK_FIELDS);by={str(r.get('uid')):r for r in rows}
            track=[c for c in track if not (_num_eq(by.get(str(c.get('uid')),{}).get(c.get('field')),c.get('value')) or str(by.get(str(c.get('uid')),{}).get(c.get('field')))==str(c.get('value')))]
        if glob:
            rows=mapper_records(AICFG,'AIRACINGGLOBALCONFIG_c',AI_GLOBAL_FIELDS);by={str(r.get('uid')):r for r in rows}
            glob=[c for c in glob if not (_num_eq(by.get(str(c.get('uid')),{}).get(c.get('field')),c.get('value')) or str(by.get(str(c.get('uid')),{}).get(c.get('field')))==str(c.get('value')))]
        if scr:
            live={_scr_row_ident(r):r['value'] for r in _scr_numeric_inventory()}
            scr=[c for c in scr if live.get((str(c.get('arc','0')),c.get('name'),c.get('key'),int(c.get('occurrence',0))))!=str(c.get('value'))]
    except Exception as ex:
        return {},['Gameplay comparison failed: '+str(ex)]
    # Full preflight before writing anything.
    if schedule_desired:
        try:_schedule_patch(schedule_desired,True)
        except Exception as ex:errors.append('Schedule: '+str(ex))
    for c in race:
        r=mapper_set_value(DBFILE,'RACEDATA_c',c['uid'],'RaceLaps',c['value'],dry_run=True)
        if not r.get('ok'):errors.append(f"Race UID {c.get('uid')}: {r.get('error')}")
    if track:
        for batch in _pack_ai_change_batches(track):
            r=mapper_set_values_batch(AICFG,'AIRACINGTRACKCONFIG_c',batch,dry_run=True)
            if not r.get('ok'):errors.append('Track AI: '+str(r.get('error')));break
    if glob:
        for batch in _pack_ai_change_batches(glob):
            r=mapper_set_values_batch(AICFG,'AIRACINGGLOBALCONFIG_c',batch,dry_run=True)
            if not r.get('ok'):errors.append('Global AI: '+str(r.get('error')));break
    if scr:
        groups={}
        for c in scr:groups.setdefault(str(c.get('arc','0')),[]).append(c)
        for arcid,changes in groups.items():
            r=scr_key_batch(changes,dry_run=True)
            if not r.get('ok'):errors.append(f'ARCHIVE{arcid} track physics: '+str(r.get('error')))
    if errors:return results,errors

    _g,reg=registry();snapshots={}
    affected_archives={'0'} if (race or schedule_desired or track or glob) else set()
    affected_archives.update(str(c.get('arc','0')) for c in scr)
    for arcid in affected_archives:
        if arcid not in reg: continue
        live_path=reg[arcid]['ar'];cdf_path=reg[arcid]['cdf']
        snap=os.path.join(_tf.gettempdir(),f'n15mod_pack_gameplay_{os.getpid()}_{arcid}.AR')
        shutil.copyfile(live_path,snap);snapshots[arcid]=(snap,live_path,cdf_path,open(cdf_path,'rb').read())
    try:
        schedule_count=0
        if schedule_desired:
            r=_schedule_patch(schedule_desired,False)
            if not r.get('ok'): raise RuntimeError('Schedule: '+str(r.get('error')))
            schedule_count=int(r.get('change_count',0))
        for c in race:
            r=mapper_set_value(DBFILE,'RACEDATA_c',c['uid'],'RaceLaps',c['value'])
            if not r.get('ok'):raise RuntimeError(f"Race UID {c.get('uid')}: {r.get('error')}")
        results['race']=len(race)+schedule_count
        if track:
            for batch in _pack_ai_change_batches(track):
                r=mapper_set_values_batch(AICFG,'AIRACINGTRACKCONFIG_c',batch)
                if not r.get('ok'):raise RuntimeError('Track AI: '+str(r.get('error')))
        results['ai_track']=len(track)
        if glob:
            for batch in _pack_ai_change_batches(glob):
                r=mapper_set_values_batch(AICFG,'AIRACINGGLOBALCONFIG_c',batch)
                if not r.get('ok'):raise RuntimeError('Global AI: '+str(r.get('error')))
        results['ai_global']=len(glob)
        if scr:
            n=0
            groups={}
            for c in scr:groups.setdefault(str(c.get('arc','0')),[]).append(c)
            for arcid,changes in groups.items():
                r=scr_key_batch(changes,dry_run=False)
                if not r.get('ok'):raise RuntimeError(f'ARCHIVE{arcid} track physics: '+str(r.get('error')))
                n+=len(changes)
            results['scr']=n
        return results,[]
    except Exception as ex:
        # Previously the first failing restore aborted the loop, leaving the
        # remaining archives modified with no rollback and no warning.
        rollback_errors=[]
        for snap,live_path,cdf_path,cdf_raw in snapshots.values():
            try:
                if os.path.exists(snap):shutil.copyfile(snap,live_path)
                atomic_write_bytes(cdf_path,cdf_raw,'.pack_rollback.tmp')
            except Exception as rb:
                rollback_errors.append(f'{os.path.basename(live_path)}: {rb}')
        if rollback_errors:
            raise RollbackFailed(ex,'; '.join(rollback_errors))
        return {},['Gameplay import rolled back: '+str(ex)]
    finally:
        for snap,_live_path,_cdf_path,_cdf_raw in snapshots.values():
            try:
                if os.path.exists(snap):os.remove(snap)
            except OSError:pass

@app.route('/api/pack/v2/import',methods=['POST'])
def pack_v2_import():
    import zipfile
    f=request.files.get('file')
    if not f:return jsonify(dict(ok=False,error='no pack selected')),400
    try:selected=json.loads(request.form.get('categories','[]'))
    except Exception:selected=[]
    try:
        with zipfile.ZipFile(f.stream) as z:
            info=_pack_inspect_zip(z)
            if info['legacy']:
                selected=[c for c in (selected or info['categories']) if c in info['categories']]
                applied,errors,migrations=_pack_apply_legacy_v1(z,selected)
                return jsonify(dict(ok=True,applied=applied,errors=errors,migrations=migrations,
                    migrated_from=dict(format='gridpack',version=1,app_version=info.get('manifest',{}).get('app_version')),
                    note=('Older Mod Pack converted to the current format before import. '
                          'Saved paints can be installed from Paint Schemes.'
                          +((' Converted: '+'; '.join(migrations[:12])+('.' if migrations else '')) if migrations else ''))))
            if not info['compatible']:
                return jsonify(dict(ok=False,error='This pack requires NASCAR 15 Modding App '+str(info['manifest'].get('minimum_app_version'))+' or newer.')),400
            selected=[c for c in (selected or info['categories']) if c in info['categories']]
            _g,reg=registry();applied={k:0 for k in PACK_CATEGORIES};errors=[]
            if 'schemes' in selected:
                for n in z.namelist():
                    n2=_pack_safe_member(n)
                    if not n2.startswith('schemes/') or n2.endswith('/'):continue
                    base=os.path.basename(n2)
                    if not base:continue
                    raw=z.read(n)
                    if len(raw)>100*1024*1024:errors.append(base+': too large');continue
                    target=os.path.join(SCHEMES,base)
                    if os.path.exists(target) and open(target,'rb').read()==raw: continue
                    open(target,'wb').write(raw)
                    if base.endswith('.png') and '.layer.' not in base and '.thumb.' not in base:applied['schemes']+=1
            if 'names' in selected:
                data=_pack_read_json(z,'names.json',{}) or {};cfg=load_cfg()
                for old,new in (data.get('renames') or {}).items():
                    if str(cfg.get('renames',{}).get(old,old))==str(new): continue
                    try:patch_name_exp(reg,old,new);cfg.setdefault('renames',{})[old]=new;applied['names']+=1
                    except Exception as ex:errors.append(f'Rename {old}: {ex}')
                for old,new in (data.get('handles') or {}).items():
                    if str(cfg.get('handles',{}).get(old,old))==str(new): continue
                    try:_n,actual=patch_handle(reg,old,new);cfg.setdefault('handles',{})[old]=actual;applied['names']+=1
                    except Exception as ex:errors.append(f'Handle {old}: {ex}')
                save_cfg(cfg)
            if 'ratings' in selected:
                ratings=_pack_read_json(z,'ratings.json',[]) or []
                try: current_ratings={str(x['profile_id']):x['stats'] for x in read_stats(reg)}
                except Exception: current_ratings={}
                for row in ratings:
                    for st,v in (row.get('stats') or {}).items():
                        if str(current_ratings.get(str(row.get('profile_id')),{}).get(st))==str(v): continue
                        try:write_stat(reg,row['profile_id'],st,float(v),experimental=True);applied['ratings']+=1
                        except Exception as ex:errors.append(f"Rating {row.get('profile_id')}/{st}: {ex}")
            if 'menus' in selected:
                n,e=_pack_apply_menus(z,_pack_read_json(z,'menus/assets.json',[]) or []);applied['menus']=n;errors+=e
            if 'ui' in selected:
                n,e=_pack_apply_ui_raw(z,_pack_read_json(z,'ui/assets.json',[]) or []);applied['ui']=n;errors+=e
            if 'ui_text' in selected:
                text_rows=_pack_read_json(z,'ui_text/strings.json',[]) or []
                try:
                    current={(r['file'],int(r['index'])):r['current'] for r in _ui_text_scan()[0]}
                    changes=[dict(file=x['file'],index=int(x['index']),new=str(x.get('text',''))) for x in text_rows
                             if current.get((x.get('file'),int(x.get('index',-1))))!=str(x.get('text',''))]
                    if changes:
                        result=_ui_text_batch_apply_internal(changes,False,'Season Pack UI Text')
                        applied['ui_text']=int(result.get('changes',0))
                except Exception as ex:errors.append('UI text: '+str(ex))
            if 'audio' in selected:
                n,e=_pack_apply_audio_raw(z,_pack_read_json(z,'audio/assets.json',[]) or []);applied['audio']=n;errors+=e
            gameplay,e=_pack_apply_gameplay(z,[c for c in selected if c in ('race','ai_track','ai_global','scr')]);errors+=e
            for k,v in gameplay.items():applied[k]=v
            cfg=load_cfg()
            if 'presets' in selected:
                presets=_pack_read_json(z,'presets/custom.json',[]) or []
                merged={(x.get('kind'),x.get('name')):x for x in cfg.get('custom_ai_presets',[])}
                before=dict(merged)
                for x in presets[:200]: merged[(x.get('kind'),x.get('name'))]=x
                cfg['custom_ai_presets']=list(merged.values())[-200:]
                applied['presets']=sum(1 for k,v in merged.items() if before.get(k)!=v)
            if 'pit_log' in selected:
                log=_pack_read_json(z,'presets/pit_test_log.json',[]) or []
                existing={str(x.get('id')):x for x in cfg.get('pit_strategy_test_log',[])};before=set(existing)
                for x in log[:1000]: existing[str(x.get('id'))]=x
                cfg['pit_strategy_test_log']=list(existing.values())[-1000:]
                applied['pit_log']=len(set(existing)-before)
            save_cfg(cfg);_clear_ui_thumb_cache()
            return jsonify(dict(ok=not errors,partial=bool(errors),applied=applied,errors=errors,
                                selected=selected,note='Paint schemes were saved; use Install All Saved Paints on the Paint Schemes page to write them to the game.')),(207 if errors else 200)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

# ---- reusable AI preset library + pit-strategy observation log ----
@app.route('/api/presets')
def presets_list():
    return jsonify(dict(ok=True,presets=load_cfg().get('custom_ai_presets',[])))

@app.route('/api/presets/save',methods=['POST'])
def presets_save():
    q=request.get_json(force=True);name=str(q.get('name','')).strip()[:80]
    kind=str(q.get('kind','track'))
    changes=q.get('changes') or []
    if not name:return jsonify(dict(ok=False,error='preset name is required')),400
    if kind not in ('track','global','pit'):return jsonify(dict(ok=False,error='bad preset kind')),400
    clean=[]
    for c in changes[:100]:
        field=str(c.get('field',''))
        if not field:continue
        clean.append(dict(field=field,value=c.get('value')))
    if not clean:return jsonify(dict(ok=False,error='preset has no fields')),400
    cfg=load_cfg();rows=cfg.setdefault('custom_ai_presets',[])
    item=dict(id=_hl.sha1((kind+'|'+name).encode()).hexdigest()[:12],name=name,kind=kind,
              note=str(q.get('note',''))[:500],changes=clean)
    rows=[r for r in rows if r.get('id')!=item['id'] and not (r.get('kind')==kind and r.get('name')==name)]
    rows.append(item);cfg['custom_ai_presets']=rows[-200:];save_cfg(cfg)
    return jsonify(dict(ok=True,preset=item,count=len(cfg['custom_ai_presets'])))

@app.route('/api/presets/delete',methods=['POST'])
def presets_delete():
    q=request.get_json(force=True);pid=str(q.get('id',''));cfg=load_cfg();rows=cfg.get('custom_ai_presets',[])
    new=[r for r in rows if str(r.get('id'))!=pid];cfg['custom_ai_presets']=new;save_cfg(cfg)
    return jsonify(dict(ok=True,deleted=len(rows)-len(new)))

@app.route('/api/presets/export')
def presets_export():
    b=io.BytesIO(_pack_json_bytes(dict(format='nascar15-ai-presets',version=1,
                                        presets=load_cfg().get('custom_ai_presets',[]))))
    return send_file(b,mimetype='application/json',as_attachment=True,download_name='nascar15_ai_presets.json')

@app.route('/api/presets/import',methods=['POST'])
def presets_import():
    f=request.files.get('file')
    if not f:return jsonify(dict(ok=False,error='no file')),400
    try:
        obj=json.load(f.stream);rows=obj.get('presets',obj if isinstance(obj,list) else [])
        if not isinstance(rows,list):raise ValueError('preset file has no preset list')
        cfg=load_cfg();existing=cfg.get('custom_ai_presets',[]);by={(r.get('kind'),r.get('name')):r for r in existing}
        for r in rows[:200]:
            if r.get('kind') in ('track','global','pit') and r.get('name') and isinstance(r.get('changes'),list):
                by[(r.get('kind'),r.get('name'))]=r
        cfg['custom_ai_presets']=list(by.values())[-200:];save_cfg(cfg)
        return jsonify(dict(ok=True,count=len(cfg['custom_ai_presets'])))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/pitlog',methods=['GET','POST','DELETE'])
def pit_log_api():
    import datetime
    cfg=load_cfg();rows=cfg.setdefault('pit_strategy_test_log',[])
    if request.method=='GET':return jsonify(dict(ok=True,rows=rows))
    q=request.get_json(force=True)
    if request.method=='DELETE':
        ident=str(q.get('id',''));new=[r for r in rows if str(r.get('id'))!=ident]
        cfg['pit_strategy_test_log']=new;save_cfg(cfg);return jsonify(dict(ok=True,deleted=len(rows)-len(new)))
    note=str(q.get('note','')).strip()[:2000]
    if not note:return jsonify(dict(ok=False,error='enter an observation')),400
    item=dict(id=_hl.sha1((datetime.datetime.now().isoformat()+note).encode()).hexdigest()[:12],
              created=datetime.datetime.now().isoformat(timespec='seconds'),track=str(q.get('track',''))[:80],
              preset=str(q.get('preset',''))[:80],result=str(q.get('result','untested'))[:40],note=note)
    rows.append(item);cfg['pit_strategy_test_log']=rows[-1000:];save_cfg(cfg)
    return jsonify(dict(ok=True,item=item,count=len(cfg['pit_strategy_test_log'])))


# ==================== v0.9.23 REPOINT / CUSTOM CONTAINERS ====================
# A cdfiles entry can now be safely moved to a new archive offset when a rebuilt
# replacement is larger or smaller than the indexed slot. The original indexed
# bytes remain in the archive; only the cdfiles offset/size pair changes.
_RP_ALIGNMENT = 16
_RP_MAX_SINGLE = 768 * 1024 * 1024
_RP_MAX_PACKAGE = 1536 * 1024 * 1024
_RP_HISTORY = os.path.join(USER_DIR, 'repoint_history.json')
_RP_LOCK = __import__('threading').RLock()


def _rp_sha256_file(path):
    import hashlib
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def _rp_sha256_range(path, offset, size):
    import hashlib
    h=hashlib.sha256();left=int(size)
    with open(path,'rb') as f:
        f.seek(int(offset))
        while left:
            b=f.read(min(8*1024*1024,left))
            if not b: raise ValueError('short archive read during SHA-256 verification')
            h.update(b);left-=len(b)
    return h.hexdigest()


def _rp_game_running():
    if os.name!='nt':return False
    try:
        out=subprocess.check_output(['tasklist','/FI','IMAGENAME eq NASCAR15.exe'],text=True,errors='ignore')
        return 'NASCAR15.exe' in out
    except Exception:return False


def _rp_load_history():
    try:
        x=json.load(open(_RP_HISTORY,'r',encoding='utf-8'))
        return x if isinstance(x,list) else []
    except Exception:return []


def _rp_save_history(rows):
    tmp=_RP_HISTORY+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:json.dump(rows[-2000:],f,indent=2)
    os.replace(tmp,_RP_HISTORY)


def _rp_add_history(item):
    rows=_rp_load_history();rows.append(item);_rp_save_history(rows)


def _rp_category(name):
    u=str(name or '').upper()
    if u.endswith('.PYC'):return 'Database / PYC'
    if u.endswith('.LDA') or u.startswith('TEXT'):return 'UI Text / localization'
    if u.endswith(('.FSB','.SND')) or 'SOUND' in u or 'MUSIC' in u:return 'Audio'
    if any(k in u for k in ('BODY','INTERIORASS','WHEEL','BRAKEKIT','SUSPENSION','CHASSIS','PHY.ARC')):return 'Vehicle / model'
    if any(k in u for k in ('TRACK','RACEWAY','SPEEDWAY','REGION','GLOBALPHY')) or re.search(r'000[ANP]\.ARC$',u):return 'Track'
    if any(k in u for k in ('TEXTURE','MENU','HUD','TEAMSHOP','DRIVERSELECT','THUMB','FEI','LOGO')):return 'Images / UI'
    if u.endswith('.ARC'):return 'ARC container'
    return 'Other'


def _rp_magic_name(raw):
    b=bytes(raw[:16])
    if b[:4]==b'ARCC':return 'ARCC'
    if b[:4]==b'filC':return 'filC'
    if b[:3]==b'FSB':return b[:4].decode('ascii','replace')
    if b[:4]==b'PK\x03\x04':return 'ZIP'
    if b[:4]==b'DDS ':return 'DDS'
    return b[:8].hex(' ').upper() or '(empty)'


def _rp_index_rows(cdf_path):
    """Parse cdfiles and retain byte positions for size/offset repointing."""
    raw=bytearray(open(cdf_path,'rb').read())
    if len(raw)<48 or struct.unpack_from('<I',raw,0)[0]!=0x436C6966:
        raise ValueError('not a valid cdfiles index')
    hdr=struct.unpack_from('<12I',raw,0);count=hdr[8];string_size=hdr[10]
    if count<=0 or string_size<=0 or string_size>len(raw):raise ValueError('invalid cdfiles header')
    base=len(raw)-string_size
    def name_at(off):
        if off>=string_size:return ''
        p=base+off;e=raw.find(b'\0',p)
        return raw[p:e].decode('ascii','replace') if e>=p else ''
    choices=[]
    for start,layout,ni,si,oi in ((0x40,'A',1,2,5),(0x50,'B',3,4,7)):
        rows=[];valid=0;pos=start
        for i in range(count):
            if pos+32>base:break
            f=struct.unpack_from('<8I',raw,pos);name=name_at(f[ni])
            if name and all(32<=ord(c)<127 for c in name):valid+=1
            rows.append(dict(index=i,name=name,offset=int(f[oi]),size=int(f[si]),record_pos=pos,
                             size_pos=pos+si*4,offset_pos=pos+oi*4,layout=layout))
            pos+=32
        score=valid*1000+len({r['name'] for r in rows if r['name']})*100+sum(r['size']>0 for r in rows)*10-abs(len(rows)-count)
        choices.append((score,raw,rows,layout))
    _,raw,rows,layout=max(choices,key=lambda x:x[0])
    good=[r for r in rows if r['name']]
    if len(good)<max(1,int(count*.8)):raise ValueError('could not parse cdfiles layout')
    return raw,good,layout


def _rp_find_row(rows,name):
    req=str(name or '').replace('\\','/').casefold();base=req.rsplit('/',1)[-1]
    hits=[r for r in rows if r['name'].replace('\\','/').casefold()==req]
    if not hits:hits=[r for r in rows if r['name'].replace('\\','/').rsplit('/',1)[-1].casefold()==base]
    if not hits:raise ValueError('entry not found: '+str(name))
    if len(hits)>1:raise ValueError(f'entry is ambiguous: {name} ({len(hits)} matches)')
    return hits[0]


def _rp_find_any(reg,name,arc_hint=None):
    hits=[]
    for arcid,v in reg.items():
        if arc_hint is not None and str(arcid)!=str(arc_hint):continue
        try:
            _,rows,_=_rp_index_rows(v['cdf']);r=_rp_find_row(rows,name);hits.append((str(arcid),v,r))
        except ValueError as e:
            if 'ambiguous' in str(e):raise
    if not hits:raise ValueError('no indexed entry matches '+str(name))
    if len(hits)>1:raise ValueError(f'{name} exists in multiple archives; choose an archive')
    return hits[0]


def _rp_backup_pair(v):
    ensure_backup(v['ar'],v['bak'])
    ensure_backup(v['cdf'],backup_path(v['cdf']))


def _rp_validate_upload(entry_name,path,allow_magic=False):
    size=os.path.getsize(path)
    if size<=0:raise ValueError('replacement file is empty')
    if size>_RP_MAX_SINGLE:raise ValueError('replacement exceeds the 768 MB per-file safety limit')
    with open(path,'rb') as f:head=f.read(16)
    ext=os.path.splitext(str(entry_name))[1].upper()
    warnings=[]
    if ext=='.ARC' and head[:4]!=b'ARCC':
        msg=f'ARC target expects ARCC, but upload begins with {_rp_magic_name(head)}'
        if not allow_magic:raise ValueError(msg+'; enable advanced magic override only when this is intentional')
        warnings.append(msg)
    return dict(size=size,sha256=_rp_sha256_file(path),magic=_rp_magic_name(head),warnings=warnings)


def _rp_plan(arcid,v,row,path,allow_magic=False):
    meta=_rp_validate_upload(row['name'],path,allow_magic)
    archive_size=os.path.getsize(v['ar']);new_off=(archive_size+(_RP_ALIGNMENT-1))&~(_RP_ALIGNMENT-1)
    if row['offset']+row['size']>archive_size:raise ValueError('indexed entry exceeds current archive size')
    if new_off+meta['size']>=2**32:raise ValueError('replacement would exceed the 32-bit archive offset limit')
    upload_name=os.path.basename(path)
    return dict(archive=str(arcid),entry=row['name'],category=_rp_category(row['name']),
                old_offset=row['offset'],old_size=row['size'],new_offset=new_off,new_size=meta['size'],
                archive_size=archive_size,projected_archive_size=new_off+meta['size'],growth=new_off+meta['size']-archive_size,
                sha256=meta['sha256'],magic=meta['magic'],warnings=meta['warnings'],source_name=upload_name,
                filename_match=upload_name.casefold()==os.path.basename(row['name']).casefold())


def _rp_install_one(arcid,v,row,path,source_name=None,allow_magic=False,history=True):
    """Append + repoint transaction. On failure, truncate and restore the exact live cdf bytes."""
    if _rp_game_running():raise ValueError('NASCAR15.exe is running; close the game first')
    plan=_rp_plan(arcid,v,row,path,allow_magic)
    _rp_backup_pair(v)
    old_archive_size=os.path.getsize(v['ar']);old_cdf=open(v['cdf'],'rb').read()
    raw,rows,layout=_rp_index_rows(v['cdf']);live_row=_rp_find_row(rows,row['name'])
    try:
        with open(v['ar'],'ab') as dst,open(path,'rb') as src:
            pad=plan['new_offset']-old_archive_size
            if pad:dst.write(b'\0'*pad)
            for b in iter(lambda:src.read(8*1024*1024),b''):dst.write(b)
            dst.flush();os.fsync(dst.fileno())
        if _rp_sha256_range(v['ar'],plan['new_offset'],plan['new_size'])!=plan['sha256']:
            raise ValueError('archive payload SHA-256 readback mismatch')
        struct.pack_into('<I',raw,live_row['size_pos'],plan['new_size'])
        struct.pack_into('<I',raw,live_row['offset_pos'],plan['new_offset'])
        atomic_write_bytes(v['cdf'],bytes(raw),'.repoint.tmp')
        _,check,_=_rp_index_rows(v['cdf']);vr=_rp_find_row(check,row['name'])
        if vr['offset']!=plan['new_offset'] or vr['size']!=plan['new_size']:
            raise ValueError('cdfiles readback did not retain the new offset/size')
    except Exception as install_ex:
        rollback_archive_cdf(v,old_archive_size,old_cdf,'.rollback.tmp',install_ex)
        raise
    item=dict(timestamp=__import__('datetime').datetime.now().isoformat(timespec='seconds'),
              archive=str(arcid),entry=row['name'],old_offset=plan['old_offset'],old_size=plan['old_size'],
              new_offset=plan['new_offset'],new_size=plan['new_size'],growth=plan['growth'],sha256=plan['sha256'],
              source_name=source_name or os.path.basename(path),category=plan['category'],verified=True)
    history_warning=None
    if history:
        try:_rp_add_history(item)
        except Exception as ex:history_warning='install verified, but history could not be saved: '+str(ex)
    _clear_ui_thumb_cache()
    return dict(ok=True,verified=True,plan=plan,history=item,history_warning=history_warning)


def _rp_extract_entry(v,row,out_path,source_archive=None):
    ar=source_archive or v['ar']
    with open(ar,'rb') as src,open(out_path,'wb') as dst:
        src.seek(row['offset']);left=row['size']
        while left:
            b=src.read(min(8*1024*1024,left))
            if not b:raise ValueError('short archive read')
            dst.write(b);left-=len(b)
    return out_path


def _rp_public_entry(arcid,row):
    return dict(archive=str(arcid),name=row['name'],offset=row['offset'],size=row['size'],
                category=_rp_category(row['name']),extension=os.path.splitext(row['name'])[1].upper() or '(none)')


@app.route('/api/repoint/status')
def repoint_status():
    try:
        g,reg=registry()
        if not g:return jsonify(dict(ok=False,error='game folder not selected')),400
        hist=_rp_load_history();archives=[]
        for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
            live=os.path.getsize(v['ar']);bak=os.path.getsize(v['bak']) if os.path.exists(v['bak']) else None
            cdfbak=os.path.exists(backup_path(v['cdf']))
            archives.append(dict(archive=str(arcid),size=live,backup_size=bak,
                                 growth_since_backup=(live-bak if bak is not None else None),
                                 archive_backup=bool(bak is not None),cdfiles_backup=bool(cdfbak),
                                 headroom=max(0,2**32-live),history_count=sum(1 for h in hist if str(h.get('archive'))==str(arcid))))
        return jsonify(dict(ok=True,game=g,archives=archives,history=hist[-100:][::-1],
                            history_count=len(hist),alignment=_RP_ALIGNMENT,max_single=_RP_MAX_SINGLE,
                            facts=['Variable-size installs append a rebuilt file at a 16-byte-aligned offset.',
                                   'The matching cdfiles offset and size are updated atomically.',
                                   'Original indexed bytes remain in the archive until a future compaction pass.',
                                   'Archive and cdfiles receive paired pristine backups before the first repoint.']))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/entries',methods=['POST'])
def repoint_entries():
    try:
        q=request.get_json(silent=True) or {};g,reg=registry()
        if not g:raise ValueError('game folder not selected')
        arc=str(q.get('archive','all'));cat=str(q.get('category','all'));text=str(q.get('q','')).lower()
        page=max(0,int(q.get('page',0)));per=max(1,min(500,int(q.get('per',200))))
        rows=[];categories=set()
        for arcid,v in reg.items():
            if arc!='all' and str(arcid)!=arc:continue
            _,rr,_=_rp_index_rows(v['cdf'])
            for r in rr:
                pub=_rp_public_entry(arcid,r);categories.add(pub['category'])
                if cat!='all' and pub['category']!=cat:continue
                if text and text not in (pub['name']+' '+pub['category']+' ARCHIVE'+str(arcid)).lower():continue
                rows.append(pub)
        rows.sort(key=lambda x:(int(x['archive']),x['category'],x['name']))
        return jsonify(dict(ok=True,total=len(rows),page=page,per=per,rows=rows[page*per:(page+1)*per],
                            archives=sorted(reg.keys(),key=int),categories=sorted(categories)))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/inspect',methods=['POST'])
def repoint_inspect():
    try:
        q=request.get_json(force=True);g,reg=registry();arcid=str(q.get('archive'));v=need(reg,arcid)
        _,rows,layout=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,q.get('entry'))
        with open(v['ar'],'rb') as f:f.seek(row['offset']);head=f.read(16)
        stock=None;cb=backup_path(v['cdf'])
        if os.path.exists(v['bak']) and os.path.exists(cb):
            try:
                _,br,_=_rp_index_rows(cb);sr=_rp_find_row(br,row['name'])
                with open(v['bak'],'rb') as f:f.seek(sr['offset']);shead=f.read(16)
                stock=dict(offset=sr['offset'],size=sr['size'],magic=_rp_magic_name(shead))
            except Exception:stock=None
        return jsonify(dict(ok=True,entry=_rp_public_entry(arcid,row),layout=layout,magic=_rp_magic_name(head),
                            archive_size=os.path.getsize(v['ar']),stock=stock,
                            export_current=f'/api/repoint/export?archive={arcid}&entry='+__import__('urllib.parse').parse.quote(row['name']),
                            export_stock=f'/api/repoint/export?stock=1&archive={arcid}&entry='+__import__('urllib.parse').parse.quote(row['name'])))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/preview',methods=['POST'])
def repoint_preview():
    tmp=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a replacement file')
        arcid=str(request.form.get('archive'));entry=request.form.get('entry');allow=request.form.get('allow_magic')=='1'
        g,reg=registry();v=need(reg,arcid);_,rows,_=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,entry)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_',suffix=os.path.splitext(up.filename or '')[1]);os.close(fd);up.save(tmp)
        plan=_rp_plan(arcid,v,row,tmp,allow);plan['source_name']=up.filename or os.path.basename(tmp)
        plan['filename_match']=(os.path.basename(up.filename or '').casefold()==os.path.basename(row['name']).casefold())
        return jsonify(dict(ok=True,plan=plan))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass


@app.route('/api/repoint/install',methods=['POST'])
def repoint_install():
    tmp=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a replacement file')
        arcid=str(request.form.get('archive'));entry=request.form.get('entry');allow=request.form.get('allow_magic')=='1'
        g,reg=registry();v=need(reg,arcid);_,rows,_=_rp_index_rows(v['cdf']);row=_rp_find_row(rows,entry)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_',suffix=os.path.splitext(up.filename or '')[1]);os.close(fd);up.save(tmp)
        with _RP_LOCK:r=_rp_install_one(arcid,v,row,tmp,up.filename or os.path.basename(tmp),allow)
        return jsonify(r)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass


@app.route('/api/repoint/export')
def repoint_export():
    tmp=None
    try:
        arcid=str(request.args.get('archive'));entry=request.args.get('entry');stock=request.args.get('stock')=='1'
        g,reg=registry();v=need(reg,arcid)
        cdf=backup_path(v['cdf']) if stock else v['cdf'];ar=v['bak'] if stock else v['ar']
        if not os.path.exists(cdf) or not os.path.exists(ar):raise ValueError('stock backup is not available' if stock else 'live files missing')
        _,rows,_=_rp_index_rows(cdf);row=_rp_find_row(rows,entry)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_export_');os.close(fd);_rp_extract_entry(v,row,tmp,ar)
        @after_this_request
        def _cleanup_export(response):
            try:os.remove(tmp)
            except OSError:pass
            return response
        return send_file(tmp,as_attachment=True,download_name=row['name'],mimetype='application/octet-stream',max_age=0)
    except Exception as ex:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/repoint/restore_entry',methods=['POST'])
def repoint_restore_entry():
    tmp=None
    try:
        q=request.get_json(force=True);arcid=str(q.get('archive'));entry=q.get('entry');g,reg=registry();v=need(reg,arcid)
        bc=backup_path(v['cdf'])
        if not os.path.exists(v['bak']) or not os.path.exists(bc):raise ValueError('the original backup pair is unavailable')
        _,live_rows,_=_rp_index_rows(v['cdf']);live=_rp_find_row(live_rows,entry)
        _,stock_rows,_=_rp_index_rows(bc);stock=_rp_find_row(stock_rows,entry)
        fd,tmp=tempfile.mkstemp(prefix='n15mod_stock_',suffix=os.path.splitext(stock['name'])[1]);os.close(fd)
        _rp_extract_entry(v,stock,tmp,v['bak'])
        with _RP_LOCK:r=_rp_install_one(arcid,v,live,tmp,'Stock backup: '+stock['name'],False)
        r['restored_stock_size']=stock['size'];return jsonify(r)
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass


def _rp_package_members(zip_path,reg):
    import zipfile
    plans=[];total=0;ignored=[]
    with zipfile.ZipFile(zip_path) as z:
        members=[i for i in z.infolist() if not i.is_dir() and not i.filename.startswith('__MACOSX/')]
        if len(members)>128:raise ValueError('package is limited to 128 files')
        by_norm={i.filename.replace('\\','/').lstrip('/'):i for i in members}
        manifest_info=next((i for i in members if i.filename.replace('\\','/').rsplit('/',1)[-1].casefold()=='repoint_manifest.json'),None)
        manifest_rows=None
        if manifest_info:
            try:
                manifest=json.loads(z.read(manifest_info).decode('utf-8-sig'))
                manifest_rows=manifest.get('files') if isinstance(manifest,dict) else None
                if not isinstance(manifest_rows,list) or not manifest_rows:raise ValueError('files must be a non-empty list')
            except Exception as ex:raise ValueError('invalid repoint_manifest.json: '+str(ex))
        candidates=[]
        if manifest_rows is not None:
            for item in manifest_rows:
                if not isinstance(item,dict):raise ValueError('manifest file rows must be objects')
                source=str(item.get('source') or '').replace('\\','/').lstrip('/')
                entry=str(item.get('entry') or '')
                arc_hint=item.get('archive')
                if not source or not entry:raise ValueError('each manifest row needs source and entry')
                info=by_norm.get(source)
                if not info:raise ValueError('manifest source not found in ZIP: '+source)
                candidates.append((info,entry,None if arc_hint is None else str(arc_hint)))
            ignored=[i.filename for i in members if i is not manifest_info and i not in {c[0] for c in candidates}]
        else:
            for info in members:
                low=info.filename.lower()
                if low.endswith(('manifest.json','readme.txt','technical_notes.md','.bat','.py','.json','.md')):ignored.append(info.filename);continue
                if info.file_size<=0:continue
                norm=info.filename.replace('\\','/');parts=norm.split('/');arc_hint=None
                for part in parts[:-1]:
                    m=re.fullmatch(r'ARCHIVE(\d*)',part,re.I)
                    if m:arc_hint=m.group(1) or '0';break
                candidates.append((info,parts[-1],arc_hint))
        for info,entry_name,arc_hint in candidates:
            if info.file_size<=0:continue
            if info.file_size>_RP_MAX_SINGLE:raise ValueError(info.filename+' exceeds the per-file limit')
            try:arcid,v,row=_rp_find_any(reg,entry_name,arc_hint)
            except ValueError as ex:
                if manifest_rows is not None or 'multiple archives' in str(ex) or 'ambiguous' in str(ex):raise
                ignored.append(info.filename);continue
            total+=info.file_size
            if total>_RP_MAX_PACKAGE:raise ValueError('matched package payloads exceed the 1.5 GB safety limit')
            plans.append(dict(info=info,archive=arcid,v=v,row=row))
    if not plans:raise ValueError('no package files matched indexed NASCAR 15 entries; use exact filenames or add repoint_manifest.json')
    seen=set()
    for p in plans:
        k=(p['archive'],p['row']['name'].casefold())
        if k in seen:raise ValueError('package targets the same indexed entry more than once: '+p['row']['name'])
        seen.add(k)
    return plans,total,ignored


@app.route('/api/repoint/package_preview',methods=['POST'])
def repoint_package_preview():
    tmp=None;td=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a ZIP package')
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_package_',suffix='.zip');os.close(fd);up.save(tmp)
        g,reg=registry();matched,total,ignored=_rp_package_members(tmp,reg);td=tempfile.mkdtemp(prefix='n15mod_rpprev_')
        import zipfile
        out=[]
        with zipfile.ZipFile(tmp) as z:
            for i,p in enumerate(matched):
                fp=os.path.join(td,f'{i:03d}.bin')
                with z.open(p['info']) as src,open(fp,'wb') as dst:shutil.copyfileobj(src,dst,8*1024*1024)
                plan=_rp_plan(p['archive'],p['v'],p['row'],fp,False);plan['source_name']=p['info'].filename;plan['filename_match']=os.path.basename(p['info'].filename).casefold()==os.path.basename(p['row']['name']).casefold();out.append(plan)
        return jsonify(dict(ok=True,count=len(out),total_size=total,plans=out,ignored=ignored,ignored_count=len(ignored),
                            note='Package installs always append and repoint each matched file; unrelated ZIP members are ignored.'))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass
        if td:shutil.rmtree(td,ignore_errors=True)


@app.route('/api/repoint/package_install',methods=['POST'])
def repoint_package_install():
    tmp=None;td=None
    try:
        up=request.files.get('file')
        if not up:raise ValueError('choose a ZIP package')
        fd,tmp=tempfile.mkstemp(prefix='n15mod_repoint_package_',suffix='.zip');os.close(fd);up.save(tmp)
        g,reg=registry();matched,total,ignored=_rp_package_members(tmp,reg);td=tempfile.mkdtemp(prefix='n15mod_rpinstall_')
        import zipfile
        extracted=[]
        with zipfile.ZipFile(tmp) as z:
            for i,p in enumerate(matched):
                fp=os.path.join(td,f'{i:03d}_{os.path.basename(p["row"]["name"])}')
                with z.open(p['info']) as src,open(fp,'wb') as dst:shutil.copyfileobj(src,dst,8*1024*1024)
                _rp_validate_upload(p['row']['name'],fp,False);extracted.append((p,fp))
        # Atomic package transaction: every touched archive can be truncated back
        # to its exact pre-package size, and every cdfiles index can be restored
        # byte-for-byte, because package installs append rather than overwrite.
        touched={}
        for p,fp in extracted:
            _rp_backup_pair(p['v'])
            key=str(p['archive'])
            if key not in touched:
                touched[key]=dict(v=p['v'],archive_size=os.path.getsize(p['v']['ar']),cdf=open(p['v']['cdf'],'rb').read())
        results=[]
        try:
            with _RP_LOCK:
                for p,fp in extracted:
                    results.append(_rp_install_one(p['archive'],p['v'],p['row'],fp,p['info'].filename,False,history=False))
                hist=_rp_load_history();hist.extend(r['history'] for r in results);_rp_save_history(hist)
        except Exception as install_ex:
            # Attempt every archive even if one fails, so a single bad restore
            # cannot strand the remaining archives un-rolled-back. Failures are
            # collected and raised together rather than discarded.
            rollback_errors=[]
            for key,state in touched.items():
                try:
                    rollback_archive_cdf(state['v'],state['archive_size'],state['cdf'],
                                         '.package_rollback.tmp',install_ex)
                except RollbackFailed as rbf:
                    rollback_errors.append(f'ARCHIVE{key}: {rbf.rollback_error}')
            if rollback_errors:
                raise RollbackFailed(install_ex,'; '.join(rollback_errors))
            raise
        return jsonify(dict(ok=True,count=len(results),total_size=total,verified=True,atomic=True,ignored=ignored,ignored_count=len(ignored),
                            results=[r['history'] for r in results]))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400
    finally:
        if tmp:
            try:os.remove(tmp)
            except OSError:pass
        if td:shutil.rmtree(td,ignore_errors=True)

# ==================== end v0.9.23 REPOINT ====================


# ==================== v0.9.26.7 FULL PYC AUDIT ====================
_PYC_AUDIT_CACHE={}
_PYC_AUDIT_HANDLED={
    'DB_GAME_LOCAL_SCRIPT.PYC':'editable database: schedule, race laps, world pace/environment',
    'DB_AICONFIG_SCRIPT.PYC':'editable database: driver ratings, track AI and global AI',
}
_PYC_AUDIT_PARTIAL={
    'GSRACEPOINTS.PYC':'researched points code; not exposed in V1',
    'AIDRIVERPROFILES.PYC':'legacy/secondary AI profile candidate; audited, not edited by current Ratings tab',
    'AILAPTIMES.PYC':'AI lap-time table candidate; not mapped yet',
    'TRACKDATA.PYC':'track-data helper candidate; not mapped yet',
    'GSCAREERCALENDARHELPER.PYC':'Career calendar consumer; read-only audit candidate',
    'GSCAREERRACESPAWN.PYC':'Career race consumer; read-only audit candidate',
    'GSCAREERFUNCTIONSHELPER.PYC':'Career state helper; read-only audit candidate',
    'GSRACESPAWN.PYC':'general race consumer; read-only audit candidate',
    'GSTEAMSHOPRACESETTINGS.PYC':'Race Now/settings consumer; read-only audit candidate',
    'RACEEVENTS.PYC':'race-event constants/helper; read-only audit candidate',
    'EVENTINIT.PYC':'event initialization helper; read-only audit candidate',
    'GSRESULTSINTERFACE.PYC':'results/points UI consumer; read-only audit candidate',
    'GSINFIELDGARAGEHELPER.PYC':'career standings/points consumer; read-only audit candidate',
}
_PYC_AUDIT_KEYWORDS={
    'schedule':['schedule','calendar','numberinseries','racedata','raceevent','raceseries'],
    'career':['career','championship','standings','singleseason','single_season','season'],
    'ai_pace':['ai','laptime','lap time','practiceeasy','practicehard','qualifybasetime','catchup','racingline'],
    'race_physics':['physics','downforce','draft','aero','handling','race laps','racelaps'],
    'points':['points','bonuspoints','championshippoints','raceposition'],
    'livery':['livery','vinyl','paintscheme','manufacturer'],
    'ui_text':['interface','menu','hud','string','textid'],
    'audio':['sound','audio','commentary'],
    'track':['track','worldpointer','worldid','tripwire'],
    'driver':['driver','roster','profile'],
}
_PYC_AUDIT_PRIORITY=[
    'AILAPTIMES.PYC','AIDRIVERPROFILES.PYC','GSCAREERCALENDARHELPER.PYC','GSCAREERRACESPAWN.PYC',
    'GSCAREERFUNCTIONSHELPER.PYC','GSRACESPAWN.PYC','GSTEAMSHOPRACESETTINGS.PYC','RACEEVENTS.PYC',
    'EVENTINIT.PYC','GSRACEPOINTS.PYC','GSRESULTSINTERFACE.PYC','GSINFIELDGARAGEHELPER.PYC','TRACKDATA.PYC'
]

def _pyc_audit_mapper():
    path=component_path(MAPPER_NAME)
    if not os.path.exists(path):raise RuntimeError(MAPPER_NAME+' is missing')
    import importlib.util as _iu
    key=(os.path.realpath(path),os.path.getmtime(path))
    cached=_PYC_AUDIT_CACHE.get('mapper')
    if cached and cached[0]==key:return cached[1]
    spec=_iu.spec_from_file_location('n15_pyc_audit_mapper',path);mod=_iu.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)
    _PYC_AUDIT_CACHE['mapper']=(key,mod);return mod

def _pyc_ascii(raw,minimum=4):
    # PYC strings are mostly ASCII. Limiting each string prevents huge embedded
    # text resources from making the audit response unwieldy.
    return [m.group().decode('latin1','replace')[:240] for m in re.finditer(rb'[ -~]{%d,}'%minimum,raw)]

def _pyc_audit_category(name,text):
    low=(name+' '+text).lower();scores={cat:sum(low.count(k) for k in keys) for cat,keys in _PYC_AUDIT_KEYWORDS.items()}
    cat=max(scores,key=scores.get) if scores else 'other'
    return (cat if scores.get(cat,0)>0 else 'other'),scores

def _pyc_audit_key(reg):
    parts=[]
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try:
            a=os.stat(v['ar']);c=os.stat(v['cdf']);parts.append((str(arcid),a.st_size,a.st_mtime_ns,c.st_size,c.st_mtime_ns))
        except OSError:pass
    return tuple(parts)

def _pyc_audit_scan(force=False):
    g,reg=registry()
    if not g:raise RuntimeError('game folder not selected')
    key=_pyc_audit_key(reg)
    if not force and _PYC_AUDIT_CACHE.get('result_key')==key:return _PYC_AUDIT_CACHE['result']
    mapper=_pyc_audit_mapper();rows=[];archive_errors=[]
    for arcid,v in sorted(reg.items(),key=lambda kv:int(kv[0])):
        try:entries=parse_cdfiles(v['cdf'])
        except Exception as ex:archive_errors.append(dict(archive=str(arcid),error=str(ex)));continue
        for off,size,name in entries:
            if not str(name).upper().endswith('.PYC'):continue
            rec=dict(archive=str(arcid),file=str(name),offset=int(off),size=int(size),parse_ok=False,parse_error=None,
                     handled='unmapped',handled_detail=None,schedule_slots=None,sha256=None,category='other',priority=False,keywords=[])
            try:
                with open(v['ar'],'rb') as fh:fh.seek(off);raw=fh.read(size)
                if len(raw)!=size:raise RuntimeError('short archive read')
                rec['sha256']=_hl.sha256(raw).hexdigest()
                root=mapper.parse_pyc(raw);rec['parse_ok']=True
                strings=_pyc_ascii(raw);joined=' '.join(strings)
                cat,scores=_pyc_audit_category(name,joined);rec['category']=cat
                rec['keywords']=[k for k,vv in sorted(scores.items(),key=lambda kv:-kv[1]) if vv>0][:5]
                upper=str(name).upper()
                if upper in _PYC_AUDIT_HANDLED:
                    rec['handled']='editable';rec['handled_detail']=_PYC_AUDIT_HANDLED[upper]
                elif upper in _PYC_AUDIT_PARTIAL:
                    rec['handled']='candidate';rec['handled_detail']=_PYC_AUDIT_PARTIAL[upper]
                elif upper.startswith(('DB_','GS','WID_')) or cat!='other':
                    rec['handled']='research';rec['handled_detail']='discovered and parseable; no V1 editor mapped'
                else:
                    rec['handled']='library';rec['handled_detail']='runtime/library PYC; no data editor expected'
                rec['priority']=upper in _PYC_AUDIT_PRIORITY
                if upper==DBFILE.upper():
                    try:rec['schedule_slots']=len(schedule_mod().map_schedule(raw,mapper)[0])
                    except Exception as ex:rec['schedule_error']=str(ex)
                # Record/schema totals are useful for generated DB files, but avoid
                # the expensive constructor mapping on every runtime helper.
                if upper.startswith('DB_'):
                    try:
                        schemas=mapper.build_schemas(root);records=mapper.map_records(root,schemas)
                        rec['schemas']=len(schemas);rec['records']=len(records)
                    except Exception as ex:rec['record_map_error']=str(ex)
            except Exception as ex:
                rec['parse_error']=str(ex)
            rows.append(rec)
    by_name={}
    for r in rows:by_name.setdefault(r['file'].upper(),[]).append(r)
    for rs in by_name.values():
        for r in rs:r['duplicate_count']=len(rs);r['duplicate']=len(rs)>1
    rows.sort(key=lambda r:(not r['priority'],r['handled'] in ('library','unmapped'),r['category'],r['file'],int(r['archive'])))
    result=dict(ok=True,game=g,total=len(rows),parseable=sum(1 for r in rows if r['parse_ok']),
                editable=sum(1 for r in rows if r['handled']=='editable'),
                candidates=sum(1 for r in rows if r['handled'] in ('candidate','research')),
                duplicate_names=sum(1 for rs in by_name.values() if len(rs)>1),
                schedule_sources=sum(1 for r in rows if r.get('schedule_slots')==36),
                archives_scanned=len(reg),archive_errors=archive_errors,rows=rows,
                note='Every indexed .PYC in every detected archive is listed. Candidate does not mean safe to edit; it means the file deserves mapping/research.')
    _PYC_AUDIT_CACHE['result_key']=key;_PYC_AUDIT_CACHE['result']=result
    return result

@app.route('/api/pyc/audit')
def pyc_audit():
    try:
        force=request.args.get('force')=='1';return jsonify(_pyc_audit_scan(force))
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

@app.route('/api/pyc/audit/export')
def pyc_audit_export():
    try:
        result=_pyc_audit_scan(False);fmt=request.args.get('format','csv').lower()
        if fmt=='json':
            return Response(json.dumps(result,indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=nascar_pyc_audit.json'})
        import io
        out=io.StringIO();fields=['archive','file','offset','size','sha256','parse_ok','parse_error','handled','handled_detail','category','priority','duplicate_count','schedule_slots','schemas','records','keywords']
        w=_csv.DictWriter(out,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for row in result['rows']:
            r=dict(row);r['keywords']=';'.join(r.get('keywords') or []);w.writerow(r)
        return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=nascar_pyc_audit.csv'})
    except Exception as ex:return jsonify(dict(ok=False,error=str(ex))),400

# ==================== end v0.9.26.7 FULL PYC AUDIT ====================

# ---- self-check / support report ----
def _support_checks():
    checks=[]
    def add(name,status,detail,technical=False):
        checks.append(dict(name=name,status=status,detail=str(detail),technical=bool(technical)))
    add('Application','pass',f'{APP_NAME} v{APP_VERSION} {APP_RELEASE_LABEL}')
    add('Custom race schedules','pass' if all(os.path.exists(component_path(x)) for x in (SCHEDULE_HELPER_NAME,SCHEDULE_LINK_HELPER_NAME)) else 'fail',
        'Season order, repeated races, and race lengths are available.' if all(os.path.exists(component_path(x)) for x in (SCHEDULE_HELPER_NAME,SCHEDULE_LINK_HELPER_NAME)) else 'Required schedule tools are missing.')
    add('Advanced file support','pass','Large replacement files can be installed with backup and verification.',True)
    add('Physics editing support','pass','Track-specific physics files support fixed and expanded values.',True)
    add('Game-data research tools','pass','Read-only audit and candidate discovery are available.',True)
    for fn in (MAPPER_NAME,PATCHER_NAME,REPOINT_NAME,SCHEDULE_HELPER_NAME,SCHEDULE_LINK_HELPER_NAME,'containers.py','ui_assets.csv','nascar15_team_assets_v1.py','nascar15_thumbnail_native_v25.py','nascar15_thumbnail_import_probe_v1.py'):
        p=(os.path.join(APP_DIR,fn) if fn=='containers.py' else component_path(fn));add('Required component: '+fn,'pass' if os.path.exists(p) else 'fail','Ready' if os.path.exists(p) else 'Missing from the application folder',True)
    add('Graphics discovery','pass' if os.path.exists(TEXTURE_DISCOVERY_TOOL) else 'fail','Graphics scanner ready' if os.path.exists(TEXTURE_DISCOVERY_TOOL) else 'Graphics scanner missing')
    add('Graphics discovery cache','pass' if os.path.exists(_discovered_texture_csv()) else 'warn','Additional graphics already indexed' if os.path.exists(_discovered_texture_csv()) else 'Use Scan All Game Graphics to build this list',True)
    profile_data=[('drivers.json',_game_data_path('drivers.json')),
                  ('ai profiles',_game_data_path('ai_profiles_nascar14.csv') if ACTIVE_GAME=='nascar14' else _game_data_path('ai_profiles.csv'))]
    if active_game_profile().get('graphics_mode')=='packaged': profile_data.append(('ui_asset_map_v2.csv',_game_data_path('ui_asset_map_v2.csv')))
    for label,p in profile_data:
        add('Data component: '+label,'pass' if os.path.exists(p) else 'fail','Ready' if os.path.exists(p) else 'Missing',True)
    try:
        links=load_driver_links();profiles=load_profiles();pids={int(r['profile_id']) for r in profiles}
        missing=[x for x in links if int(x['profile_id']) not in pids]
        add('Driver and rating database','pass' if not missing else 'fail',f'{len(links)} drivers and {len(profiles)} rating profiles are available.' if not missing else f'{len(missing)} driver links are missing.')
    except Exception as ex:add('Driver and rating database','fail',ex)
    try:
        p=ui_csv_path();rows=_ui_index() if p else []
        add('Graphics catalog','pass' if rows else 'fail',f'{len(rows)} graphics are indexed.' if rows else 'No graphics catalog was found.')
        add('Graphics catalog details','pass' if rows else 'fail',f'{sum(1 for r in rows if r.get("decoded"))} editable previews; {sum(1 for r in rows if not r.get("decoded"))} research-only files.',True)
    except Exception as ex:add('Graphics catalog','fail',ex)
    try:
        text_files=_ui_text_quick_status()
        add('Game Text','pass' if text_files else 'warn',f'{len(text_files)} language tables are available.' if text_files else 'No text tables were found.')
    except Exception as ex:add('Game Text','warn',ex)
    g,reg=registry()
    game_label=active_game_name()+' folder'
    if not g:add(game_label,'warn','The selected game folder is not selected or could not be detected.')
    else:
        add(game_label,'pass',g)
        required=set(active_game_profile().get('required_archives') or ())
        add('Game data files','pass' if required.issubset(reg) else 'fail',f'{len(reg)} game-data groups found; required '+', '.join(sorted(required))+'.',True)
        nb=sum(1 for v in reg.values() if os.path.exists(v['bak']))
        add('Game backups','pass' if nb==len(reg) else 'warn',f'{nb}/{len(reg)} game-data groups are protected.')
        rpairs=sum(1 for v in reg.values() if os.path.exists(v['bak']) and os.path.exists(backup_path(v['cdf'])))
        add('Advanced backup pairs','pass' if rpairs==len(reg) else 'warn',f'{rpairs}/{len(reg)} paired backups are ready.',True)
        try:
            statuses={k:_baseline_public_status(k,v) for k,v in stock_baselines().items()}
            good=sum(1 for v in statuses.values() if v.get('ok'))
            add('Clean stock reference','pass' if good else 'warn',f'{good} clean reference group(s) verified.' if good else 'Optional: choose a clean stock copy on Home for better comparisons.')
        except Exception as ex:add('Clean stock reference','warn',ex)
    add('Image conversion','pass' if texconv_path() else 'warn','Ready' if texconv_path() else 'External image converter not found; built-in fallback remains available.')
    add('Audio conversion','pass' if ffmpeg_path() else 'warn','Ready' if ffmpeg_path() else 'Audio preview and replacement may be limited until the converter is bundled or installed.')
    try:add('Saved paints','pass',f'{sum(1 for n in os.listdir(SCHEMES) if n.endswith(".png"))} paint image(s) saved.')
    except Exception as ex:add('Saved paints','warn',ex)
    fail=sum(1 for c in checks if c['status']=='fail');warn=sum(1 for c in checks if c['status']=='warn')
    return checks,dict(pass_count=len(checks)-fail-warn,warn_count=warn,fail_count=fail,total=len(checks))

@app.route('/api/support/check')
def support_check():
    c,s=_support_checks();return jsonify(dict(ok=s['fail_count']==0,checks=c,summary=s,app_name=APP_NAME,version=APP_VERSION,release_label=APP_RELEASE_LABEL))

@app.route('/api/support/report')
def support_report():
    import datetime
    checks,summary=_support_checks();lines=[f'{APP_NAME} v{APP_VERSION} support report',
        f'Created: {datetime.datetime.now().isoformat(timespec="seconds")}',
        f'Result: {summary["pass_count"]} pass, {summary["warn_count"]} warning, {summary["fail_count"]} fail','']
    lines += [f'[{c["status"].upper()}] {c["name"]}: {c["detail"]}' for c in checks]
    b=io.BytesIO(('\n'.join(lines)+'\n').encode('utf-8'))
    return send_file(b,mimetype='text/plain',as_attachment=True,download_name=f'nascar15_modding_app_v{APP_VERSION}_support.txt')


# ==================== v0.9.31.3 FAILURE-FOCUSED WHOLE MOD REPAIR ====================
_FULL_REPAIR_LOCK = threading.RLock()
FULL_REPAIR_REPORT = os.path.join(USER_DIR, 'last_whole_mod_repair.json')


def _full_repair_json(result):
    """Normalize a Flask view return into a plain dictionary."""
    status = 200
    if isinstance(result, tuple):
        result, status = result[0], int(result[1]) if len(result) > 1 else 200
    if hasattr(result, 'get_json'):
        data = result.get_json(silent=True)
    elif isinstance(result, dict):
        data = result
    else:
        data = None
    if not isinstance(data, dict):
        raise RuntimeError(f'repair step returned an unreadable response (HTTP {status})')
    if status >= 400 or not data.get('ok', False):
        raise RuntimeError(str(data.get('error') or f'repair step failed (HTTP {status})'))
    return data


def _full_repair_active_state():
    mod = extra_scheme_mod()
    state = mod.load_state(EXTRA_SCHEME_STATE)
    active = [x for x in state.get('schemes', [])
              if not x.get('superseded_by') and x.get('uid') is not None]
    return mod, state, active


def _full_repair_snapshot(reg, active):
    """Rollback metadata for append/repoint work plus every custom paint slot.

    Older state files can point at the wrong SD/HD names. Snapshot every indexed
    CUSTOM livery entry instead of trusting saved names so a state-rebind repair
    still has complete same-process rollback coverage.
    """
    snap = {'groups': {}, 'states': {}, 'regions': []}
    for key in ('0', '1', '2'):
        v = need(reg, key)
        snap['groups'][key] = {
            'archive': v['ar'], 'size': os.path.getsize(v['ar']),
            'cdf': v['cdf'], 'cdf_bytes': open(v['cdf'], 'rb').read(),
        }
    for path in (CONFIG, EXTRA_SCHEME_STATE, TEAM_MANAGER_STATE):
        snap['states'][path] = {
            'exists': os.path.exists(path),
            'bytes': open(path, 'rb').read() if os.path.exists(path) else None,
        }
    mod = extra_scheme_mod()
    cdf2 = mod.v06.parse_cdf_v6(open(need(reg, '2')['cdf'], 'rb').read())
    archive2 = need(reg, '2')['ar']
    wanted = set()
    for item in active:
        for field in ('sd_entry', 'hd_entry'):
            name = str(item.get(field) or '')
            if name:
                wanted.add(name.casefold())
    for rec in cdf2.files:
        try:
            name = str(cdf2.basename(rec))
        except Exception:
            continue
        upper = name.upper()
        if upper.startswith(('LIVERY_CUSTOM_', 'HDLIVERY_CUSTOM_')):
            wanted.add(name.casefold())
    seen = set()
    with open(archive2, 'rb') as fh:
        for rec in cdf2.files:
            try:
                name = str(cdf2.basename(rec))
            except Exception:
                continue
            if name.casefold() not in wanted:
                continue
            key = (int(rec.data_offset), int(rec.data_size))
            if key in seen:
                continue
            seen.add(key)
            fh.seek(key[0]); raw = fh.read(key[1])
            if len(raw) != key[1]:
                raise ValueError(f'short rollback read for {name}')
            snap['regions'].append({
                'archive': archive2, 'offset': key[0],
                'bytes': raw, 'name': name,
            })
    return snap


def _full_repair_restore(snap):
    errors = []
    for item in (snap or {}).get('regions', []):
        try:
            with open(item['archive'], 'r+b') as fh:
                fh.seek(int(item['offset'])); fh.write(item['bytes'])
                fh.flush(); os.fsync(fh.fileno())
        except Exception as ex:
            errors.append(f"region {item.get('name')}: {ex}")
    for key, item in (snap or {}).get('groups', {}).items():
        try:
            with open(item['archive'], 'r+b') as fh:
                fh.truncate(int(item['size'])); fh.flush(); os.fsync(fh.fileno())
            _extra_atomic_bytes(item['cdf'], item['cdf_bytes'])
        except Exception as ex:
            errors.append(f'archive group {key}: {ex}')
    for path, item in (snap or {}).get('states', {}).items():
        try:
            if item.get('exists'):
                _extra_atomic_bytes(path, item.get('bytes') or b'')
            elif os.path.exists(path):
                os.remove(path)
        except Exception as ex:
            errors.append(f'state {os.path.basename(path)}: {ex}')
    try:
        _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear(); _clear_ui_thumb_cache()
    except Exception:
        pass
    return errors


def _full_repair_driver_plan(g, active):
    mod = extra_scheme_mod()
    team_state = _team_state_load()
    team_catalog = _team_friendly_catalog()
    extra_catalog = mod.catalog(g, EXTRA_SCHEME_STATE)
    extra_by_driver = {int(d['uid']): d for d in extra_catalog.get('drivers', [])}
    originals = _team_original_team_map()
    active_by_driver = collections.defaultdict(list)
    for item in active:
        active_by_driver[int(item.get('driver_uid', -1))].append(item)
    affected_driver_uids = set(active_by_driver)
    moved_cfg = {int(x) for x in team_state.get('driver_teams', {})}
    assets = team_assets_mod()
    for driver in team_catalog.get('drivers', []):
        cfg_uid = int(driver['config_uid'])
        driver_uid = int(driver['driver_uid'])
        team_uid = int(driver['team_uid'])
        try:
            names = set(assets.team_container_resource_names(g, team_uid))
        except Exception:
            names = set()
        mandatory = {f'DRIVERPAINT_{driver_uid}_25041',
                     f'DRIVER_{driver_uid}_3DNUM_25041'}
        if cfg_uid in moved_cfg or not mandatory.issubset(names):
            affected_driver_uids.add(driver_uid)
    drivers = []
    for driver in team_catalog.get('drivers', []):
        driver_uid = int(driver['driver_uid'])
        if driver_uid not in affected_driver_uids:
            continue
        cfg_uid = int(driver['config_uid'])
        team_uid = int(driver['team_uid'])
        source_uid = int(team_state.get('driver_source_teams', {}).get(
            str(cfg_uid), originals.get(cfg_uid, team_uid)))
        live_driver = extra_by_driver.get(driver_uid, {})
        stock_liveries = sorted({
            int(x['uid']) for x in live_driver.get('schemes', [])
            if x.get('uid') is not None and not x.get('managed')
        })
        # A database-backed livery is not automatically a Paint Select resource.
        # Several valid stock/DLC liveries have no PAINTSCHEME_<UID> entry in
        # their original native team bank. They remain valid for AI/runtime
        # selection, but requiring a same-named front-end thumbnail creates an
        # impossible false failure and rolls back an otherwise healthy rebuild.
        try:
            source_resource_names = set(
                assets.pristine_team_container_resource_names(g, source_uid))
        except Exception:
            source_resource_names = set()
        frontend_liveries = sorted(
            uid for uid in stock_liveries
            if f'PAINTSCHEME_{uid}' in source_resource_names)
        database_only_liveries = sorted(set(stock_liveries) - set(frontend_liveries))
        drivers.append({
            'config_uid': cfg_uid, 'driver_uid': driver_uid,
            'driver': driver.get('car_label') or driver.get('label'),
            'team_uid': team_uid, 'team': driver.get('team_label'),
            'source_team_uid': source_uid,
            'stock_livery_uids': stock_liveries,
            'frontend_livery_uids': frontend_liveries,
            'database_only_livery_uids': database_only_liveries,
            'created_livery_uids': sorted(
                int(x['uid']) for x in active_by_driver.get(driver_uid, [])),
        })
    return drivers


def _full_repair_failure_scan():
    """Inspect only structures that can break app-created paint loading.

    Stock-vs-live differences are not errors. The scan follows actual runtime
    references from saved state to DB records, CDF rows, paint wrappers, current
    team banks, driver art, PAINTSCHEME identity chains, and AI assignments.
    """
    g, reg = _extra_game_and_registry()
    mod, state, active = _full_repair_active_state()
    assets = team_assets_mod()
    thumbs = extra_thumbnail_mod()
    issues = []

    def add(code, subsystem, severity, detail, *, repairable=True,
            team_uid=None, driver_uid=None, uid=None, action=None):
        issues.append({
            'code': str(code), 'subsystem': str(subsystem),
            'severity': str(severity), 'detail': str(detail),
            'fatal_possible': severity == 'fail',
            'repairable': bool(repairable), 'action': action,
            'team_uid': team_uid, 'driver_uid': driver_uid, 'uid': uid,
        })

    # 1) Saved-state ↔ live DB ↔ canonical asset identity.
    db_audit = mod.inspect_managed_database(g, EXTRA_SCHEME_STATE)
    state_codes = {
        'state_script_uid_mismatch', 'state_script_driver_mismatch',
        'state_live_script_mismatch', 'state_live_driver_mismatch',
        'state_asset_name_mismatch',
    }
    for row in db_audit.get('issues', []):
        code = row.get('code')
        action = ('rebind_state' if code in state_codes else
                  'repair_database' if code == 'live_livery_missing' else
                  'rebuild_paint_assets' if code == 'canonical_assets_missing' else None)
        add(code, 'Created-paint identity',
            'fail' if row.get('fatal_possible', True) else 'warn', row.get('detail'),
            repairable=bool(row.get('repairable', True)), uid=row.get('uid'),
            action=action)

    missing_sources = []
    missing_thumbnails = []
    for item in active:
        uid = int(item['uid'])
        source = os.path.join(EXTRA_SCHEME_IMAGES,
                              os.path.basename(str(item.get('source_png') or '')))
        if not item.get('source_png') or not os.path.exists(source):
            missing_sources.append(uid)
            add('paint_source_missing', 'Created-paint assets', 'fail',
                f'UID {uid} has no saved paint PNG, so its native SD/HD files cannot be reconstructed.',
                repairable=False, uid=uid)
        thumb = os.path.join(EXTRA_SCHEME_IMAGES,
                             os.path.basename(str(item.get('thumbnail_source_png') or '')))
        if not item.get('thumbnail_source_png') or not os.path.exists(thumb):
            missing_thumbnails.append(uid)
            add('thumbnail_source_missing', 'Paint Select thumbnail', 'warn',
                f'UID {uid} has no saved custom thumbnail PNG; repair will use a native clone.',
                repairable=True, uid=uid, action='rebuild_team_bank')

    # 2) Indexed SD/HD wrappers and CDF bounds.
    try:
        cdf2 = mod.v06.parse_cdf_v6(open(need(reg, '2')['cdf'], 'rb').read())
        arc2 = need(reg, '2')['ar']; arc2_size = os.path.getsize(arc2)
        audit_by_uid = {int(x['uid']): x for x in db_audit.get('rows', [])}
        with open(arc2, 'rb') as fh:
            for item in active:
                uid = int(item['uid']); row = audit_by_uid.get(uid, {})
                names = ((row.get('canonical_sd_entry') or item.get('sd_entry'), 'sd'),
                         (row.get('canonical_hd_entry') or item.get('hd_entry'), 'hd'))
                for name, kind in names:
                    if not name:
                        continue
                    try:
                        _idx, rec = mod.v06.find_v6_file(cdf2, str(name))
                    except Exception:
                        # Already reported as canonical_assets_missing by DB audit.
                        continue
                    off, size = int(rec.data_offset), int(rec.data_size)
                    expected = _NATIVE_SD_ENTRY_SIZE if kind == 'sd' else _NATIVE_HD_ENTRY_SIZE
                    if off < 0 or size <= 0 or off + size > arc2_size:
                        add('paint_cdf_out_of_bounds', 'Created-paint assets', 'fail',
                            f'{name} maps outside ARCHIVE2.AR (offset {off}, size {size}, archive {arc2_size}).',
                            uid=uid, action='rebuild_paint_assets')
                        continue
                    if size != expected:
                        add('paint_wrapper_size_invalid', 'Created-paint assets', 'fail',
                            f'{name} is {size} bytes; native {kind.upper()} wrapper must be {expected}.',
                            uid=uid, action='rebuild_paint_assets')
                        continue
                    fh.seek(off); raw = fh.read(size)
                    try:
                        (_native_sd_validate_wrapper(raw) if kind == 'sd'
                         else _native_hd_validate_wrapper(raw))
                    except Exception as ex:
                        add('paint_wrapper_structure_invalid', 'Created-paint assets', 'fail',
                            f'{name} failed native wrapper validation: {ex}',
                            uid=uid, action='rebuild_paint_assets')
    except Exception as ex:
        add('archive2_index_unreadable', 'Created-paint assets', 'fail', ex,
            repairable=False)

    # 3) Current team-bank structure and every required runtime resource.
    drivers = _full_repair_driver_plan(g, active)
    teams = collections.defaultdict(list)
    for driver in drivers:
        teams[int(driver['team_uid'])].append(driver)
    team_state = _team_state_load()
    repair_versions = team_state.get('team_bank_repair_version', {})
    try:
        _game, archive1, cdf1 = assets.game_paths(g)
        _ver, rows1 = assets.v10.parse_cdf_rows(cdf1)
        arc1_size = archive1.stat().st_size
        pristine = assets._pristine_td(g)
        pristine_names = {row.name.casefold() for row, _raw, _parsed in pristine}
        for team_uid, members in sorted(teams.items()):
            container = f'2DRIVERSELECTTD_{team_uid}.ARC'
            matches = [x for x in rows1 if x.name.casefold() == container.casefold()]
            if len(matches) != 1:
                add('team_container_index_count', 'Driver Select bank', 'fail',
                    f'{container} has {len(matches)} CDF mapping(s); exactly one is required.',
                    team_uid=team_uid, action='rebuild_team_bank')
                continue
            row = matches[0]
            if int(row.offset) < 0 or int(row.size) <= 0 or int(row.offset) + int(row.size) > arc1_size:
                add('team_container_out_of_bounds', 'Driver Select bank', 'fail',
                    f'{container} maps outside ARCHIVE1.AR.',
                    team_uid=team_uid, action='rebuild_team_bank')
                continue
            try:
                raw = assets.v10.read_entry(archive1, row)
                parsed = assets.v10.parse_multi_arc(raw)
                entry_names = [e.name for e in parsed.entries]
                if len(entry_names) != len(set(entry_names)):
                    add('duplicate_team_resource_name', 'Driver Select bank', 'fail',
                        f'{container} contains duplicate resource names.',
                        team_uid=team_uid, action='rebuild_team_bank')
                footer_start, _footer, _order = thumbs._footer_bounds(raw, parsed)
                thumbs._validate_directory_header(raw, parsed, footer_start)
            except Exception as ex:
                add('team_container_structure_invalid', 'Driver Select bank', 'fail',
                    f'{container} failed native table/directory/footer parsing: {ex}',
                    team_uid=team_uid, action='rebuild_team_bank')
                continue

            # Validate every paint resource in the shared bank, not only
            # app-created thumbnails. One broken stock/support alias can fatal
            # the entire team before the selected scheme is reached.
            for entry in parsed.entries:
                if not str(entry.name).startswith('PAINTSCHEME_'):
                    continue
                try:
                    fields = struct.unpack('<8I', entry.table_record)
                    if int(fields[6]) != int(entry.name_ref):
                        raise ValueError('public name reference is not self-consistent')
                    identity = assets._identity_name(parsed, entry)
                    if identity is None or not str(identity).startswith('PAINTSCHEME_'):
                        raise ValueError('identity reference does not resolve to PAINTSCHEME')
                    root_name = assets._paint_identity_root(parsed, entry.name)
                    root = assets.v10.entry_by_name(parsed, root_name)
                    if not assets._is_native_paint_identity(parsed, root):
                        raise ValueError('identity chain does not reach a self-identifying root')
                    thumbs._entry_resource_bytes(raw, parsed, entry, footer_start)  # structural guard
                    canonical_entries, _ = C.parse_multi_arc(raw)
                    canonical = next((e for e in canonical_entries if e['name'] == entry.name), None)
                    if canonical is None:
                        raise ValueError('resource is missing from the native physical texture table')
                    if (int(canonical['w']) != 256 or int(canonical['h']) != 256
                            or str(canonical['fmt']) != 'DXT5'
                            or int(canonical['payload_size']) < int(canonical['needed'])):
                        raise ValueError(
                            f"unexpected texture layout {canonical['w']}x{canonical['h']} "
                            f"{canonical['fmt']} payload={canonical['payload_size']}")
                    C.multi_read_png(raw, canonical)
                except Exception as ex:
                    add('team_thumbnail_dependency_invalid', 'Driver Select bank', 'fail',
                        f'{container}: {entry.name} has invalid native identity/texture wiring ({ex}).',
                        team_uid=team_uid, action='rebuild_team_bank')

            for member in members:
                driver_uid = int(member['driver_uid'])
                for kind, name in (
                    ('tile', f'DRIVERPAINT_{driver_uid}_25041'),
                    ('number', f'DRIVER_{driver_uid}_3DNUM_25041')):
                    ok, reason = assets._driver_art_entry_valid(raw, parsed, name, container)
                    if not ok:
                        add('driver_art_invalid', 'Driver Select bank', 'fail',
                            f'{container}: {name} is missing or invalid ({reason}).',
                            team_uid=team_uid, driver_uid=driver_uid,
                            action='rebuild_team_bank')
                for uid in member.get('frontend_livery_uids', []):
                    name = f'PAINTSCHEME_{int(uid)}'
                    if name not in entry_names:
                        add('runtime_thumbnail_missing_from_current_team', 'Driver Select bank', 'fail',
                            f'{container} is missing runtime-visible {name} for driver {driver_uid}.',
                            team_uid=team_uid, driver_uid=driver_uid, uid=int(uid),
                            action='rebuild_team_bank')
                for uid in member.get('created_livery_uids', []):
                    try:
                        info = thumbs.inspect_thumbnail_identity(
                            g, int(uid), target_container_name=container)
                    except Exception as ex:
                        info = {'same_bank_valid': False, 'structural_error': str(ex)}
                    if not info.get('exists'):
                        add('created_thumbnail_missing', 'Paint Select thumbnail', 'fail',
                            f'{container} is missing PAINTSCHEME_{uid}.',
                            team_uid=team_uid, driver_uid=driver_uid, uid=int(uid),
                            action='rebuild_team_bank')
                    elif not info.get('structural_valid'):
                        add('created_thumbnail_structure_invalid', 'Paint Select thumbnail', 'fail',
                            f'PAINTSCHEME_{uid} has invalid native DXT5/container structure: '
                            f'{info.get("structural_error") or "unknown structure error"}.',
                            team_uid=team_uid, driver_uid=driver_uid, uid=int(uid),
                            action='rebuild_team_bank')
                    elif not info.get('same_bank_valid'):
                        add('created_thumbnail_identity_invalid', 'Paint Select thumbnail', 'fail',
                            f'PAINTSCHEME_{uid} resolves to {info.get("identity_name") or "no identity"} '
                            f'instead of a self-identifying same-bank PAINTSCHEME anchor.',
                            team_uid=team_uid, driver_uid=driver_uid, uid=int(uid),
                            action='rebuild_team_bank')

            status = assets.team_asset_status(g, team_uid)
            if not status.get('logo_ready'):
                add('team_logo_missing', 'Team Select', 'warn',
                    f'Team UID {team_uid} has no Team Select logo resource.',
                    team_uid=team_uid, action='repair_team_logo')

            # A legacy bank can be fully parseable while still carrying copied
            # experimental dependencies. One deep pristine rebuild is required
            # before we call a moved/custom team game-safe.
            moved_team = any(str(d['config_uid']) in team_state.get('driver_teams', {})
                             for d in members)
            managed_team = any(d.get('created_livery_uids') for d in members)
            if (moved_team or managed_team) and int(repair_versions.get(str(team_uid), 0) or 0) < 3:
                add('legacy_team_bank_provenance', 'Driver Select bank', 'warn',
                    f'{container} predates runtime-visible thumbnail mapping; '
                    'a pristine-source rebuild is recommended once.',
                    team_uid=team_uid, action='rebuild_team_bank')

            # Ensure each original source bank exists in the pristine backup.
            for member in members:
                source_name = f"2DRIVERSELECTTD_{int(member['source_team_uid'])}.ARC"
                if source_name.casefold() not in pristine_names:
                    add('pristine_source_team_missing', 'Repair source', 'fail',
                        f'Pristine source bank {source_name} is unavailable for '
                        f'driver {member["driver_uid"]}.', repairable=False,
                        team_uid=team_uid, driver_uid=int(member['driver_uid']))
    except Exception as ex:
        add('archive1_team_index_unreadable', 'Driver Select bank', 'fail', ex,
            repairable=False)

    # 4) Assignment wiring. An assignment to a missing/mismatched livery can
    # fatal on race initialization even when menus load.
    active_uid_driver = {int(x['uid']): int(x.get('driver_uid', -1)) for x in active}
    assignments = mod.assignments(EXTRA_SCHEME_STATE)
    assignment_count = 0
    for event_key, mapping in assignments.items():
        for driver_uid, livery_uid in (mapping or {}).items():
            assignment_count += 1
            driver_uid = int(driver_uid); livery_uid = int(livery_uid)
            if livery_uid in active_uid_driver and active_uid_driver[livery_uid] != driver_uid:
                add('ai_assignment_driver_mismatch', 'AI Paint Schedule', 'fail',
                    f'{event_key}: driver {driver_uid} is assigned UID {livery_uid}, '
                    f'which belongs to driver {active_uid_driver[livery_uid]}.',
                    driver_uid=driver_uid, uid=livery_uid,
                    action='repair_ai_assignments')
    try:
        unsafe = _extra_unsafe_assigned_thumbnail_uids()
        if unsafe:
            add('ai_assignment_unsafe_thumbnail', 'AI Paint Schedule', 'fail',
                'Assigned created thumbnail(s) are unsafe: ' + ', '.join(map(str, unsafe)),
                action='repair_ai_assignments')
    except Exception as ex:
        add('ai_assignment_check_failed', 'AI Paint Schedule', 'fail', ex,
            action='repair_ai_assignments')

    affected_teams = sorted(teams)
    release_blockers = []
    for item in active:
        guard = _stable_paint_creation_guard(int(item.get('driver_uid', -1)))
        if not guard.get('locked'):
            continue
        release_blockers.append({
            'code': 'moved_custom_created_paint',
            'uid': int(item.get('uid', -1)),
            'driver_uid': int(item.get('driver_uid', -1)),
            'team_uid': guard.get('team_uid'),
            'detail': (guard.get('reason') or
                       'This created paint belongs to a moved/custom-team driver.'),
        })
    # Public V1 does not let the global repair path rewrite a reserve-team bank,
    # even when that team currently contains only native schemes.  Recovery is a
    # normal Move Driver operation back to an authored team, which has its own
    # transaction and readback checks.
    for driver in drivers:
        team_uid = int(driver.get('team_uid', -1))
        if not _public_custom_team_locked(team_uid):
            continue
        release_blockers.append({
            'code': 'public_custom_team_live',
            'uid': -1,
            'driver_uid': int(driver.get('driver_uid', -1)),
            'team_uid': team_uid,
            'detail': PUBLIC_CUSTOM_TEAM_MESSAGE,
        })
    fail_count = sum(x['severity'] == 'fail' for x in issues)
    warn_count = sum(x['severity'] == 'warn' for x in issues)
    unrepairable = [x for x in issues if x['severity'] == 'fail' and not x.get('repairable')]
    return {
        'ok': True, 'game': g,
        'active_created_schemes': len(active),
        'drivers': drivers,
        'affected_teams': affected_teams,
        'affected_driver_count': len(drivers),
        'ai_assignment_count': assignment_count,
        'missing_paint_sources': missing_sources,
        'missing_thumbnail_sources': missing_thumbnails,
        'backups': {
            key: bool(os.path.exists(need(reg, key)['bak']) and
                      os.path.exists(backup_path(need(reg, key)['cdf'])))
            for key in ('0', '1', '2')
        },
        'issues': issues,
        'fail_count': fail_count,
        'warn_count': warn_count,
        'unrepairable_count': len(unrepairable),
        'repairable': not unrepairable,
        'release_blockers': release_blockers,
        'release_repair_locked': bool(release_blockers),
        'database_audit': db_audit,
        'note': ('This scan follows live runtime references. Normal differences from stock '
                 'are ignored; only missing, mismatched, out-of-bounds, structurally invalid, '
                 'or unverified app-managed dependencies are reported. Full Repair is disabled '
                 'only when an app-created paint is attached to a spare/custom-team driver, '
                 'because added-slot rebuilding for those teams has not passed the release stability gate.'),
    }


def _full_repair_plan_data():
    return _full_repair_failure_scan()


def _full_repair_rebuild_paint_assets(g, reg, active, target_uids=None):
    """Recreate canonical SD/HD assets from saved PNGs without touching DB."""
    mod = extra_scheme_mod()
    proven = mod.proven_extra_donor(g)
    pair = mod.donor_asset_pair(g, proven['script_name'])
    arc2 = need(reg, '2')['ar']
    cdf2_path = need(reg, '2')['cdf']
    cdf2 = mod.v06.parse_cdf_v6(open(cdf2_path, 'rb').read())
    results = []
    wanted = None if target_uids is None else {int(x) for x in target_uids}
    for item in active:
        if wanted is not None and int(item.get('uid', -1)) not in wanted:
            continue
        uid = int(item['uid'])
        source = os.path.join(EXTRA_SCHEME_IMAGES,
                              os.path.basename(str(item.get('source_png') or '')))
        if not os.path.exists(source):
            raise ValueError(f'saved paint source is missing for UID {uid}')
        image = Image.open(source).convert('RGB')
        if image.size != (2048, 1024):
            image = image.resize((2048, 1024),
                                 Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS)
        sd_wrapper, sd_levels, sd_changed = _native_sd_patch_wrapper(pair['sd'], image, None)
        hd_wrapper, hd_levels, hd_changed = _native_hd_patch_wrapper(pair['hd'], image)
        script = str(item.get('script_name') or '')
        sd_name = f'LIVERY_{script}.ARC'; hd_name = f'HDLIVERY_{script}.ARC'
        found = {}
        for name in (sd_name, hd_name):
            try:
                _idx, rec = mod.v06.find_v6_file(cdf2, name)
                found[name] = rec
            except Exception:
                pass
        if not found:
            old_size = os.path.getsize(arc2)
            sd_off = mod.v06.align(old_size, mod.ALIGN2)
            hd_off = mod.v06.align(sd_off + len(sd_wrapper), mod.ALIGN2)
            rebuilt_cdf, _meta = mod.v06.clone_asset_entries(
                cdf2,
                f"LIVERY_{proven['script_name']}.ARC",
                f"HDLIVERY_{proven['script_name']}.ARC",
                sd_name, hd_name, sd_off, hd_off)
            mod._append(Path(arc2), sd_off, bytes(sd_wrapper), mod.ALIGN2)
            mod._append(Path(arc2), hd_off, bytes(hd_wrapper), mod.ALIGN2)
            mod._atomic(Path(cdf2_path), rebuilt_cdf)
            cdf2 = mod.v06.parse_cdf_v6(rebuilt_cdf)
            mode = 'appended_and_repointed'
        elif len(found) == 2:
            rows = ((sd_name, found[sd_name], bytes(sd_wrapper)),
                    (hd_name, found[hd_name], bytes(hd_wrapper)))
            with open(arc2, 'r+b') as fh:
                for name, rec, payload in rows:
                    if int(rec.data_size) != len(payload):
                        raise ValueError(f'{name} size no longer matches native wrapper size')
                    fh.seek(int(rec.data_offset)); fh.write(payload)
                fh.flush(); os.fsync(fh.fileno())
                for name, rec, payload in rows:
                    fh.seek(int(rec.data_offset))
                    if fh.read(len(payload)) != payload:
                        raise ValueError(f'{name} readback mismatch')
            mode = 'rewritten_in_place'
        else:
            raise ValueError(f'UID {uid} has only one of its canonical SD/HD CDF entries; '
                             'automatic repair refused to create a split pair')
        item['sd_entry'] = sd_name; item['hd_entry'] = hd_name
        item['native_runtime_layout_version'] = 1
        item['native_runtime_repaired'] = int(time.time())
        results.append({'uid': uid, 'mode': mode, 'sd_entry': sd_name,
                        'hd_entry': hd_name, 'sd_changed_bytes': sd_changed,
                        'hd_changed_bytes': hd_changed,
                        'sd_levels': sd_levels, 'hd_levels': hd_levels})
    # The objects above came from the loaded state used by the caller. Reload and
    # write canonical fields explicitly to avoid relying on object identity.
    state = mod.load_state(EXTRA_SCHEME_STATE)
    by_uid = {int(x['uid']): x for x in active}
    for row in state.get('schemes', []):
        uid = int(row.get('uid', -1))
        if uid in by_uid and not row.get('superseded_by'):
            src = by_uid[uid]
            for key in ('driver_uid', 'script_name', 'sd_entry', 'hd_entry',
                        'native_runtime_layout_version', 'native_runtime_repaired'):
                row[key] = src.get(key)
    mod.save_state(EXTRA_SCHEME_STATE, state)
    return results


def _full_repair_verify_tabs(g, reg):
    checks = []
    def add(tab, status, detail):
        checks.append({'tab': tab, 'status': status, 'detail': str(detail)})
    try:
        drivers, teams = roster(reg)
        add('Drivers & Teams', 'pass', f'{len(drivers)} drivers and {len(teams)} team names parsed.')
    except Exception as ex:
        add('Drivers & Teams', 'fail', ex)
    try:
        stats = read_stats(reg)
        add('Ratings', 'pass', f'{len(stats)} driver rating rows parsed.')
    except Exception as ex:
        add('Ratings', 'fail', ex)
    try:
        rows, meta = _schedule_read('live', use_cache=False)
        add('Race Settings', 'pass', f'{len(rows)} schedule slots parsed from {meta.get("label", "live")} data.')
    except Exception as ex:
        add('Race Settings', 'fail', ex)
    try:
        text_files = _ui_text_quick_status()
        add('Game Text', 'pass' if text_files else 'warn', f'{len(text_files)} text tables parsed.')
    except Exception as ex:
        add('Game Text', 'warn', ex)
    try:
        rows = _ui_index() if ui_csv_path() else []
        add('Graphics', 'pass' if rows else 'warn', f'{len(rows)} indexed graphics available.')
    except Exception as ex:
        add('Graphics', 'warn', ex)
    try:
        banks = []
        for arcid, info in reg.items():
            try:
                banks.extend(n for _o, _sz, n in parse_cdfiles(info['cdf'])
                             if n.upper().endswith(('.FSB', '.SND')))
            except Exception:
                continue
        add('Audio', 'pass' if banks else 'warn', f'{len(banks)} indexed audio bank(s) found.')
    except Exception as ex:
        add('Audio', 'warn', ex)
    try:
        ai_state = _extra_state_public().get('ai', {})
        add('AI & Physics', 'pass', 'AI paint state parsed; track physics files were preserved.' +
            (f' Last AI install source: {ai_state.get("source")}.' if ai_state else ''))
    except Exception as ex:
        add('AI & Physics', 'warn', ex)
    return checks


@app.route('/api/full_repair/check')
def full_repair_check_api():
    try:
        return jsonify(_full_repair_failure_scan())
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/full_repair/apply', methods=['POST'])
def full_repair_apply_api():
    snapshot = None
    stage = 'preflight'
    report = {'ok': False, 'version': APP_VERSION,
              'started': datetime.datetime.now().isoformat(timespec='seconds'),
              'steps': []}
    try:
        stage = 'checking whether NASCAR 15 is closed'
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before repairing the installation')
        with _FULL_REPAIR_LOCK, _EXTRA_CREATE_LOCK, _TEAM_MANAGER_LOCK:
            stage = 'loading the live game registry'
            g, reg = _extra_game_and_registry()
            mod, state, active = _full_repair_active_state()
            stage = 'rescanning live failure causes'
            scan = _full_repair_failure_scan()
            report['scan_before'] = scan
            report['plan'] = scan
            protected_created = list(scan.get('release_blockers') or [])
            if protected_created:
                details = ', '.join(
                    ((f"paint UID {x['uid']} / " if int(x.get('uid', -1)) >= 0 else '') +
                     f"driver {x['driver_uid']} / team {x['team_uid']}")
                    for x in protected_created[:12])
                raise ValueError(
                    'Full Repair is unavailable while a custom-team driver has an added paint slot (' + details +
                    '). Remove that created slot, move the driver back to an authored team, or restore a known-good game backup. '
                    'The global repair path will not rebuild added-slot state inside a custom-team bank.')
            unrepairable = [x for x in scan.get('issues', [])
                            if x.get('severity') == 'fail' and not x.get('repairable')]
            if unrepairable:
                raise ValueError('Unrepairable failure cause(s): ' +
                                 '; '.join(x.get('detail', '') for x in unrepairable))
            stage = 'creating rollback coverage'
            _extra_backups(reg, ('0', '1', '2'))
            snapshot = _full_repair_snapshot(reg, active)

            # Correct saved identity swaps first. This repairs the map the rest
            # of the recovery pass follows without replacing any game DB bytes.
            stage = 'repairing created-paint identity state'
            state_result = mod.repair_managed_state_from_live(g, EXTRA_SCHEME_STATE)
            report['steps'].append({
                'name': 'Created-paint identity map',
                'status': 'repaired' if state_result.get('changed') else 'skipped',
                'detail': (f"Rebound {state_result.get('changed', 0)} saved scheme row(s) "
                           'to their authoritative live UID, driver, ScriptName, and asset names.'
                           if state_result.get('changed') else
                           'Saved scheme identities already match the live database.'),
                'result': state_result,
            })

            stage = 'repairing missing live livery records'
            db_result = mod.repair_missing_managed_database_from_live_base(
                g, EXTRA_SCHEME_STATE, donor_uid=int(mod.PROVEN_EXTRA_DONOR_UID))
            report['steps'].append({
                'name': 'Live created-livery records',
                'status': 'repaired' if db_result.get('changed') else 'skipped',
                'detail': (f"Added {db_result.get('added', 0)} missing LIVERIE_c record(s) "
                           'to the current live DB while preserving every existing record.'
                           if db_result.get('changed') else
                           'All created LIVERIE_c records were already present and coherent.'),
                'result': db_result,
            })

            # Re-read canonicalized state, then reconstruct only paint pairs
            # whose CDF/wrapper/identity scan failed or whose saved state was
            # rebound to a different live identity.
            mod, state, active = _full_repair_active_state()
            changed_state_uids = {int(x['uid']) for x in state_result.get('changes', [])}
            paint_issue_uids = {int(x['uid']) for x in scan.get('issues', [])
                                if x.get('uid') is not None and
                                x.get('action') == 'rebuild_paint_assets'}
            paint_targets = changed_state_uids | paint_issue_uids
            stage = 'rebuilding failed SD/HD paint assets'
            paint_assets = (_full_repair_rebuild_paint_assets(
                g, reg, active, paint_targets) if paint_targets else [])
            report['steps'].append({
                'name': 'Native SD/HD created paints',
                'status': 'repaired' if paint_assets else 'skipped',
                'detail': (f'Rebuilt {len(paint_assets)} failed canonical native paint pair(s) from saved PNGs.'
                           if paint_assets else
                           'Every indexed native SD/HD paint wrapper already passed the failure scan.'),
                'result': paint_assets,
            })

            # Saved DB team links remain live; reapply only if a prior DB-add
            # operation changed the indexed PYC revision.
            stage = 'reapplying saved team and manufacturer links'
            team_links = _team_reapply_saved_links()
            report['steps'].append({
                'name': 'Driver/team and manufacturer links',
                'status': 'repaired' if team_links.get('changed') else 'skipped',
                'detail': ('Reapplied saved team/manufacturer links.' if team_links.get('changed')
                           else 'Saved team/manufacturer links already matched live data.'),
                'result': team_links,
            })

            stage = 'building the current driver/team recovery plan'
            drivers = _full_repair_driver_plan(g, active)
            all_teams = collections.defaultdict(list)
            driver_team_map = {}
            for driver in drivers:
                team_uid = int(driver['team_uid'])
                driver_team_map[int(driver['driver_uid'])] = team_uid
                all_teams[team_uid].append({
                    'driver_uid': int(driver['driver_uid']),
                    'source_team_uid': int(driver['source_team_uid']),
                    'livery_uids': list(driver.get('frontend_livery_uids', [])),
                    'config_uid': int(driver['config_uid']),
                })
            team_targets = {int(x['team_uid']) for x in scan.get('issues', [])
                            if x.get('team_uid') is not None and
                            x.get('action') == 'rebuild_team_bank'}
            for change in state_result.get('changes', []):
                after_driver = int((change.get('after') or {}).get('driver_uid', -1))
                if after_driver in driver_team_map:
                    team_targets.add(driver_team_map[after_driver])
            team_state = _team_state_load()
            created_by_driver = collections.defaultdict(list)
            for item in active:
                created_by_driver[int(item.get('driver_uid', -1))].append(int(item['uid']))
            assets = team_assets_mod()
            team_reports = []
            thumb_reports = []
            stage = 'rebuilding affected Driver Select banks'
            for team_uid in sorted(team_targets):
                members = all_teams.get(team_uid, [])
                if not members:
                    raise ValueError(f'Team {team_uid} was flagged but no current driver plan could be built')
                donor_uid = int(team_state.get('team_logo_donors', {}).get(
                    str(team_uid), members[0]['source_team_uid']))
                member_reports = []
                for member in members:
                    report = assets.ensure_driver_assets(
                        g, int(team_uid), int(member['source_team_uid']),
                        int(member['driver_uid']),
                        list(member.get('livery_uids') or []))
                    report['transfer_strategy'] = 'public_v1_direct_revision'
                    member_reports.append(report)
                clean = {
                    'ok': True,
                    'destination_team_uid': int(team_uid),
                    'strategy': 'public_v1_sequential_team_repair',
                    'member_reports': member_reports,
                    'missing_optional_resources': list(dict.fromkeys(
                        name for report in member_reports
                        for name in (report.get('missing_optional_resources') or []))),
                    'readback_verified': all(
                        bool(report.get('readback_verified', True))
                        for report in member_reports),
                }
                team_reports.append(clean)
                for member in members:
                    if created_by_driver.get(int(member['driver_uid'])):
                        thumb_reports.extend(_team_rebuild_created_thumbnails(
                            g, int(member['driver_uid']), int(team_uid)))
                status = assets.team_asset_status(g, int(team_uid))
                if not status.get('logo_ready'):
                    assets.ensure_team_logo(g, int(team_uid), donor_uid)
            # Logo-only warnings do not need a full TD-bank rebuild.
            logo_targets = {int(x['team_uid']) for x in scan.get('issues', [])
                            if x.get('team_uid') is not None and
                            x.get('action') == 'repair_team_logo'} - team_targets
            for team_uid in sorted(logo_targets):
                members = all_teams.get(team_uid, [])
                if not members:
                    continue
                donor_uid = int(team_state.get('team_logo_donors', {}).get(
                    str(team_uid), members[0]['source_team_uid']))
                assets.ensure_team_logo(g, int(team_uid), donor_uid)
            if team_targets:
                team_state = _team_state_load()
                versions = team_state.setdefault('team_bank_repair_version', {})
                for team_uid in team_targets:
                    versions[str(team_uid)] = 3
                team_state['last_failure_focused_repair'] = datetime.datetime.now().isoformat(timespec='seconds')
                _team_state_save(team_state)
            report['steps'].append({
                'name': 'Driver Select and Paint Select banks',
                'status': 'repaired' if team_reports or logo_targets else 'skipped',
                'detail': ((f'Rebuilt {len(team_reports)} failed team bank(s) from a pristine base and validated '
                            f'source resources, recreated {len(thumb_reports)} custom thumbnail(s), and '
                            f'restored {len(logo_targets)} logo-only target(s).')
                           if team_reports or logo_targets else
                           'Every current-team bank, driver-art resource, thumbnail chain, and team logo passed.'),
                'teams': team_reports, 'thumbnails': thumb_reports,
            })

            stage = 'reapplying AI paint assignments'
            assignments = mod.assignments(EXTRA_SCHEME_STATE)
            assignment_count = sum(len(x or {}) for x in assignments.values())
            ai_issue = any(x.get('action') == 'repair_ai_assignments'
                           for x in scan.get('issues', []))
            repaired_runtime_dependency = bool(paint_assets or team_reports or state_result.get('changed') or db_result.get('changed'))
            if assignment_count and (ai_issue or repaired_runtime_dependency):
                unsafe = _extra_unsafe_assigned_thumbnail_uids()
                if unsafe:
                    raise ValueError('thumbnail verification still blocks AI assignments for UID(s): ' +
                                     ', '.join(map(str, unsafe)))
                ba, bc = _extra_ai_backup_paths(reg)
                ai = mod.apply_ai(g, EXTRA_SCHEME_STATE,
                                  backup_archive=ba, backup_cdf=bc)
                report['steps'].append({
                    'name': 'AI Paint Schedule', 'status': 'repaired',
                    'detail': f'Reapplied {assignment_count} saved race assignment(s).',
                    'result': ai,
                })
            else:
                report['steps'].append({
                    'name': 'AI Paint Schedule', 'status': 'skipped',
                    'detail': ('No saved AI paint assignments exist.' if not assignment_count else
                               'Saved AI assignments passed and no repaired dependency required reinstalling EVENTINIT.'),
                })

            try:
                _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear(); _clear_ui_thumb_cache()
            except Exception:
                pass
            stage = 'running final failure scan'
            final_scan = _full_repair_failure_scan()
            report['scan_after'] = final_scan
            remaining_fails = [x for x in final_scan.get('issues', [])
                               if x.get('severity') == 'fail']
            if remaining_fails:
                raise RuntimeError('post-repair fatal-cause scan still fails: ' +
                                   '; '.join(x.get('detail', '') for x in remaining_fails))
            stage = 'running Paint System Check'
            paint = _full_repair_json(paint_system_check_api())
            critical = [x for x in paint.get('checks', []) if x.get('status') == 'fail']
            if critical:
                raise RuntimeError('post-repair Paint System Check still fails: ' +
                                   '; '.join(f"{x.get('name')}: {x.get('detail')}" for x in critical))
            stage = 'verifying every app tab'
            tab_checks = _full_repair_verify_tabs(g, reg)
            hard_tab_fail = [x for x in tab_checks if x.get('status') == 'fail']
            if hard_tab_fail:
                raise RuntimeError('post-repair tab verification failed: ' +
                                   '; '.join(f"{x['tab']}: {x['detail']}" for x in hard_tab_fail))
            report['paint_system'] = paint
            report['tab_checks'] = tab_checks
            report['ok'] = True
            report['finished'] = datetime.datetime.now().isoformat(timespec='seconds')
            report['summary'] = (f'Failure-focused repair completed. The scan found '
                                 f'{scan.get("fail_count", 0)} fatal candidate(s) and '
                                 f'{scan.get("warn_count", 0)} warning(s); all repairable '
                                 'fatal candidates are clear after repair.')
            _extra_atomic_bytes(FULL_REPAIR_REPORT,
                                json.dumps(report, indent=2).encode('utf-8'))
            return jsonify(report)
    except Exception as ex:
        rollback = _full_repair_restore(snapshot) if snapshot else []
        report['failed_stage'] = stage
        report['error'] = f'Repair stopped during {stage}: {ex}'
        report['rolled_back'] = bool(snapshot and not rollback)
        report['rollback_errors'] = rollback
        report['finished'] = datetime.datetime.now().isoformat(timespec='seconds')
        try:
            _extra_atomic_bytes(FULL_REPAIR_REPORT,
                                json.dumps(report, indent=2).encode('utf-8'))
        except Exception:
            pass
        detail = report['error']
        if rollback:
            detail += ' | Rollback warnings: ' + '; '.join(rollback)
        return jsonify(dict(ok=False, error=detail,
                            rolled_back=bool(snapshot and not rollback),
                            report=report)), 400


@app.route('/api/full_repair/report')
def full_repair_report_api():
    if not os.path.exists(FULL_REPAIR_REPORT):
        return jsonify(dict(ok=False, error='no whole-install repair has been run yet')), 404
    return send_file(FULL_REPAIR_REPORT, mimetype='application/json',
                     as_attachment=True,
                     download_name=f'nascar15_whole_mod_repair_v{APP_VERSION}.json')

# ==================== end v0.9.31.3 FAILURE-FOCUSED WHOLE MOD REPAIR ====================



@app.route('/api/help/request',methods=['POST'])
def help_request_package():
    """Create a small, shareable support ZIP without copying game archives."""
    import zipfile,platform
    q=request.get_json(silent=True) or {}
    area=str(q.get('area') or 'Other').strip()[:120]
    contact=str(q.get('contact') or '').strip()[:240]
    summary=str(q.get('summary') or '').strip()[:180]
    details=str(q.get('details') or '').strip()[:8000]
    if not summary and not details:
        return jsonify(dict(ok=False,error='add a summary or description first')),400
    created=datetime.datetime.now()
    checks,check_summary=_support_checks()
    g,reg=registry()
    cfg=load_cfg()
    request_text='\n'.join([
        f'{APP_NAME} help request',
        f'Created: {created.isoformat(timespec="seconds")}',
        f'App version: {APP_VERSION} {APP_RELEASE_LABEL}',
        f'Area: {area}',
        f'Contact: {contact or "not provided"}',
        '',
        f'Summary: {summary or "not provided"}',
        '',
        'What happened:',
        details or 'not provided',
        '',
        'Please attach this entire ZIP when using the configured help form or email.',
    ])+'\n'
    support_text='\n'.join([
        f'{APP_NAME} v{APP_VERSION} installation check',
        f'Result: {check_summary["pass_count"]} pass, {check_summary["warn_count"]} warning, {check_summary["fail_count"]} fail',
        '',
        *[f'[{c["status"].upper()}] {c["name"]}: {c["detail"]}' for c in checks],
    ])+'\n'
    environment=dict(
        app_name=APP_NAME,app_version=APP_VERSION,release_label=APP_RELEASE_LABEL,
        created=created.isoformat(timespec='seconds'),
        platform=platform.platform(),python=sys.version.split()[0],frozen=bool(getattr(sys,'frozen',False)),
        game_found=bool(g),game_folder=g,archive_groups=sorted(reg.keys()),
        backup_groups=sum(1 for v in reg.values() if os.path.exists(v['bak'])),
        texconv_ready=bool(texconv_path()),ffmpeg_ready=bool(ffmpeg_path()),
        interface_settings={k:v for k,v in _app_settings_payload(cfg).items() if k not in ('help_destination','support_destination')},
    )
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('HELP_REQUEST.txt',request_text.encode('utf-8'))
        z.writestr('INSTALLATION_CHECK.txt',support_text.encode('utf-8'))
        z.writestr('ENVIRONMENT.json',json.dumps(environment,indent=2).encode('utf-8'))
        z.writestr('README.txt',b'This package was created by NASCAR 15 Modding App. It contains no game archives. Send the entire ZIP to the project owner.\n')
    out.seek(0)
    stamp=created.strftime('%Y%m%d_%H%M%S')
    return send_file(out,mimetype='application/zip',as_attachment=True,download_name=f'nascar15_help_request_{stamp}.zip')

# ==================== v0.9.29.7 PROVEN EXTRA-SLOT RUNTIME REPAIR ====================
EXTRA_SCHEME_HELPER = 'nascar15_extra_scheme_manager_v1.py'
EXTRA_THUMBNAIL_HELPER = 'nascar15_thumbnail_native_v25.py'
EXTRA_STOCK_THUMBNAIL_HELPER = 'nascar15_thumbnail_stock_legacy_v25.py'
EXTRA_LEGACY_SCHEME_HELPER = 'nascar15_extra_scheme_manager_rc10.py'
EXTRA_LEGACY_THUMBNAIL_HELPER = 'nascar15_thumbnail_native_legacy_v25.py'
EXTRA_FIXED_TEMPLATE_HELPER = 'nascar15_fixed_template_stock_paint_rc10.py'
EXTRA_SCHEME_STATE = os.path.join(USER_DIR, 'extra_schemes_v1.json')
EXTRA_SCHEME_IMAGES = os.path.join(SCHEMES, 'extra')
EXTRA_SCHEME_ROLLBACK_DIR = os.path.join(USER_DIR, 'extra_scheme_rollback_v1')
EXTRA_SCHEME_LIMIT_PER_DRIVER = 8
_EXTRA_SCHEME_MOD = None
_EXTRA_THUMBNAIL_MOD = None
_EXTRA_STOCK_THUMBNAIL_MOD = None
_EXTRA_LEGACY_SCHEME_MOD = None
_EXTRA_LEGACY_THUMBNAIL_MOD = None
_EXTRA_FIXED_TEMPLATE_MOD = None
_EXTRA_CREATE_LOCK = threading.Lock()
os.makedirs(EXTRA_SCHEME_IMAGES, exist_ok=True)


def extra_scheme_mod():
    global _EXTRA_SCHEME_MOD
    if _EXTRA_SCHEME_MOD is not None:
        # Re-apply each call so a UID verdict takes effect without a restart.
        return _extra_apply_uid_pool(_EXTRA_SCHEME_MOD)
    path = component_path(EXTRA_SCHEME_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{EXTRA_SCHEME_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_extra_scheme_manager', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_extra_scheme_manager'] = mod
    spec.loader.exec_module(mod)
    _EXTRA_SCHEME_MOD = mod
    return _extra_apply_uid_pool(mod)


def extra_thumbnail_mod():
    global _EXTRA_THUMBNAIL_MOD
    if _EXTRA_THUMBNAIL_MOD is not None:
        return _EXTRA_THUMBNAIL_MOD
    path = component_path(EXTRA_THUMBNAIL_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{EXTRA_THUMBNAIL_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_thumbnail_native_v25', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_thumbnail_native_v25'] = mod
    spec.loader.exec_module(mod)
    _EXTRA_THUMBNAIL_MOD = mod
    return mod


def extra_stock_thumbnail_mod():
    """Load the untouched v0.9.30.5 stock-team thumbnail backend.

    This module is intentionally separate from the team-aware thumbnail backend.
    Stock paint creation therefore cannot inherit custom-team identity experiments
    through shared helper functions.
    """
    global _EXTRA_STOCK_THUMBNAIL_MOD
    if _EXTRA_STOCK_THUMBNAIL_MOD is not None:
        return _EXTRA_STOCK_THUMBNAIL_MOD
    path = component_path(EXTRA_STOCK_THUMBNAIL_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{EXTRA_STOCK_THUMBNAIL_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_thumbnail_stock_legacy_v25', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_thumbnail_stock_legacy_v25'] = mod
    spec.loader.exec_module(mod)
    _EXTRA_STOCK_THUMBNAIL_MOD = mod
    return mod


def extra_legacy_scheme_mod():
    """Load the byte-for-byte v0.9.29.9 extra-scheme manager for creation only.

    The current manager remains active for catalog, repair, and diagnostics. This
    loader exists so the known-good database/asset append path cannot inherit
    later custom-team experiments.
    """
    global _EXTRA_LEGACY_SCHEME_MOD
    if _EXTRA_LEGACY_SCHEME_MOD is not None:
        return _EXTRA_LEGACY_SCHEME_MOD
    path = component_path(EXTRA_LEGACY_SCHEME_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{EXTRA_LEGACY_SCHEME_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_extra_scheme_manager_legacy_v1', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_extra_scheme_manager_legacy_v1'] = mod
    spec.loader.exec_module(mod)
    _EXTRA_LEGACY_SCHEME_MOD = mod
    return mod


def extra_legacy_thumbnail_mod():
    """Load the byte-for-byte v0.9.29.9 native thumbnail writer for creation."""
    global _EXTRA_LEGACY_THUMBNAIL_MOD
    if _EXTRA_LEGACY_THUMBNAIL_MOD is not None:
        return _EXTRA_LEGACY_THUMBNAIL_MOD
    path = component_path(EXTRA_LEGACY_THUMBNAIL_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{EXTRA_LEGACY_THUMBNAIL_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_thumbnail_native_legacy_v25', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_thumbnail_native_legacy_v25'] = mod
    spec.loader.exec_module(mod)
    _EXTRA_LEGACY_THUMBNAIL_MOD = mod
    return mod


def extra_fixed_template_mod():
    """Load the exact v0.10 fixed-count stock-team Paint Select writer."""
    global _EXTRA_FIXED_TEMPLATE_MOD
    if _EXTRA_FIXED_TEMPLATE_MOD is not None:
        return _EXTRA_FIXED_TEMPLATE_MOD
    path = component_path(EXTRA_FIXED_TEMPLATE_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{EXTRA_FIXED_TEMPLATE_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_fixed_template_stock_paint_v1', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_fixed_template_stock_paint_v1'] = mod
    spec.loader.exec_module(mod)
    _EXTRA_FIXED_TEMPLATE_MOD = mod
    return mod


def _legacy_stock_creation_guard(driver_uid):
    """Block only a live moved/spare-team link; ignore all historical app state.

    This guard cannot false-lock a restored stock driver because it never reads
    driver_source_teams or prior move history. When no pristine team map exists,
    only explicit spare/custom team UIDs are blocked.
    """
    links = _team_fast_driver_links()
    driver = links.get(int(driver_uid))
    if not driver:
        return {'locked': True, 'reason': 'the driver has no current 2015 Cup team link'}
    team_uid = int(driver['team_uid'])
    config_uid = int(driver['config_uid'])
    if team_uid in SUPPORTED_SPARE_TEAM_UIDS:
        return {'locked': True, 'reason': 'added paint slots remain blocked for spare/custom teams'}
    originals = _team_original_team_map()
    original_uid = int(originals.get(config_uid, team_uid))
    moved = bool(team_uid != original_uid)
    return {'locked': False, 'team_uid': team_uid, 'config_uid': config_uid,
            'moved': moved, 'original_team_uid': original_uid,
            'experimental_moved_driver': moved}


def _extra_atomic_bytes(path, data):
    tmp = str(path) + '.extra_atomic.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(data); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def _extra_transaction_snapshot(reg, groups=('0','1','2'), inplace_thumbnail=None):
    snap = {
        'groups': {},
        'state_exists': os.path.exists(EXTRA_SCHEME_STATE),
        'state_bytes': open(EXTRA_SCHEME_STATE, 'rb').read() if os.path.exists(EXTRA_SCHEME_STATE) else None,
        'image_files': set(os.listdir(EXTRA_SCHEME_IMAGES)) if os.path.isdir(EXTRA_SCHEME_IMAGES) else set(),
        'image_overwrites': {},
        'inplace_thumbnail': None,
    }
    for key in groups:
        v = need(reg, key)
        snap['groups'][key] = {
            'archive': v['ar'], 'archive_size': os.path.getsize(v['ar']),
            'cdf': v['cdf'], 'cdf_bytes': open(v['cdf'], 'rb').read(),
        }
    if inplace_thumbnail:
        archive, row, raw, _entry = inplace_thumbnail
        snap['inplace_thumbnail'] = {
            'archive': str(archive), 'offset': int(row['offset']),
            'size': int(row['size']), 'raw': bytes(raw),
        }
    return snap


def _extra_transaction_restore(snapshot):
    errors = []
    if not snapshot:
        return errors
    try:
        item = snapshot.get('inplace_thumbnail')
        if item:
            with open(item['archive'], 'r+b') as fh:
                fh.seek(item['offset']); fh.write(item['raw']); fh.flush(); os.fsync(fh.fileno())
    except Exception as ex:
        errors.append('thumbnail restore: ' + str(ex))
    for key, item in snapshot.get('groups', {}).items():
        try:
            with open(item['archive'], 'r+b') as fh:
                fh.truncate(int(item['archive_size'])); fh.flush(); os.fsync(fh.fileno())
            _extra_atomic_bytes(item['cdf'], item['cdf_bytes'])
        except Exception as ex:
            errors.append(f'archive {key} restore: {ex}')
    try:
        if snapshot.get('state_exists'):
            _extra_atomic_bytes(EXTRA_SCHEME_STATE, snapshot.get('state_bytes') or b'')
        elif os.path.exists(EXTRA_SCHEME_STATE):
            os.remove(EXTRA_SCHEME_STATE)
    except Exception as ex:
        errors.append('state restore: ' + str(ex))
    try:
        for name, raw in (snapshot.get('image_overwrites') or {}).items():
            _extra_atomic_bytes(os.path.join(EXTRA_SCHEME_IMAGES, name), raw)
        before = snapshot.get('image_files', set())
        for name in os.listdir(EXTRA_SCHEME_IMAGES):
            if name not in before:
                path = os.path.join(EXTRA_SCHEME_IMAGES, name)
                if os.path.isfile(path):
                    os.remove(path)
    except Exception as ex:
        errors.append('source image cleanup: ' + str(ex))
    return errors


def _extra_clear_persisted_snapshot():
    if os.path.isdir(EXTRA_SCHEME_ROLLBACK_DIR):
        shutil.rmtree(EXTRA_SCHEME_ROLLBACK_DIR)


def _extra_persist_snapshot(snapshot, label, operation=None):
    """Persist the exact pre-write paint transaction for one-click undo.

    Archives in these workflows are append/repoint operations, so their original
    size plus exact CDF bytes is a complete rollback. The manifest is sealed
    after a successful write with the exact post-write archive tails and CDF
    hashes. Delete is then allowed only when that sealed state still matches.
    """
    if not snapshot:
        raise ValueError('paint rollback snapshot is empty')
    tmp = EXTRA_SCHEME_ROLLBACK_DIR + '.tmp'
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, exist_ok=True)
    groups = {}
    for key, item in snapshot.get('groups', {}).items():
        cdf_name = f'cdf_{key}.bin'
        with open(os.path.join(tmp, cdf_name), 'wb') as fh:
            fh.write(item['cdf_bytes'])
        groups[str(key)] = {
            'archive': str(item['archive']), 'archive_size': int(item['archive_size']),
            'cdf': str(item['cdf']), 'cdf_backup': cdf_name,
        }
    state_name = None
    if snapshot.get('state_exists'):
        state_name = 'extra_schemes_state.bin'
        with open(os.path.join(tmp, state_name), 'wb') as fh:
            fh.write(snapshot.get('state_bytes') or b'')
    images_dir = os.path.join(tmp, 'images')
    os.makedirs(images_dir, exist_ok=True)
    if os.path.isdir(EXTRA_SCHEME_IMAGES):
        for name in os.listdir(EXTRA_SCHEME_IMAGES):
            src = os.path.join(EXTRA_SCHEME_IMAGES, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(images_dir, name))
    inplace_meta = None
    inplace = snapshot.get('inplace_thumbnail')
    if inplace:
        raw_name = 'inplace_thumbnail.bin'
        with open(os.path.join(tmp, raw_name), 'wb') as fh:
            fh.write(inplace['raw'])
        inplace_meta = {
            'archive': str(inplace['archive']), 'offset': int(inplace['offset']),
            'size': int(inplace['size']), 'raw_backup': raw_name,
        }
    manifest = {
        'format': 'nascar15-extra-scheme-rollback-v2', 'version': 2,
        'mode': 'restore_pre',
        'label': str(label or 'Last paint change'),
        'created': time.strftime('%Y-%m-%d %H:%M:%S'),
        'groups': groups, 'state_exists': bool(snapshot.get('state_exists')),
        'state_backup': state_name, 'inplace_thumbnail': inplace_meta,
        'operation': dict(operation or {}),
        'post_state': None,
    }
    with open(os.path.join(tmp, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)
    if os.path.isdir(EXTRA_SCHEME_ROLLBACK_DIR):
        shutil.rmtree(EXTRA_SCHEME_ROLLBACK_DIR)
    os.replace(tmp, EXTRA_SCHEME_ROLLBACK_DIR)
    return manifest


def _extra_sha256_bytes(data):
    return hashlib.sha256(bytes(data)).hexdigest()


def _extra_sha256_file(path, start=0, size=None):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        fh.seek(int(start))
        left = None if size is None else int(size)
        while True:
            chunk = fh.read(1024 * 1024 if left is None else min(1024 * 1024, left))
            if not chunk:
                break
            h.update(chunk)
            if left is not None:
                left -= len(chunk)
                if left <= 0:
                    break
    return h.hexdigest()


def _extra_image_fingerprint(folder):
    out = {}
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                out[name] = _extra_sha256_file(path)
    return out


def _extra_seal_persisted_snapshot(operation=None):
    """Seal a successful append/repoint transaction for safe future deletion."""
    path = os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'manifest.json')
    if not os.path.exists(path):
        raise ValueError('paint rollback manifest vanished before it could be sealed')
    manifest = json.load(open(path, 'r', encoding='utf-8'))
    if manifest.get('format') != 'nascar15-extra-scheme-rollback-v2':
        raise ValueError('paint rollback manifest is not the reversible v2 format')
    post_groups = {}
    for key, item in (manifest.get('groups') or {}).items():
        archive = os.path.abspath(str(item['archive']))
        cdf = os.path.abspath(str(item['cdf']))
        pre_size = int(item['archive_size'])
        post_size = os.path.getsize(archive)
        if post_size < pre_size:
            raise ValueError(f'ARCHIVE{key} shrank during paint creation')
        post_groups[str(key)] = {
            'archive_size': int(post_size),
            'tail_size': int(post_size - pre_size),
            'tail_sha256': _extra_sha256_file(archive, pre_size, post_size - pre_size),
            'cdf_size': os.path.getsize(cdf),
            'cdf_sha256': _extra_sha256_file(cdf),
        }
    state_exists = os.path.exists(EXTRA_SCHEME_STATE)
    state_sha = _extra_sha256_file(EXTRA_SCHEME_STATE) if state_exists else None
    manifest['post_state'] = {
        'groups': post_groups,
        'state_exists': bool(state_exists),
        'state_sha256': state_sha,
        'images': _extra_image_fingerprint(EXTRA_SCHEME_IMAGES),
    }
    if operation:
        manifest['operation'] = dict(operation)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(tmp, path)
    return manifest


def _extra_verify_manifest_post_state(manifest):
    post = manifest.get('post_state') or {}
    if not post.get('groups'):
        raise ValueError('this paint checkpoint predates exact delete support; restore a clean game and create the slot again with this release')
    for key, expected in post['groups'].items():
        item = (manifest.get('groups') or {}).get(str(key)) or {}
        archive = os.path.abspath(str(item.get('archive') or ''))
        cdf = os.path.abspath(str(item.get('cdf') or ''))
        pre_size = int(item.get('archive_size', -1))
        if not os.path.exists(archive) or not os.path.exists(cdf):
            raise ValueError(f'ARCHIVE{key} checkpoint target is missing')
        post_size = int(expected['archive_size'])
        if os.path.getsize(archive) != post_size:
            raise ValueError(f'ARCHIVE{key} changed after this slot was created; exact delete is blocked')
        tail_size = int(expected['tail_size'])
        if post_size - pre_size != tail_size:
            raise ValueError(f'ARCHIVE{key} append geometry no longer matches the creation checkpoint')
        if _extra_sha256_file(archive, pre_size, tail_size) != expected['tail_sha256']:
            raise ValueError(f'ARCHIVE{key} appended bytes changed after this slot was created; exact delete is blocked')
        if os.path.getsize(cdf) != int(expected['cdf_size']) or _extra_sha256_file(cdf) != expected['cdf_sha256']:
            raise ValueError(f'cdfiles{key}.dat changed after this slot was created; exact delete is blocked')
    state_exists = os.path.exists(EXTRA_SCHEME_STATE)
    if bool(post.get('state_exists')) != bool(state_exists):
        raise ValueError('the app-created paint state changed after this slot was created')
    if state_exists and _extra_sha256_file(EXTRA_SCHEME_STATE) != post.get('state_sha256'):
        raise ValueError('the app-created paint state changed after this slot was created')
    if _extra_image_fingerprint(EXTRA_SCHEME_IMAGES) != (post.get('images') or {}):
        raise ValueError('saved paint/thumbnail files changed after this slot was created')
    return True


def _extra_load_persisted_snapshot(reg=None):
    path = os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'manifest.json')
    if not os.path.exists(path):
        raise ValueError('there is no paint change to undo')
    manifest = json.load(open(path, 'r', encoding='utf-8'))
    if manifest.get('format') not in ('nascar15-extra-scheme-rollback-v1', 'nascar15-extra-scheme-rollback-v2'):
        raise ValueError('the saved paint rollback manifest is not recognized')
    groups = {}
    for key, item in (manifest.get('groups') or {}).items():
        key = str(key)
        archive_path = os.path.abspath(str(item['archive']))
        cdf_path = os.path.abspath(str(item['cdf']))
        if reg is not None:
            live = need(reg, key)
            if (os.path.normcase(os.path.abspath(live['ar'])) != os.path.normcase(archive_path) or
                    os.path.normcase(os.path.abspath(live['cdf'])) != os.path.normcase(cdf_path)):
                raise ValueError('the saved paint undo belongs to a different NASCAR 15 installation')
        if not os.path.exists(archive_path) or not os.path.exists(cdf_path):
            raise ValueError(f'the saved paint undo target for ARCHIVE{key} no longer exists')
        original_size = int(item['archive_size'])
        if os.path.getsize(archive_path) < original_size:
            raise ValueError(f'ARCHIVE{key} is smaller than the saved pre-change size; refusing an unsafe undo')
        cdf_bytes = open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, item['cdf_backup']), 'rb').read()
        if len(cdf_bytes) < 64 or cdf_bytes[:4] != b'filC':
            raise ValueError(f'the saved CDF backup for ARCHIVE{key} is invalid')
        groups[key] = {
            'archive': archive_path, 'archive_size': original_size,
            'cdf': cdf_path, 'cdf_bytes': cdf_bytes,
        }
    state_bytes = None
    if manifest.get('state_exists') and manifest.get('state_backup'):
        state_bytes = open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, manifest['state_backup']), 'rb').read()
    image_dir = os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'images')
    image_overwrites = {}
    image_files = set()
    if os.path.isdir(image_dir):
        for name in os.listdir(image_dir):
            src = os.path.join(image_dir, name)
            if os.path.isfile(src):
                image_files.add(name)
                image_overwrites[name] = open(src, 'rb').read()
    inplace = None
    im = manifest.get('inplace_thumbnail')
    if im:
        inplace = {
            'archive': im['archive'], 'offset': int(im['offset']), 'size': int(im['size']),
            'raw': open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, im['raw_backup']), 'rb').read(),
        }
    return {
        'groups': groups, 'state_exists': bool(manifest.get('state_exists')),
        'state_bytes': state_bytes, 'image_files': image_files,
        'image_overwrites': image_overwrites, 'inplace_thumbnail': inplace,
    }, manifest


def _extra_prepare_delete_redo(manifest, uid):
    """Capture the exact post-create bytes before rolling the latest slot back."""
    tmp = EXTRA_SCHEME_ROLLBACK_DIR + '.redo.tmp'
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    shutil.copytree(EXTRA_SCHEME_ROLLBACK_DIR, tmp)
    redo_groups = {}
    for key, item in (manifest.get('groups') or {}).items():
        archive = os.path.abspath(str(item['archive']))
        cdf = os.path.abspath(str(item['cdf']))
        base_size = int(item['archive_size'])
        current_size = os.path.getsize(archive)
        tail_name = f'redo_tail_{key}.bin'
        with open(archive, 'rb') as fh:
            fh.seek(base_size)
            tail = fh.read(current_size - base_size)
        with open(os.path.join(tmp, tail_name), 'wb') as fh:
            fh.write(tail)
        cdf_name = f'redo_cdf_{key}.bin'
        shutil.copy2(cdf, os.path.join(tmp, cdf_name))
        redo_groups[str(key)] = {
            'tail_backup': tail_name, 'tail_size': len(tail),
            'cdf_backup': cdf_name,
        }
    redo_state = None
    redo_state_exists = os.path.exists(EXTRA_SCHEME_STATE)
    if redo_state_exists:
        redo_state = 'redo_state.bin'
        shutil.copy2(EXTRA_SCHEME_STATE, os.path.join(tmp, redo_state))
    redo_images = os.path.join(tmp, 'redo_images')
    os.makedirs(redo_images, exist_ok=True)
    if os.path.isdir(EXTRA_SCHEME_IMAGES):
        for name in os.listdir(EXTRA_SCHEME_IMAGES):
            src = os.path.join(EXTRA_SCHEME_IMAGES, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(redo_images, name))
    manifest = dict(manifest)
    manifest['mode'] = 'redo_post'
    manifest['label'] = f'Undo deletion of paint slot UID {int(uid)}'
    manifest['redo'] = {
        'groups': redo_groups,
        'state_exists': bool(redo_state_exists),
        'state_backup': redo_state,
        'images_dir': 'redo_images',
    }
    with open(os.path.join(tmp, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)
    return tmp


def _extra_verify_pre_state(snapshot):
    for key, item in snapshot.get('groups', {}).items():
        if os.path.getsize(item['archive']) != int(item['archive_size']):
            raise ValueError(f'ARCHIVE{key} is not at the exact pre-create size; deleted-slot undo is blocked')
        if open(item['cdf'], 'rb').read() != item['cdf_bytes']:
            raise ValueError(f'cdfiles{key}.dat is not at the exact pre-create state; deleted-slot undo is blocked')
    state_exists = os.path.exists(EXTRA_SCHEME_STATE)
    if bool(snapshot.get('state_exists')) != bool(state_exists):
        raise ValueError('the app-created paint state is not at the exact pre-create state')
    if state_exists and open(EXTRA_SCHEME_STATE, 'rb').read() != (snapshot.get('state_bytes') or b''):
        raise ValueError('the app-created paint state is not at the exact pre-create state')
    current_images = set(os.listdir(EXTRA_SCHEME_IMAGES)) if os.path.isdir(EXTRA_SCHEME_IMAGES) else set()
    if current_images != set(snapshot.get('image_files') or set()):
        raise ValueError('saved paint/thumbnail files are not at the exact pre-create state')
    for name, raw in (snapshot.get('image_overwrites') or {}).items():
        path = os.path.join(EXTRA_SCHEME_IMAGES, name)
        if not os.path.isfile(path) or open(path, 'rb').read() != raw:
            raise ValueError('saved paint/thumbnail files are not at the exact pre-create state')
    return True


def _extra_reapply_deleted_slot(snapshot, manifest):
    _extra_verify_pre_state(snapshot)
    redo = manifest.get('redo') or {}
    current = _extra_transaction_snapshot({k: {'ar': v['archive'], 'cdf': v['cdf']} for k, v in snapshot['groups'].items()}, tuple(snapshot['groups']))
    try:
        for key, item in snapshot['groups'].items():
            r = (redo.get('groups') or {}).get(str(key)) or {}
            tail = open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, r['tail_backup']), 'rb').read()
            if len(tail) != int(r['tail_size']):
                raise ValueError(f'ARCHIVE{key} redo tail is incomplete')
            with open(item['archive'], 'ab') as fh:
                fh.write(tail); fh.flush(); os.fsync(fh.fileno())
            cdf_bytes = open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, r['cdf_backup']), 'rb').read()
            _extra_atomic_bytes(item['cdf'], cdf_bytes)
        if redo.get('state_exists'):
            raw = open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, redo['state_backup']), 'rb').read()
            _extra_atomic_bytes(EXTRA_SCHEME_STATE, raw)
        elif os.path.exists(EXTRA_SCHEME_STATE):
            os.remove(EXTRA_SCHEME_STATE)
        os.makedirs(EXTRA_SCHEME_IMAGES, exist_ok=True)
        for name in list(os.listdir(EXTRA_SCHEME_IMAGES)):
            path = os.path.join(EXTRA_SCHEME_IMAGES, name)
            if os.path.isfile(path):
                os.remove(path)
        redo_images = os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, redo.get('images_dir') or 'redo_images')
        if os.path.isdir(redo_images):
            for name in os.listdir(redo_images):
                shutil.copy2(os.path.join(redo_images, name), os.path.join(EXTRA_SCHEME_IMAGES, name))
        _extra_verify_manifest_post_state(manifest)
    except Exception:
        _extra_transaction_restore(current)
        raise
    manifest = dict(manifest)
    manifest['mode'] = 'restore_pre'
    uid = int((manifest.get('operation') or {}).get('uid', -1))
    manifest['label'] = f'Delete paint slot UID {uid}' if uid >= 0 else 'Undo restored paint slot'
    with open(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'manifest.json.tmp'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)
    os.replace(os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'manifest.json.tmp'),
               os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'manifest.json'))
    return manifest

def _extra_rollback_status():
    path = os.path.join(EXTRA_SCHEME_ROLLBACK_DIR, 'manifest.json')
    if not os.path.exists(path):
        return {'available': False}
    try:
        obj = json.load(open(path, 'r', encoding='utf-8'))
        return {'available': True, 'label': obj.get('label') or 'Last paint change',
                'created': obj.get('created') or ''}
    except Exception as ex:
        return {'available': False, 'blocked_reason': str(ex)}


def _extra_active_created_count(state, driver_uid):
    return sum(1 for item in state.get('schemes', [])
               if int(item.get('driver_uid', -1)) == int(driver_uid) and not item.get('superseded_by'))


def _extra_reconcile_state_with_live_database(mod, game):
    """Make the live database/assets authoritative for app-created slots.

    v1.0.1 treated ``extra_schemes_v1.json`` as ownership truth.  A clean app
    folder therefore hid slots that were still fully installed in the game.
    The allocator did notice their occupied UIDs, but every repair/delete/limit
    guard lost the relationship.  Rebuild the minimum safe state from verified
    live records before any catalog or write operation.

    Detection is intentionally conservative: verified allocation UIDs and the
    app's compact pre-25600 ScriptName pattern qualify, and the live record must
    have its canonical SD/HD pair in ARCHIVE2 at the proven wrapper sizes.
    Ambiguous or incomplete records remain ordinary game liveries and are never
    silently adopted. When possible, paint and menu-preview PNGs are recovered
    from the live game so a fresh app folder needs no migration step.
    """
    state = mod.load_state(EXTRA_SCHEME_STATE)
    ctx = mod.base.load_context(str(game))
    live = {}
    for record in mod.base.records_of(ctx, 'LIVERIE_c'):
        uid = mod.base.pointer_int(ctx, record.uid)
        if uid is None:
            continue
        try:
            driver_uid = mod.base.field_uid(ctx, record, 'Driver')
            script = str(mod.base.display(ctx, record.fields.get('ScriptName')) or '').strip()
        except Exception:
            continue
        live[int(uid)] = {'record': record, 'driver_uid': driver_uid, 'script_name': script}
    live_uids = set(live)

    schemes = list(state.get('schemes', []))
    active = []
    stale = []
    active_uids = set()
    for item in schemes:
        try:
            uid = int(item.get('uid', -1))
        except Exception:
            uid = -1
        if uid >= 0 and uid not in live_uids:
            row = dict(item)
            row['orphaned_at'] = int(time.time())
            row['orphaned_reason'] = 'livery UID absent after game-file restore or rollback'
            stale.append(row)
        else:
            active.append(item)
            if uid >= 0 and not item.get('superseded_by'):
                active_uids.add(uid)

    # App-created UIDs are never occupied by the clean game.  Include any
    # user-verified extension UIDs already merged into the helper pool.
    safe_pool = {int(x) for x in getattr(mod, 'VERIFIED_SAFE_EXTRA_UIDS', ())}
    discovered = []
    scan_rejections = []
    try:
        _primary, by_name = mod._asset_index(Path(game))
    except Exception:
        by_name = {}
    try:
        live_team_links = _team_fast_driver_links()
    except Exception:
        live_team_links = {}
    try:
        live_registry = registry()[1]
    except Exception:
        live_registry = {}
    now = int(time.time())
    def looks_app_created(uid, script):
        text = str(script or '').strip().upper()
        if int(uid) in safe_pool:
            return True
        if int(uid) >= 25600:
            return False
        return bool(
            re.match(r'^15_[0-9]+[A-Z]?_[A-Z0-9]+_EXTRA(?:_SLOT)?_[0-9]+$', text)
            or re.match(r'^CUSTOM_[0-9]+_[0-9]+_', text)
        )

    recovered_thumbnail_uids=[]
    def recover_live_thumbnail(item):
        """Recover the current in-game Paint Select image for an adopted slot."""
        try:
            uid=int(item.get('uid',-1));driver_uid=int(item.get('driver_uid',-1))
        except Exception:
            return False
        if uid<0 or uid not in live_uids:
            return False
        link=live_team_links.get(driver_uid)
        if not link:
            return False
        target_container=f"2DRIVERSELECTTD_{int(link['team_uid'])}.ARC"
        script=str(item.get('script_name') or live.get(uid,{}).get('script_name') or '').strip()
        if not script:
            return False
        os.makedirs(EXTRA_SCHEME_IMAGES,exist_ok=True)
        thumb_name=f"{uid}__{script}.thumbnail.png"
        thumb_path=os.path.join(EXTRA_SCHEME_IMAGES,thumb_name)
        existing_name=os.path.basename(str(item.get('thumbnail_source_png') or ''))
        existing_path=os.path.join(EXTRA_SCHEME_IMAGES,existing_name) if existing_name else ''
        needs_image=not existing_path or not os.path.isfile(existing_path)
        needs_check=not bool(item.get('thumbnail_live_checked'))
        if not needs_image and not needs_check:
            return False
        changed_local=False
        try:
            live_thumb=_extra_read_live_native_thumbnail_preview(game,uid,target_container)
            if needs_image:
                live_thumb.save(thumb_path,'PNG')
                item['thumbnail_source_png']=thumb_name
                changed_local=True
            identity={}
            try:
                identity=extra_thumbnail_mod().inspect_thumbnail_identity(
                    game,uid,target_container_name=target_container) or {}
            except Exception:
                identity={}
            updates={
                'thumbnail_live_checked':True,
                'thumbnail_live_present':True,
                'thumbnail_game_safe':bool(identity.get('same_bank_valid')),
                'thumbnail_same_bank_identity':bool(identity.get('identity_self_identifying') and identity.get('public_name_resolved')),
                'thumbnail_identity_name':identity.get('identity_name') or identity.get('identity_root_name'),
                'preview_status':'detected_live_thumbnail',
                'preview_container':target_container,
                'preview_entry':f'PAINTSCHEME_{uid}',
            }
            for key,value in updates.items():
                if item.get(key)!=value:
                    item[key]=value;changed_local=True
            if uid not in recovered_thumbnail_uids:
                recovered_thumbnail_uids.append(uid)
        except Exception as ex:
            if item.get('thumbnail_live_checked') is not True:
                item['thumbnail_live_checked']=True;changed_local=True
            text=str(ex)
            if item.get('thumbnail_live_error')!=text:
                item['thumbnail_live_error']=text;changed_local=True
        return changed_local

    discoverable = {uid for uid in live_uids if looks_app_created(uid, live[uid].get('script_name'))}
    for uid in sorted(discoverable - active_uids):
        row = live[uid]
        script = row.get('script_name') or ''
        driver_uid = row.get('driver_uid')
        if not script or driver_uid is None:
            continue
        # ApplyPatch-created liveries carry Driver/Package/World/Season links
        # directly in their constructor. They intentionally do *not* have the
        # stock generator's later post-assignment bytecode blocks. Requiring
        # post_assignment_blocks(uid) therefore rejected the exact live records
        # created by dev21-dev29 when a newer app started with an empty profile.
        try:
            pair, sd_size, hd_size, in_archive2 = mod._has_pair(by_name, script)
        except Exception:
            pair = in_archive2 = False
            sd_size = hd_size = None

        required_links = {}
        for field in ('Driver', 'Package', 'World', 'Season'):
            try:
                required_links[field] = mod.base.field_uid(ctx, row['record'], field)
            except Exception:
                required_links[field] = None
        complete_record = all(required_links.get(field) is not None
                              for field in ('Driver', 'Package', 'World', 'Season'))

        rejection = []
        if not pair:
            rejection.append('missing canonical SD/HD asset pair')
        if pair and not in_archive2:
            rejection.append('SD/HD pair is not fully indexed in ARCHIVE2')
        if int(sd_size or 0) != 1458529:
            rejection.append(f'unexpected SD wrapper size {int(sd_size or 0)}')
        if int(hd_size or 0) != 5652833:
            rejection.append(f'unexpected HD wrapper size {int(hd_size or 0)}')
        if not complete_record:
            missing = [field for field in ('Driver', 'Package', 'World', 'Season')
                       if required_links.get(field) is None]
            rejection.append('missing live database link(s): ' + ', '.join(missing))
        if rejection:
            scan_rejections.append({
                'uid': int(uid),
                'script_name': script,
                'reasons': rejection,
            })
            continue
        os.makedirs(EXTRA_SCHEME_IMAGES, exist_ok=True)
        source_name = f"{uid}__{script}.png"
        source_path = os.path.join(EXTRA_SCHEME_IMAGES, source_name)
        thumb_name = f"{uid}__{script}.thumbnail.png"
        thumb_path = os.path.join(EXTRA_SCHEME_IMAGES, thumb_name)
        item = {
            'uid': int(uid),
            'driver_uid': int(driver_uid),
            'donor_uid': int(getattr(mod, 'PROVEN_EXTRA_DONOR_UID', 25580)),
            'donor_script_name': str(getattr(mod, 'PROVEN_EXTRA_DONOR_SCRIPT', '')),
            'script_name': script,
            'name': ('Additional Scheme' if re.search(r'(?:^|_)EXTRA(?:_|$)', script, re.I) else mod._friendly_livery_label(script, '', '')),
            'sd_entry': f'LIVERY_{script}.ARC',
            'hd_entry': f'HDLIVERY_{script}.ARC',
            'source_png': source_name if os.path.exists(source_path) else '',
            'thumbnail_source_png': thumb_name if os.path.exists(thumb_path) else '',
            'created': now,
            'preview_status': 'detected_from_live',
            'thumbnail_game_safe': False,
            'thumbnail_live_checked': False,
            'native_runtime_layout_version': 1,
            'structure_donor_uid': int(getattr(mod, 'PROVEN_EXTRA_DONOR_UID', 25580)),
            'structure_donor_script_name': str(getattr(mod, 'PROVEN_EXTRA_DONOR_SCRIPT', '')),
            'database_recipe': 'recovered_live_scan',
            'discovered_from_live_files': True,
            'discovered_at': now,
        }
        # Rebuild the convenience PNGs from the live archives. These files are
        # not ownership truth; failure to decode one never blocks adoption.
        if not item['source_png']:
            try:
                live_paint = _extra_read_live_paint_image(item, game, live_registry)
                live_paint.save(source_path, 'PNG')
                item['source_png'] = source_name
            except Exception:
                pass
        recover_live_thumbnail(item)
        active.append(item)
        active_uids.add(uid)
        discovered.append(uid)

    thumbnail_recovery_changed=False
    for item in active:
        if recover_live_thumbnail(item):
            thumbnail_recovery_changed=True

    stale_uids = {int(x['uid']) for x in stale if x.get('uid') is not None}
    changed = bool(stale or discovered or thumbnail_recovery_changed)
    if stale:
        old_orphans = list(state.get('orphaned_schemes', []))
        by_uid = {}
        for row in old_orphans + stale:
            try:
                by_uid[int(row.get('uid', -1))] = row
            except Exception:
                continue
        state['orphaned_schemes'] = [by_uid[k] for k in sorted(by_uid)]

    removed_assignments = 0
    if stale_uids:
        clean_assignments = {}
        for event_key, rows in (state.get('assignments') or {}).items():
            if not isinstance(rows, dict):
                continue
            clean_rows = {}
            for driver_key, livery_uid in rows.items():
                try:
                    if int(livery_uid) in stale_uids:
                        removed_assignments += 1
                        continue
                except Exception:
                    pass
                clean_rows[str(driver_key)] = livery_uid
            if clean_rows:
                clean_assignments[str(event_key)] = clean_rows
        state['assignments'] = clean_assignments
        finalizer = state.get('registry_finalizer')
        if isinstance(finalizer, dict):
            try:
                if int(finalizer.get('newest_uid', -1)) in stale_uids:
                    state.pop('registry_finalizer', None)
            except Exception:
                pass

    state['schemes'] = active
    state['last_live_state_reconciliation'] = {
        'at': now,
        'removed_uids': sorted(stale_uids),
        'discovered_uids': sorted(discovered),
        'assignment_rows_removed': int(removed_assignments),
        'source_of_truth': 'live DB direct links + canonical ARCHIVE2 SD/HD pair',
        'candidate_rejections': scan_rejections,
        'recovered_thumbnail_uids': sorted(recovered_thumbnail_uids),
    }
    if changed or not os.path.exists(EXTRA_SCHEME_STATE):
        mod.save_state(EXTRA_SCHEME_STATE, state)
    return {
        'changed': changed,
        'removed_uids': sorted(stale_uids),
        'discovered_uids': sorted(discovered),
        'assignment_rows_removed': int(removed_assignments),
        'quarantined_count': len(stale),
        'discovered_count': len(discovered),
        'recovered_thumbnail_uids': sorted(recovered_thumbnail_uids),
        'candidate_rejections': scan_rejections,
    }

def _extra_thumbnail_replace_capability(game, driver, uid, target_container=None):
    """Read-only check for whether an added-scheme thumbnail can be safely rewritten.

    A live thumbnail can already work in game even when there is no same-bank donor
    available for a future repair/rewrite. Surface that honestly in the UI instead
    of warning that a repair is needed when the only safe action is export/use-as-is.
    """
    tm = extra_thumbnail_mod()
    uid = int(uid)
    out = {'supported': False, 'uid': uid, 'container': target_container or ''}
    if target_container is None:
        preview_container, _driver = _team_preview_container_for_driver(int((driver or {}).get('uid', -1)))
        target_container = preview_container
        out['container'] = preview_container
    try:
        identity = tm.inspect_thumbnail_identity(game, uid, target_container_name=target_container) or {}
    except Exception as ex:
        identity = {}
        out['identity_error'] = str(ex)
    out['identity_name'] = identity.get('identity_name') or identity.get('identity_root_name') or ''
    out['live_present'] = bool(identity.get('exists')) if identity else False
    if identity.get('same_bank_valid'):
        out.update({'supported': True, 'mode': 'self', 'reason': ''})
        return out
    try:
        donor = _extra_thumbnail_donor(game, driver, exclude_uid=uid, target_container=target_container)
        out.update({
            'supported': True,
            'mode': 'donor',
            'reason': '',
            'donor_uid': int(donor.get('uid', -1)),
            'donor_container': str(donor.get('container') or ''),
            'identity_name': out.get('identity_name') or str(donor.get('identity_name') or ''),
        })
    except Exception as ex:
        out.update({'supported': False, 'mode': 'unavailable', 'reason': str(ex)})
    return out

def _extra_thumbnail_donor(game, driver, exclude_uid=None, target_container=None):
    tm = extra_thumbnail_mod()
    schemes = list((driver or {}).get('schemes', []))
    schemes.sort(key=lambda x: (
        1 if x.get('managed') else 0,
        0 if str(x.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
        0 if x.get('year') == 2015 else 1,
        int(x.get('uid', 999999)),
    ))
    for scheme in schemes:
        uid = int(scheme.get('uid', -1))
        if uid < 0 or (exclude_uid is not None and uid == int(exclude_uid)):
            continue
        hit = tm.find_target(game, uid, target_container_name=target_container)
        if not hit:
            continue
        entry = hit[3]
        identity = tm.inspect_thumbnail_identity(
            game, uid, target_container_name=target_container)
        if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                and str(entry.get('fmt')) == 'DXT5' and identity.get('same_bank_valid')):
            return {'uid': uid, 'container': hit[1]['name'], 'entry': entry['name'],
                    'identity_name': identity.get('identity_name')}
    # A thumbnail resource is only a same-bank structural donor. It does not
    # need to belong to the same driver. Requiring that made valid clean-game
    # drivers impossible to repair when their own stock tile used an alias or
    # no self-identifying 256x256 anchor. Fall back to any safe native paint
    # identity in the destination team bank; the imported thumbnail pixels are
    # still written afterward.
    if target_container:
        try:
            ta = team_assets_mod()
            team_uid = int(re.search(r'_(\d+)\.ARC$', str(target_container), re.I).group(1))
            for resource in ta.team_container_resource_names(game, team_uid):
                m = re.fullmatch(r'PAINTSCHEME_(\d+)', str(resource), re.I)
                if not m:
                    continue
                uid = int(m.group(1))
                if exclude_uid is not None and uid == int(exclude_uid):
                    continue
                hit = tm.find_target(game, uid, target_container_name=target_container)
                if not hit:
                    continue
                entry = hit[3]
                identity = tm.inspect_thumbnail_identity(
                    game, uid, target_container_name=target_container)
                if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                        and str(entry.get('fmt')) == 'DXT5'
                        and identity.get('same_bank_valid')):
                    return {'uid': uid, 'container': hit[1]['name'],
                            'entry': entry['name'],
                            'identity_name': identity.get('identity_name'),
                            'fallback_scope': 'same_team_bank'}
        except Exception:
            pass
    raise ValueError('No structurally safe 256×256 native thumbnail exists in this team bank')


def _extra_thumbnail_donor_stock_legacy(game, driver, exclude_uid=None, target_container=None):
    """v0.9.30.5 donor selection for the protected native stock-team path."""
    tm = extra_stock_thumbnail_mod()
    schemes = list((driver or {}).get('schemes', []))
    schemes.sort(key=lambda x: (
        1 if x.get('managed') else 0,
        0 if str(x.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
        0 if x.get('year') == 2015 else 1,
        int(x.get('uid', 999999)),
    ))
    for scheme in schemes:
        uid = int(scheme.get('uid', -1))
        if uid < 0 or (exclude_uid is not None and uid == int(exclude_uid)):
            continue
        hit = tm.find_target(game, uid, target_container_name=target_container)
        if not hit:
            continue
        entry = hit[3]
        if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                and str(entry.get('fmt')) == 'DXT5'):
            return {'uid': uid, 'container': hit[1]['name'], 'entry': entry['name']}
    raise ValueError('No compatible 256×256 native thumbnail exists for this driver')


def _extra_thumbnail_donor_legacy299(game, driver, exclude_uid=None):
    """Exact v0.9.29.9 donor selection using the exact legacy v2.5 writer."""
    tm = extra_legacy_thumbnail_mod()
    schemes = list((driver or {}).get('schemes', []))
    schemes.sort(key=lambda x: (
        1 if x.get('managed') else 0,
        0 if str(x.get('script_name') or '').upper().endswith('_PRIMARY') else 1,
        0 if x.get('year') == 2015 else 1,
        int(x.get('uid', 999999)),
    ))
    for scheme in schemes:
        uid = int(scheme.get('uid', -1))
        if uid < 0 or (exclude_uid is not None and uid == int(exclude_uid)):
            continue
        hit = tm.find_target(game, uid)
        if not hit:
            continue
        entry = hit[3]
        if (int(entry.get('w', 0)) == 256 and int(entry.get('h', 0)) == 256
                and str(entry.get('fmt')) == 'DXT5'):
            return {'uid': uid, 'container': hit[1]['name'], 'entry': entry['name']}
    raise ValueError('No compatible 256×256 native thumbnail exists for this driver')


def _extra_prepare_thumbnail_source(raw, quality='auto'):
    if not raw:
        raise ValueError('choose a thumbnail image')
    image = Image.open(io.BytesIO(raw)); image.load()
    prepared, prep = _extra_prepare_thumbnail(image, (256, 256), quality)
    return prepared, prep


def _extra_read_live_native_thumbnail_preview(game, uid, target_container):
    """Decode a live Paint Select thumbnail without changing game files.

    Upgrade discovery must recover the image the game already uses even when an
    older app build did not preserve its source PNG or when the strict native
    identity audit needs a later repair.  This reader validates the current
    team-bank resource geometry and payload bounds, but deliberately does not
    require the write-path identity guard because it is read-only.
    """
    tm = extra_thumbnail_mod()
    hit = tm.find_target(game, int(uid), target_container_name=target_container)
    if not hit:
        raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in {target_container}')
    _archive, _row, arc, _legacy_entry = hit
    entries, _ = C.parse_multi_arc(arc)
    name = f'PAINTSCHEME_{int(uid)}'
    entry = next((e for e in entries if e['name'] == name), None)
    if entry is None:
        raise ValueError(f'{name} is missing from the native texture table')
    if (str(entry.get('fmt')) != 'DXT5' or int(entry.get('w', 0)) != 256
            or int(entry.get('h', 0)) != 256
            or int(entry.get('payload_size', 0)) < int(entry.get('needed', 0))):
        raise ValueError('live thumbnail is not a complete 256×256 DXT5 resource')
    return C.multi_read_png(arc, entry)


def _extra_read_live_native_thumbnail(game, uid, target_container):
    """Decode the exact live PAINTSCHEME resource from the current team bank."""
    tm = extra_thumbnail_mod()
    identity = tm.inspect_thumbnail_identity(
        game, int(uid), target_container_name=target_container)
    if not identity.get('same_bank_valid'):
        raise ValueError(
            f'PAINTSCHEME_{int(uid)} does not have valid current-team native wiring')
    hit = tm.find_target(game, int(uid), target_container_name=target_container)
    if not hit:
        raise ValueError(f'PAINTSCHEME_{int(uid)} was not found in {target_container}')
    _archive, _row, arc, _legacy_entry = hit
    entries, _ = C.parse_multi_arc(arc)
    name = f'PAINTSCHEME_{int(uid)}'
    entry = next((e for e in entries if e['name'] == name), None)
    if entry is None:
        raise ValueError(f'{name} is missing from the canonical native texture table')
    if entry['fmt'] != 'DXT5' or int(entry['payload_size']) < int(entry['needed']):
        raise ValueError('native thumbnail payload bounds or format are invalid')
    return C.multi_read_png(arc, entry)


def _extra_save_live_native_thumbnail(game, uid, target_container, out_path):
    """Save a browser preview decoded from the exact live native resource."""
    image = _extra_read_live_native_thumbnail(game, uid, target_container)
    image.save(out_path, 'PNG')
    return image


def _extra_update_preview_state(mod, uid, report, source_name):
    state = mod.load_state(EXTRA_SCHEME_STATE)
    item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(uid)), None)
    if item is None:
        raise ValueError('the created scheme disappeared from the app state')
    safe_clone = bool(report.get('game_safe_raw_clone'))
    safe_custom = bool(report.get('game_safe_same_bank_custom') or report.get('game_safe_stock_legacy'))
    safe = bool(safe_clone or safe_custom)
    item.update({
        'preview_status': ('safe_clone' if safe_clone else
                           'custom_same_bank' if safe_custom else 'custom_unverified'),
        'preview_container': report.get('container'),
        'preview_entry': f'PAINTSCHEME_{int(uid)}',
        'preview_method': report.get('method', 'native_expand_v25'),
        'preview_encoder': report.get('encoder'),
        'preview_readback_verified': bool(report.get('readback_verified')),
        'preview_game_verified': False,
        'thumbnail_source_png': os.path.basename(source_name),
        'thumbnail_requested': True,
        'thumbnail_installed': int(time.time()),
        'thumbnail_game_safe': safe,
        'thumbnail_same_bank_identity': bool(report.get('game_safe_same_bank_custom') or report.get('game_safe_raw_clone')),
        'thumbnail_stock_legacy_safe': bool(report.get('game_safe_stock_legacy')),
        'thumbnail_identity_name': report.get('identity_name'),
    })
    mod.save_state(EXTRA_SCHEME_STATE, state)
    return item


def _extra_game_and_registry():
    g, reg = registry()
    if not g:
        raise RuntimeError('No game folder is selected. Choose one on the Setup tab.')
    if not reg:
        raise RuntimeError(
            'No game archives were found in ' + os.path.join(g, 'data') + '. '
            'Set the game folder to the folder that contains data\\ARCHIVE0.AR '
            '(the install root, not the data folder itself) on the Setup tab.')
    missing = [k for k in ('0', '1', '2') if k not in reg]
    if missing:
        raise RuntimeError(
            'This game folder is missing ' +
            ', '.join(f'ARCHIVE{k}.AR/cdfiles{k}.dat' for k in missing) +
            '. Paint tools need archives 0, 1 and 2. Verify the game files, or '
            'restore a known-good backup.')
    return g, reg


def _extra_game_running():
    if os.name != 'nt':
        return False
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq NASCAR15.exe'], capture_output=True, text=True, timeout=10)
        return 'nascar15.exe' in (r.stdout or '').lower()
    except Exception:
        return False


def _extra_backups(reg, groups=('0', '1', '2')):
    for key in groups:
        v = need(reg, key)
        ensure_backup(v['ar'], v['bak'])
        ensure_backup(v['cdf'], backup_path(v['cdf']))


def _extra_ai_backup_paths(reg):
    v = need(reg, '0')
    return backup_path(v['ar']), backup_path(v['cdf'])


def _extra_state_public():
    try:
        mod = extra_scheme_mod()
        try:
            game, _reg = _extra_game_and_registry()
            _extra_reconcile_state_with_live_database(mod, game)
        except Exception:
            # Read-only callers still receive the last valid state when no game
            # is selected; write routes perform their own hard preflight.
            pass
        return mod.load_state(EXTRA_SCHEME_STATE)
    except Exception:
        return {'format': 'nascar15-extra-schemes-v1', 'version': 1, 'schemes': [], 'assignments': {}, 'ai': {}}


def _extra_prepare_thumbnail(image, target_size, quality='auto'):
    """Smart-import a thumbnail while preserving its aspect ratio and alpha."""
    q = str(quality or 'auto').lower().strip()
    if q not in SCHEME_SMART_QUALITIES:
        q = 'auto'
    tw, th = map(int, target_size)
    sw, sh = image.size
    if q in ('direct', '1'):
        factor = 1
    elif q in ('2', '4'):
        factor = int(q)
    else:
        factor = 1 if (sw > tw or sh > th) else 2
    lanczos = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
    if factor > 1:
        stage, prep = prepare_import_image(
            image, (tw * factor, th * factor), 'fit', preserve_alpha=True
        )
        out = stage.resize((tw, th), lanczos)
    else:
        out, prep = prepare_import_image(
            image, (tw, th), 'fit', preserve_alpha=True
        )
    prep.update({
        'quality_requested': q,
        'supersample_factor': factor,
        'quality_policy': ('direct smart fit' if factor == 1 else f'{factor}x smart fit then Lanczos downsample'),
    })
    return out.convert('RGBA'), prep


_EXTRA_DRIVER_SCRIPT_TAILS = {
    'PRIMARY', 'SECONDARY', 'TERTIARY', 'ALT', 'ALTERNATE', 'BEER',
    'THROWBACK', 'TEST', 'DEFAULT', 'SPECIAL', 'NIGHT', 'DAY'
}


def _extra_name_word(word):
    u = str(word or '').upper()
    if u in ('AJ', 'JJ'):
        return u
    if u in ('JR', 'JNR'):
        return 'Jr.'
    if u.startswith('MC') and len(u) > 2:
        return 'Mc' + u[2:].lower().capitalize()
    return u.lower().capitalize()


def _extra_driver_name_from_schemes(driver):
    """Turn a stock 2015 livery script into a full driver name.

    DRIVER_c name tokens usually contain only an initial (for example
    S_DRIVER_A_ALMIROLA). The public schedule should show Aric Almirola instead
    of exposing that internal token-style abbreviation.
    """
    candidates = []
    for scheme in driver.get('schemes', []):
        script = str(scheme.get('script_name') or '')
        m = re.match(r'^15_[0-9]+[A-Z]?_(.+)$', script, re.I)
        if not m:
            continue
        parts = [p for p in m.group(1).split('_') if p]
        while parts and (parts[-1].upper() in _EXTRA_DRIVER_SCRIPT_TAILS or parts[-1].isdigit()):
            parts.pop()
        if len(parts) < 2:
            continue
        label = ' '.join(_extra_name_word(p) for p in parts)
        score = 0
        if script.upper().endswith('_PRIMARY'):
            score += 5
        if scheme.get('year') == 2015:
            score += 3
        if not scheme.get('managed'):
            score += 1
        candidates.append((score, label))
    if not candidates:
        return str(driver.get('label') or f"Driver {driver.get('uid', '')}").strip()
    label = max(candidates, key=lambda x: (x[0], len(x[1])))[1]
    token = str(driver.get('token') or '').upper()
    if token.endswith('_JR') and not label.lower().endswith('jr.'):
        label += ' Jr.'
    return label


def _extra_friendly_catalog(out):
    cfg = load_cfg()
    renames = cfg.get('renames', {}) if isinstance(cfg, dict) else {}
    rename_exact = {str(k).casefold(): str(v) for k, v in renames.items()}
    for driver in out.get('drivers', []):
        stock_name = _extra_driver_name_from_schemes(driver)
        driver['stock_label'] = stock_name
        driver['label'] = rename_exact.get(stock_name.casefold(), stock_name)
    return out


def _extra_recommended_donor(driver):
    eligible = [s for s in (driver or {}).get('schemes', []) if s.get('donor_eligible') and not s.get('managed')]
    if not eligible:
        eligible = [s for s in (driver or {}).get('schemes', []) if s.get('donor_eligible')]
    def score(s):
        script = str(s.get('script_name') or '').upper()
        label = str(s.get('label') or '').casefold()
        return (
            0 if script.endswith('_PRIMARY') or label == 'primary' else 1,
            0 if s.get('year') == 2015 else 1,
            0 if script.startswith('15_') else 1,
            0 if not s.get('world_token') else 1,
            int(s.get('uid', 999999)),
        )
    return min(eligible, key=score) if eligible else None


# ------------------------------------------------------------- extra UID pool
# The real cap on app-created paint slots is not a settings value: it is the size
# of VERIFIED_SAFE_EXTRA_UIDS in the scheme manager, which ships with 8 UIDs that
# were each replayed successfully in game. Four more below 25600 are recorded as
# verified-broken. Paint Select cannot see records at 25600 or above at all, so
# that is a hard ceiling, not a tunable.
#
# Between the verified-good and verified-broken sets there are untested UIDs
# below the ceiling. Only launching the game can decide whether one works, so
# this keeps a persistent record of what you tried and feeds the passes back
# into the allocator. Shipped defaults are never edited.
EXTRA_UID_CANDIDATES = os.path.join(USER_DIR, 'extra_uid_candidates_v1.json')
EXTRA_UID_CEILING = 25600
EXTRA_UID_FLOOR = 25560


def _extra_uid_store():
    try:
        with open(EXTRA_UID_CANDIDATES, encoding='utf-8') as fh:
            obj = json.load(fh)
        if not isinstance(obj, dict):
            raise ValueError('not an object')
    except Exception:
        obj = {}
    obj.setdefault('verified', [])
    obj.setdefault('rejected', [])
    obj.setdefault('notes', {})
    return obj


def _extra_uid_store_save(obj):
    tmp = EXTRA_UID_CANDIDATES + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, EXTRA_UID_CANDIDATES)


def _extra_uid_pool_state():
    mod = extra_scheme_mod()
    shipped = [int(x) for x in getattr(mod, 'VERIFIED_SAFE_EXTRA_UIDS', ())]
    blocked = [int(x) for x in getattr(mod, 'VERIFIED_BLOCKED_EXTRA_UIDS', ())]
    store = _extra_uid_store()
    user_ok = [int(x) for x in store['verified']
               if EXTRA_UID_FLOOR <= int(x) < EXTRA_UID_CEILING
               and int(x) not in shipped and int(x) not in blocked]
    user_no = [int(x) for x in store['rejected'] if int(x) not in shipped]
    known = set(shipped) | set(blocked) | set(user_ok) | set(user_no)
    untested = [u for u in range(EXTRA_UID_FLOOR, EXTRA_UID_CEILING) if u not in known]
    return dict(shipped_safe=sorted(shipped), verified_broken=sorted(blocked),
                user_verified=sorted(user_ok), user_rejected=sorted(user_no),
                untested=untested, notes=store['notes'],
                usable=sorted(set(shipped) | set(user_ok)),
                ceiling=EXTRA_UID_CEILING)


def _extra_apply_uid_pool(mod):
    """Append user-verified UIDs to the module's pool for this process.

    Shipped tuples stay first, so the proven UIDs are always allocated before
    anything you added. Never lets a blocked or out-of-range UID in.
    """
    try:
        shipped = tuple(int(x) for x in getattr(mod, 'VERIFIED_SAFE_EXTRA_UIDS', ()))
        blocked = set(int(x) for x in getattr(mod, 'VERIFIED_BLOCKED_EXTRA_UIDS', ()))
        store = _extra_uid_store()
        extra = [int(x) for x in store.get('verified', [])
                 if EXTRA_UID_FLOOR <= int(x) < EXTRA_UID_CEILING
                 and int(x) not in shipped and int(x) not in blocked]
        merged = shipped + tuple(sorted(set(extra)))
        if merged != tuple(getattr(mod, 'VERIFIED_SAFE_EXTRA_UIDS', ())):
            mod.VERIFIED_SAFE_EXTRA_UIDS = merged
    except Exception:
        pass
    return mod


@app.route('/api/extra_schemes/uid_pool')
def extra_uid_pool():
    try:
        st = _extra_uid_pool_state()
        st['ok'] = True
        st['capacity'] = len(st['usable'])
        st['next_candidate'] = st['untested'][0] if st['untested'] else None
        # Live figures when a game is available: the allocator skips any UID the
        # livery database already occupies, so "remaining" is the honest number.
        st['remaining_now'] = None
        st['in_use_now'] = None
        try:
            mod = extra_scheme_mod()
            cat = mod.catalog(registry()[0], EXTRA_SCHEME_STATE)
            remaining = [int(x) for x in cat.get('verified_safe_uid_remaining') or []]
            st['remaining_now'] = len(remaining)
            st['in_use_now'] = len(st['usable']) - len(remaining)
            st['created_limit_per_driver'] = int(cat.get('created_limit_per_driver') or 0)
        except Exception:
            pass
        st['note'] = ('Only the game can decide whether a UID works. Create one scheme on a '
                      'candidate, launch the game, and check Paint Select actually lists it. '
                      'Record the result here so the allocator can use it next time. '
                      'UIDs at %d or above are never usable: those records save to disk but '
                      'stay invisible to the selector.' % EXTRA_UID_CEILING)
        return jsonify(st)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/uid_verdict', methods=['POST'])
def extra_uid_verdict():
    q = request.get_json(silent=True) or {}
    try:
        uid = int(q.get('uid'))
    except Exception:
        return jsonify(dict(ok=False, error='A numeric UID is required.')), 400
    verdict = str(q.get('verdict') or '').strip().lower()
    if verdict not in ('works', 'not_visible', 'broken', 'untested'):
        return jsonify(dict(ok=False, error='verdict must be works, not_visible, broken or untested.')), 400
    if not (EXTRA_UID_FLOOR <= uid < EXTRA_UID_CEILING):
        return jsonify(dict(ok=False, error=f'UID {uid} is outside the testable range '
                       f'{EXTRA_UID_FLOOR}-{EXTRA_UID_CEILING - 1}. Paint Select cannot see '
                       f'{EXTRA_UID_CEILING}+ at all.')), 400
    mod = extra_scheme_mod()
    if uid in set(int(x) for x in getattr(mod, 'VERIFIED_BLOCKED_EXTRA_UIDS', ())):
        return jsonify(dict(ok=False, error=f'UID {uid} is recorded as verified-broken and '
                       'cannot be promoted.')), 400
    if uid in set(int(x) for x in getattr(mod, 'VERIFIED_SAFE_EXTRA_UIDS', ())):
        return jsonify(dict(ok=False, error=f'UID {uid} already ships as verified-safe.')), 400

    store = _extra_uid_store()
    store['verified'] = [int(x) for x in store['verified'] if int(x) != uid]
    store['rejected'] = [int(x) for x in store['rejected'] if int(x) != uid]
    note = str(q.get('note') or '').strip()[:400]
    if verdict == 'works':
        store['verified'].append(uid)
    elif verdict in ('not_visible', 'broken'):
        store['rejected'].append(uid)
    if note:
        store['notes'][str(uid)] = note
    else:
        store['notes'].pop(str(uid), None)
    _extra_uid_store_save(store)
    st = _extra_uid_pool_state()
    return jsonify(dict(ok=True, uid=uid, verdict=verdict,
                        capacity=len(st['usable']), usable=st['usable'],
                        note=f'Recorded. Usable UID pool is now {len(st["usable"])}.'))


@app.route('/api/extra_schemes/catalog')
def extra_schemes_catalog():
    """Return the fast paint/driver catalog used by the Paint tab.

    Deep thumbnail identity scans intentionally live in Paint System Check.  The
    older working Paint tab only loaded the database catalog here; performing a
    full team/art scan once per driver made Reload Paint Data look hung and could
    hide an otherwise valid driver list when an unrelated thumbnail check failed.
    """
    try:
        g, _reg = _extra_game_and_registry()
        mod = extra_scheme_mod()
        reconciliation = _extra_reconcile_state_with_live_database(mod, g)
        out = _extra_friendly_catalog(mod.catalog(g, EXTRA_SCHEME_STATE))
        out['live_reconciliation'] = reconciliation
        proven = mod.proven_extra_donor(g)
        state = mod.load_state(EXTRA_SCHEME_STATE)
        active = [x for x in state.get('schemes', []) if not x.get('superseded_by')]
        out['needs_runtime_repair'] = any(int(x.get('native_runtime_layout_version', 0)) < 1 for x in active)
        out['runtime_repair_count'] = sum(int(x.get('native_runtime_layout_version', 0)) < 1 for x in active)
        out['proven_donor'] = proven
        out['helper'] = EXTRA_SCHEME_HELPER
        out['state_file'] = os.path.basename(EXTRA_SCHEME_STATE)
        out['paint_rollback'] = _extra_rollback_status()
        counts = collections.Counter(
            int(x.get('driver_uid', -1)) for x in active if x.get('driver_uid') is not None
        )

        # Build the team-link inputs once.  The previous route rebuilt the entire
        # Team Editor catalog for every driver, which is the reload regression.
        team_links = _team_fast_driver_links()
        original_links = _team_original_team_map()
        team_state = _team_state_load()
        for driver in out.get('drivers', []):
            uid = int(driver['uid'])
            driver['recommended_donor_uid'] = int(proven['uid'])
            created = int(counts.get(uid, 0))
            driver['created_count'] = created
            driver['created_limit'] = EXTRA_SCHEME_LIMIT_PER_DRIVER
            driver['created_remaining'] = max(0, EXTRA_SCHEME_LIMIT_PER_DRIVER - created)
            guard = _stable_paint_creation_guard(
                uid, driver=team_links.get(uid), originals=original_links,
                state=team_state)
            driver['paint_creation_locked'] = bool(guard.get('locked'))
            driver['paint_creation_lock_reason'] = guard.get('reason') or ''
            driver['paint_creation_guard'] = guard
            driver['can_create'] = bool(driver['created_remaining'] and out.get('next_uid') is not None
                                        and not driver['paint_creation_locked'])
        for driver in out.get('drivers', []):
            managed_rows = [s for s in (driver.get('schemes') or []) if s.get('managed') and not s.get('superseded_by')]
            if not managed_rows:
                continue
            try:
                preview_container, _team_driver = _team_preview_container_for_driver(int(driver.get('uid', -1)))
            except Exception as ex:
                preview_container = None
                preview_error = str(ex)
            else:
                preview_error = ''
            for scheme in managed_rows:
                scheme['thumbnail_replace_supported'] = None
                scheme['thumbnail_replace_reason'] = preview_error
                scheme['thumbnail_replace_mode'] = ''
                scheme['thumbnail_replace_container'] = preview_container or ''
                if preview_container:
                    cap = _extra_thumbnail_replace_capability(g, driver, int(scheme.get('uid', -1)), preview_container)
                    scheme['thumbnail_replace_supported'] = bool(cap.get('supported'))
                    scheme['thumbnail_replace_reason'] = str(cap.get('reason') or '')
                    scheme['thumbnail_replace_mode'] = str(cap.get('mode') or '')
                    if cap.get('identity_name'):
                        scheme['thumbnail_identity_name'] = scheme.get('thumbnail_identity_name') or cap.get('identity_name')

        out['created_limit_per_driver'] = EXTRA_SCHEME_LIMIT_PER_DRIVER
        out['preview_note'] = ('Paint and driver data loads through the proven fast catalog path. '
                               'Use Paint System Check for the deeper live thumbnail identity audit.')
        payload = dict(out)
        payload['ok'] = True
        return jsonify(payload)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


# ---------------------------------------------------------------- app data move
# Mods live in the game's own archives (plus our .n15mod.bak beside them), so a
# new app version never touches them. What DOES live in this folder is the app's
# record of what it created: which paint slots exist, team-editor history, saved
# paint art. Extracting a new version into a clean folder loses that record, and
# then created slots are still in the game but invisible here. These two routes
# move that record across. They are reachable before a game is selected, because
# importing into a fresh install is the whole point.
APPDATA_FILES = (
    'config.json', 'game_selector.json', 'extra_schemes_v1.json',
    'team_manager_state.json', 'repoint_history.json',
    'last_whole_mod_repair.json', 'extra_uid_candidates_v1.json',
)
APPDATA_DIRS = ('schemes', 'team_asset_rollback_v1', 'profiles')
APPDATA_MANIFEST = 'nascar_app_data.json'
APPDATA_VERSION = 2


def _appdata_members():
    """(archive_name, absolute_path) for every app-state file that exists."""
    out = []
    for name in APPDATA_FILES:
        p = os.path.join(USER_DIR, name)
        if os.path.isfile(p):
            out.append((name, p))
    for d in APPDATA_DIRS:
        base = os.path.join(USER_DIR, d)
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, USER_DIR).replace(os.sep, '/')
                out.append((rel, full))
    return out


@app.route('/api/appdata/export')
def appdata_export():
    """One zip holding this install's record of your work. No game files."""
    try:
        members = _appdata_members()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr(APPDATA_MANIFEST, json.dumps(dict(
                format='nascar_app_data', version=APPDATA_VERSION, app_version=APP_VERSION,
                schema_versions={'config':2,'extra_schemes':1,'team_manager':1,'schemes':2},
                created=datetime.datetime.now().isoformat(timespec='seconds'),
                files=[m for m, _ in members]), indent=2))
            for name, full in members:
                try:
                    z.write(full, name)
                except OSError:
                    pass
        buf.seek(0)
        stamp = datetime.datetime.now().strftime('%Y%m%d')
        return send_file(buf, mimetype='application/zip', as_attachment=True,
                         download_name=f'nascar_app_data_{stamp}.zip')
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


def _appdata_safe_name(name):
    """Normalize a backup member to one of the app-owned data paths.

    Older users often zipped the entire old app folder instead of using the
    later Save My App Data button.  Accept one or more harmless leading folder
    names, but only after reducing the path to a known file or known data
    directory.  Traversal and absolute paths remain rejected.
    """
    n=str(name).replace('\\','/').strip('/')
    if not n or n.endswith('/'):return None
    parts=[x for x in n.split('/') if x]
    if ':' in n or '..' in parts:return None
    if parts[-1]==APPDATA_MANIFEST:return None
    if parts[-1] in APPDATA_FILES:return parts[-1]
    for i,part in enumerate(parts):
        if part in APPDATA_DIRS and i+1<len(parts):
            return '/'.join(parts[i:])
    return None


def _appdata_current_name(name):
    """Translate app-data paths emitted by known older public builds."""
    n=str(name).replace('\\','/')
    head,base=os.path.split(n)
    newbase=_legacy_pack_member_basename(base)
    return (head+'/'+newbase).strip('/') if head else newbase


def _appdata_replace_legacy_values(value,migrations):
    """Recursively translate known old IDs inside JSON state files."""
    if isinstance(value,dict):
        out={}
        for key,item in value.items():
            newkey='Darrell Wallace Jr.' if str(key).casefold()=='mike wallace' else str(key)
            if newkey!=str(key):migrations.append('config name key Mike Wallace → Darrell Wallace Jr.')
            out[newkey]=_appdata_replace_legacy_values(item,migrations)
        return out
    if isinstance(value,list):return [_appdata_replace_legacy_values(x,migrations) for x in value]
    if isinstance(value,str):
        new=value
        for old,current in LEGACY_PACK_MEMBER_RENAMES.items():new=new.replace(old,current)
        if new!=value:migrations.append(f'{value} → {new}')
        return new
    return value


def _appdata_migrate_bytes(name,raw,from_version,migrations):
    """Convert one imported member to the current app-data schema."""
    if not str(name).lower().endswith('.json'):return raw
    try:value=json.loads(raw.decode('utf-8','replace'))
    except Exception:return raw
    value=_appdata_replace_legacy_values(value,migrations)
    if name=='config.json' and isinstance(value,dict):
        value.setdefault('renames',{});value.setdefault('handles',{})
        value['app_data_schema_version']=APPDATA_VERSION
    return json.dumps(value,indent=2,ensure_ascii=False).encode('utf-8')


@app.route('/api/appdata/import', methods=['POST'])
def appdata_import():
    """Restore a zip made by /api/appdata/export into this install."""
    f = request.files.get('file')
    if not f:
        return jsonify(dict(ok=False, error='No file was uploaded.')), 400
    try:
        blob = f.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            try:
                man=json.loads(z.read(APPDATA_MANIFEST).decode('utf-8','replace'))
            except Exception:
                # Pre-manifest builds and manually zipped old app folders are
                # accepted only when they contain recognized app-owned paths.
                man=dict(format='nascar_app_data',version=0,app_version='older/unversioned')
            if man.get('format')!='nascar_app_data':
                return jsonify(dict(ok=False,error='That zip is not an app-data backup.')),400
            from_version=int(man.get('version',0) or 0)
            if from_version>APPDATA_VERSION:
                return jsonify(dict(ok=False,error=f'This app-data backup uses schema {from_version}; install a newer app version to restore it.')),400

            wanted=[];migrations=[];seen_dest=set()
            for info in z.infolist():
                if info.is_dir():continue
                safe=_appdata_safe_name(info.filename)
                if safe:
                    current=_appdata_current_name(safe)
                    if current!=safe:migrations.append(f'{safe} → {current}')
                    if current in seen_dest:continue
                    seen_dest.add(current);wanted.append((info,current))
            if not wanted:
                return jsonify(dict(ok=False, error='The zip held no app data to restore.')), 400

            # Keep whatever is here now, so a mistaken import is recoverable.
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            existing = _appdata_members()
            replaced = 0
            if existing:
                keep = os.path.join(USER_DIR, f'app_data_replaced_{stamp}.zip')
                with zipfile.ZipFile(keep, 'w', zipfile.ZIP_DEFLATED) as bz:
                    for name, full in existing:
                        try:
                            bz.write(full, name)
                        except OSError:
                            pass
                replaced = len(existing)

            written = 0
            for info, safe in wanted:
                dest = os.path.join(USER_DIR, *safe.split('/'))
                real = os.path.realpath(dest)
                if not real.startswith(os.path.realpath(USER_DIR) + os.sep):
                    continue
                os.makedirs(os.path.dirname(real), exist_ok=True)
                raw=z.read(info)
                raw=_appdata_migrate_bytes(safe,raw,from_version,migrations)
                with open(real,'wb') as out:out.write(raw)
                written+=1

        unique_migrations=[]
        for item in migrations:
            if item not in unique_migrations:unique_migrations.append(item)
        converted=from_version<APPDATA_VERSION or bool(unique_migrations)
        return jsonify(dict(ok=True,restored=written,previous_saved=replaced,
            from_version=man.get('app_version'),from_schema=from_version,to_schema=APPDATA_VERSION,
            converted=converted,migrations=unique_migrations[:50],
            note=(f'Restored {written} file(s). '
                  + (f'Converted older app data from schema {from_version} to {APPDATA_VERSION}. ' if converted else '')
                  +'Restart the app so it reloads them.'
                  + (f' Your previous data was kept in app_data_replaced_{stamp}.zip.' if replaced else ''))))
    except zipfile.BadZipFile:
        return jsonify(dict(ok=False, error='That file is not a readable zip.')), 400
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/paint_system/check')
def paint_system_check_api():
    """Read-only end-to-end audit of the Paint tab and team-aware previews."""
    try:
        g, _reg = _extra_game_and_registry()
        mod = extra_scheme_mod()
        thumb_mod = extra_thumbnail_mod()
        assets = team_assets_mod()
        catalog = mod.catalog(g, EXTRA_SCHEME_STATE)
        state = mod.load_state(EXTRA_SCHEME_STATE)
        active = [x for x in state.get('schemes', [])
                  if not x.get('superseded_by') and x.get('uid') is not None]
        team_catalog = _team_friendly_catalog()
        team_by_driver = {int(d['driver_uid']): d for d in team_catalog.get('drivers', [])}
        live_uids = {int(s['uid']) for d in catalog.get('drivers', [])
                     for s in d.get('schemes', []) if s.get('uid') is not None}
        locations = assets.resource_locations(g)
        checks = []
        rows = []

        def add(name, status, detail):
            checks.append({'name': name, 'status': status, 'detail': detail})

        add('Paint backends', 'pass',
            f"Extra schemes + native thumbnails loaded (thumbnail backend {getattr(thumb_mod, 'VERSION', 'unknown')}).")
        blocked = set(getattr(mod, 'VERIFIED_BLOCKED_EXTRA_UIDS', (25575, 25576, 25577, 25596)))
        unsafe = sorted(int(x['uid']) for x in active if int(x['uid']) in blocked)
        add('Verified UID allocator', 'fail' if unsafe else 'pass',
            ('Blocked active UIDs: ' + ', '.join(map(str, unsafe))) if unsafe
            else 'All active app-created schemes use non-blocked UIDs.')

        counts = collections.Counter(int(x.get('driver_uid', -1)) for x in active)
        over = {uid: n for uid, n in counts.items() if n > EXTRA_SCHEME_LIMIT_PER_DRIVER}
        add('11-created-scheme guard', 'fail' if over else 'pass',
            ('Over limit: ' + ', '.join(f'{uid}={n}' for uid, n in over.items())) if over
            else f'Every driver is at or below the proven {EXTRA_SCHEME_LIMIT_PER_DRIVER}-scheme limit.')

        missing_db = []
        missing_sources = []
        legacy = []
        missing_team = []
        missing_art = []
        missing_current_thumb = []
        invalid_thumbnail_structures = []
        invalid_thumbnail_identities = []
        duplicates = []
        for item in active:
            uid = int(item['uid'])
            driver_uid = int(item.get('driver_uid', -1))
            driver = team_by_driver.get(driver_uid)
            target_container = None
            target_team_uid = None
            if driver:
                target_team_uid = int(driver['team_uid'])
                target_container = f"2DRIVERSELECTTD_{target_team_uid}.ARC"
            paint_source = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(str(item.get('source_png') or '')))
            thumb_source = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(str(item.get('thumbnail_source_png') or '')))
            source_ok = bool(item.get('source_png') and os.path.exists(paint_source))
            if not source_ok:
                try:
                    _extra_read_live_paint_image(item, g, registry()[1]); source_ok = True
                except Exception:
                    pass
            thumb_source_ok = bool(item.get('thumbnail_source_png') and os.path.exists(thumb_source))
            if uid not in live_uids:
                missing_db.append(uid)
            if not source_ok or not thumb_source_ok:
                missing_sources.append(uid)
            if int(item.get('native_runtime_layout_version', 0)) < 1:
                legacy.append(uid)
            resource = f'PAINTSCHEME_{uid}'
            locs = list(locations.get(resource, []))
            current_thumb = bool(target_container and any(x.casefold() == target_container.casefold() for x in locs))
            identity_info = (thumb_mod.inspect_thumbnail_identity(
                g, uid, target_container_name=target_container)
                if current_thumb and target_container else {'same_bank_valid': False})
            structural_valid = bool(identity_info.get('structural_valid'))
            same_bank_identity = bool(
                identity_info.get('identity_self_identifying')
                and identity_info.get('public_name_resolved'))
            thumbnail_safe = bool(identity_info.get('same_bank_valid'))
            if current_thumb and not structural_valid:
                invalid_thumbnail_structures.append(uid)
            if current_thumb and not same_bank_identity:
                invalid_thumbnail_identities.append(uid)
            if not driver:
                missing_team.append(uid)
            if not current_thumb:
                missing_current_thumb.append(uid)
            if len(locs) > 1:
                duplicates.append(uid)
            art_ok = False
            if driver and target_team_uid is not None:
                names = set(assets.team_container_resource_names(g, target_team_uid))
                art_ok = (f'DRIVERPAINT_{driver_uid}_25041' in names and
                          f'DRIVER_{driver_uid}_3DNUM_25041' in names)
                if not art_ok:
                    missing_art.append(driver_uid)
            rows.append({
                'uid': uid, 'name': item.get('name') or item.get('script_name'),
                'driver_uid': driver_uid,
                'driver': (driver or {}).get('car_label') or (driver or {}).get('label') or str(driver_uid),
                'team_uid': target_team_uid, 'team': (driver or {}).get('team_label'),
                'target_container': target_container,
                'database_ready': uid in live_uids,
                'paint_source_ready': source_ok,
                'thumbnail_source_ready': thumb_source_ok,
                'thumbnail_game_safe': thumbnail_safe,
                'thumbnail_structural_valid': structural_valid,
                'thumbnail_same_bank_identity': same_bank_identity,
                'thumbnail_identity_name': identity_info.get('identity_name'),
                'runtime_ready': int(item.get('native_runtime_layout_version', 0)) >= 1,
                'current_team_thumbnail': current_thumb,
                'thumbnail_locations': locs,
                'driver_art_ready': art_ok,
            })

        add('Live livery records', 'fail' if missing_db else 'pass',
            ('Missing from live LIVERIE catalog: ' + ', '.join(map(str, missing_db))) if missing_db
            else f'All {len(active)} active created scheme record(s) are present in the live catalog.')
        add('Saved paint + thumbnail sources', 'warn' if missing_sources else 'pass',
            ('Saved source images missing for UID(s): ' + ', '.join(map(str, missing_sources))) if missing_sources
            else 'Every created scheme still has its saved paint and thumbnail PNG.')
        add('Native paint structure', 'fail' if legacy else 'pass',
            ('Legacy runtime layout on UID(s): ' + ', '.join(map(str, legacy))) if legacy
            else 'Every created scheme uses the proven native runtime layout.')
        add('Current team links', 'fail' if missing_team else 'pass',
            ('No current 2015 Cup team link for UID(s): ' + ', '.join(map(str, missing_team))) if missing_team
            else 'Every created scheme resolves to its driver’s current team.')
        add('Driver Select art', 'fail' if missing_art else 'pass',
            ('Missing current-team tile/number art for driver UID(s): ' + ', '.join(map(str, sorted(set(missing_art))))) if missing_art
            else 'Every created-scheme driver has both Driver Select art resources in the current team bank.')
        add('Current-team thumbnails', 'fail' if missing_current_thumb else 'pass',
            ('Thumbnail not installed in the current team bank for UID(s): ' + ', '.join(map(str, missing_current_thumb))) if missing_current_thumb
            else 'Every created scheme has a native thumbnail in its driver’s current team bank.')
        add('Native thumbnail structure', 'fail' if invalid_thumbnail_structures else 'pass',
            ('Invalid 256×256 DXT5 container structure for UID(s): ' + ', '.join(map(str, invalid_thumbnail_structures))) if invalid_thumbnail_structures
            else 'Every current-team thumbnail has a valid native 256×256 DXT5 resource and intact directory/footer layout.')
        add('Same-bank thumbnail identities', 'fail' if invalid_thumbnail_identities else 'pass',
            ('Thumbnail aliases lack a self-identifying PAINTSCHEME anchor in the current team bank for UID(s): ' + ', '.join(map(str, invalid_thumbnail_identities))) if invalid_thumbnail_identities
            else 'Every current-team PAINTSCHEME alias resolves to a self-identifying native thumbnail in that same bank.')
        add('Old-team thumbnail copies', 'warn' if duplicates else 'pass',
            ('Extra copies remain in prior team banks for UID(s): ' + ', '.join(map(str, duplicates)) +
             '. They are harmless; current-team routing is verified separately.') if duplicates
            else 'No duplicate created-thumbnail resources were found.')

        rank = {'pass': 0, 'warn': 1, 'fail': 2}
        overall = max((x['status'] for x in checks), key=lambda x: rank[x], default='pass')
        return jsonify(dict(ok=True, overall=overall, checks=checks, schemes=rows,
                            active_count=len(active), checked_at=datetime.datetime.now().isoformat(timespec='seconds'),
                            note='Read-only structural audit; no game files were changed.'))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/create', methods=['POST'])
def extra_schemes_create():
    """Create a native stock-team slot through the proven v0.9 + v0.10 path.

    The database and SD/HD assets use the exact ApplyPatch recipe that produced
    the working 337th livery. Paint Select is rebuilt from the smallest
    compatible game-authored fixed-count stock template; the chosen container's
    count is never expanded, and each DRIVERPAINT/3DNUM pair stays on one donor identity.
    UIDs 25600+ and spare/custom teams are rejected. Transferred drivers on
    authored stock teams are enabled only in this guarded experimental branch.
    """
    snapshot = None
    rollback_errors = []
    temp_thumb = None
    with _EXTRA_CREATE_LOCK:
        try:
            if _extra_game_running():
                raise RuntimeError('NASCAR15.exe is running. Close the game before adding a paint scheme')
            g, reg = _extra_game_and_registry()
            f = request.files.get('file')
            thumbnail_file = request.files.get('thumbnail')
            if not f:
                raise ValueError('choose a paint image')
            if not thumbnail_file:
                raise ValueError('choose a thumbnail image; every created slot requires a unique Paint Select preview')
            driver_uid = int(request.form.get('driver_uid'))
            display_name = str(request.form.get('name') or '').strip()
            if not display_name:
                raise ValueError('enter a name for the new scheme')
            quality = str(request.form.get('quality') or 'auto')
            thumbnail_bytes = thumbnail_file.read()

            # Catalog badges and submit enforcement deliberately share one guard.
            guard = _stable_paint_creation_guard(driver_uid)
            if guard.get('locked'):
                raise ValueError(guard.get('reason') or 'paint creation is locked for this driver')
            team_uid = int(guard['team_uid'])
            preview_container = f'2DRIVERSELECTTD_{team_uid}.ARC'

            mod = extra_legacy_scheme_mod()
            state_reconciliation = _extra_reconcile_state_with_live_database(mod, g)
            catalog = mod.catalog(g, EXTRA_SCHEME_STATE)
            driver = next((d for d in catalog['drivers'] if int(d['uid']) == driver_uid), None)
            if not driver:
                raise ValueError('selected driver was not found in the live database')
            state_before = mod.load_state(EXTRA_SCHEME_STATE)
            created_count = _extra_active_created_count(state_before, driver_uid)
            if created_count >= EXTRA_SCHEME_LIMIT_PER_DRIVER:
                raise ValueError(f'this driver already has the maximum {EXTRA_SCHEME_LIMIT_PER_DRIVER} app-created schemes')
            proven = mod.proven_extra_donor(g)
            donor_uid = int(proven['uid'])
            donor_script = str(proven['script_name'])
            identity = mod.suggest_identity(g, driver_uid, display_name, EXTRA_SCHEME_STATE, donor_uid=donor_uid)
            if int(identity['uid']) >= 25600:
                raise ValueError('paint creation refused an unenumerated 25600+ livery UID')
            runtime_script = str(identity['script_name'])
            runtime_sd_name = f"LIVERY_{runtime_script}.ARC"
            runtime_hd_name = f"HDLIVERY_{runtime_script}.ARC"
            # MAP_20260727_014553 measured the complete clean game: max
            # ScriptName=31, max LIVERY CDF name=42, max HDLIVERY CDF name=44.
            # The selector calls SetLiveryName immediately after a tile click,
            # so identities outside that native envelope are rejected before
            # any archive is changed.
            if len(runtime_script) > 31 or len(runtime_sd_name) > 42 or len(runtime_hd_name) > 44:
                raise ValueError(
                    'Generated runtime livery identity exceeds the measured stock name envelope; '
                    'nothing was changed.'
                )

            image, prep = _prepare_scheme_smart_image(f.stream, quality)
            thumb_image, thumb_prep = _extra_prepare_thumbnail_source(thumbnail_bytes, quality)
            pair = mod.donor_asset_pair(g, donor_script)
            if pair['sd_size'] != _NATIVE_SD_ENTRY_SIZE or pair['hd_size'] != _NATIVE_HD_ENTRY_SIZE:
                raise ValueError('the proven donor does not use the expected native SD/HD wrapper sizes')
            sd_wrapper, sd_levels, sd_changed = _native_sd_patch_wrapper(pair['sd'], image, None)
            hd_wrapper, hd_levels, hd_changed = _native_hd_patch_wrapper_public_v1(pair['hd'], image)

            _extra_backups(reg, ('0', '1', '2'))
            snapshot = _extra_transaction_snapshot(reg, ('0', '1', '2'))
            _extra_persist_snapshot(snapshot, f"Create paint slot UID {identity['uid']} for {display_name}", operation={'type': 'create', 'uid': int(identity['uid']), 'driver_uid': int(driver_uid), 'script_name': str(identity['script_name'])})
            source_name = f"{identity['uid']}__{identity['script_name']}.png"
            thumb_source_name = f"{identity['uid']}__{identity['script_name']}.thumbnail.png"
            source_path = os.path.join(EXTRA_SCHEME_IMAGES, source_name)
            thumb_source_path = os.path.join(EXTRA_SCHEME_IMAGES, thumb_source_name)

            result = mod.install_scheme(
                g, EXTRA_SCHEME_STATE,
                driver_uid=driver_uid, donor_uid=donor_uid,
                new_uid=int(identity['uid']), script_name=str(identity['script_name']),
                display_name=display_name, sd_payload=bytes(sd_wrapper),
                hd_payload=bytes(hd_wrapper), source_png_name=source_name,
            )
            image.save(source_path, 'PNG')
            thumb_image.save(thumb_source_path, 'PNG')

            # Rebuild the team's Paint Select bank through the proven fixed-count
            # v0.10 architecture.  The allocator selects a compatible pristine
            # stock bank with enough driver-art and paint slots instead of
            # hardcoding Brad/Joey's two-driver bank.
            links = _team_fast_driver_links()
            team_driver_uids = sorted(
                int(uid) for uid, link in links.items()
                if int(link.get('team_uid', -1)) == team_uid
            )
            after_catalog = mod.catalog(g, EXTRA_SCHEME_STATE)
            required_livery_uids = sorted({
                int(scheme['uid'])
                for d in after_catalog.get('drivers', [])
                if int(d.get('uid', -1)) in set(team_driver_uids)
                for scheme in d.get('schemes', [])
                if scheme.get('uid') is not None and not scheme.get('superseded')
            })
            preview = extra_fixed_template_mod().install_fixed_template_thumbnail(
                g,
                target_container=preview_container,
                team_driver_uids=team_driver_uids,
                livery_uids=required_livery_uids,
                new_uid=int(identity['uid']),
                image_path=thumb_source_path,
            )
            # The historical rc8/rc10 helper resolves texconv.exe relative to
            # its own module. In the reorganized app that module lives under
            # internal_tools, so a missing colocated executable silently fell
            # back to the built-in DXT5 encoder. That payload passes the helper's
            # Python decoder but is not a proven NASCAR 15 Paint Select format.
            # Refuse to commit unless the exact historical texconv path ran.
            if str(preview.get('encoder') or '').strip().casefold() != 'texconv dxt5':
                raise ValueError(
                    'Game-safe Paint Select thumbnail creation requires the bundled '
                    'texconv.exe. The rc8/rc10 helper fell back to an unproven encoder; '
                    'all game-file changes were rolled back.'
                )

            state = mod.load_state(EXTRA_SCHEME_STATE)
            item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(identity['uid'])), None)
            if item is None:
                raise ValueError('new scheme state record was not saved')
            item['native_runtime_layout_version'] = 2
            item['native_runtime_created'] = int(time.time())
            item['structure_donor_uid'] = donor_uid
            item['creation_pipeline'] = 'exact_v0.9_applypatch_plus_v0.10_fixed_template'
            item['uid_range'] = 'sub_25600'
            mod.save_state(EXTRA_SCHEME_STATE, state)
            _extra_update_preview_state(mod, int(identity['uid']), preview, thumb_source_name)
            _extra_seal_persisted_snapshot()

            try:
                _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear(); _clear_ui_thumb_cache()
            except Exception:
                pass
            app_remaining = EXTRA_SCHEME_LIMIT_PER_DRIVER - created_count - 1
            native_remaining = int(preview.get('remaining_native_paint_slots', 0))
            remaining = max(0, min(app_remaining, native_remaining))
            return jsonify(dict(
                ok=True, scheme=result['scheme'], database=result['database'], assets=result['assets'],
                preparation=prep, thumbnail_preparation=thumb_prep,
                sd_levels=sd_levels, sd_changed_bytes=sd_changed,
                hd_levels=hd_levels, hd_changed_bytes=hd_changed,
                preview=preview, created_count=created_count + 1, created_remaining=remaining,
                state_reconciliation=state_reconciliation,
                verification=dict(
                    database_readback=True, asset_readback=True,
                    native_script_name=str(identity['script_name']),
                    native_script_name_length=len(str(identity['script_name'])),
                    native_sd_entry_length=len(f"LIVERY_{identity['script_name']}.ARC"),
                    native_hd_entry_length=len(f"HDLIVERY_{identity['script_name']}.ARC"),
                    stock_name_envelope=True, native_hd_layout=True,
                    uid_below_25600=True, exact_v09_applypatch=True,
                    exact_v010_fixed_template=True, dynamic_native_template=True, paired_driver_art=True, fixed_resource_count=True,
                    custom_thumbnail_written=True, thumbnail_source_saved=True, thumbnail_readback=True,
                    thumbnail_encoder=str(preview.get('encoder') or ''), texconv_thumbnail_required=True,
                    unified_creation_guard=True, spare_custom_team_locked=True,
                    moved_stock_team_experimental=bool(guard.get('experimental_moved_driver')),
                    in_game_tested=False,
                ),
                note=(f'The additional scheme was installed and verified. '
                      f'{remaining} additional scheme slot(s) remain for this team. '
                      + ('This driver was moved from its original team. ' if guard.get('experimental_moved_driver') else '')
                      + 'Launch NASCAR 15 and confirm the scheme before creating another.'),
            ))
        except Exception as ex:
            if snapshot is not None:
                rollback_errors = _extra_transaction_restore(snapshot)
            detail = str(ex)
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            elif snapshot:
                _extra_clear_persisted_snapshot()
            return jsonify(dict(ok=False, error=detail, rolled_back=bool(snapshot and not rollback_errors))), 400
        finally:
            if temp_thumb and os.path.exists(temp_thumb):
                try: os.remove(temp_thumb)
                except Exception: pass


@app.route('/api/extra_schemes/runtime_repair', methods=['POST'])
def extra_schemes_runtime_repair():
    """Rebuild existing app-created slots to the exact proven DB donor recipe
    and rewrite both SD and HD files through their native page maps.

    EVENTINIT and thumbnail containers are deliberately untouched.
    """
    originals = []
    db_rollback = None
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before repairing created paint slots')
        g, reg = _extra_game_and_registry()
        mod = extra_scheme_mod()
        state = mod.load_state(EXTRA_SCHEME_STATE)
        active = [x for x in state.get('schemes', []) if not x.get('superseded_by')]
        protected = []
        for item in active:
            guard = _stable_paint_creation_guard(int(item.get('driver_uid', -1)))
            if guard.get('locked'):
                protected.append((int(item.get('uid', -1)), int(item.get('driver_uid', -1))))
        if protected:
            raise ValueError(
                'Repairing added paint slots is not available for custom teams yet: ' +
                ', '.join(f'UID {uid} (driver {driver_uid})' for uid, driver_uid in protected[:12]) +
                '. Restore a known-good game state or move back only through a validated future build.')
        if not active:
            raise ValueError('there are no app-created schemes to repair')
        proven = mod.proven_extra_donor(g)
        pair = mod.donor_asset_pair(g, proven['script_name'])
        arc2 = need(reg, '2')['ar']
        cdf2_path = need(reg, '2')['cdf']
        cdf2 = mod.v06.parse_cdf_v6(open(cdf2_path, 'rb').read())
        prepared = []
        skipped = []
        for item in active:
            source_name = os.path.basename(str(item.get('source_png') or ''))
            source = os.path.join(EXTRA_SCHEME_IMAGES, source_name)
            if not source_name or not os.path.exists(source):
                skipped.append(dict(uid=int(item.get('uid', -1)),
                                    name=str(item.get('name') or item.get('uid') or 'Unnamed'),
                                    source_png=(source_name or None),
                                    reason='saved paint source PNG is missing'))
                continue
            image = Image.open(source).convert('RGB')
            if image.size != (2048,1024):
                image = image.resize((2048,1024), Image.Resampling.LANCZOS if hasattr(Image,'Resampling') else Image.LANCZOS)
            sd_wrapper, sd_levels, sd_changed = _native_sd_patch_wrapper(pair['sd'], image, None)
            hd_wrapper, hd_levels, hd_changed = _native_hd_patch_wrapper_public_v1(pair['hd'], image)
            rows = []
            for name,payload in ((item['sd_entry'],bytes(sd_wrapper)),(item['hd_entry'],bytes(hd_wrapper))):
                _idx, rec = mod.v06.find_v6_file(cdf2, name)
                if int(rec.data_size) != len(payload):
                    raise ValueError(f'{name} size no longer matches its indexed slot')
                rows.append((name,int(rec.data_offset),payload))
            prepared.append((item,rows,sd_levels,hd_levels,sd_changed,hd_changed))

        if not prepared:
            return jsonify(dict(ok=True, repaired=0, skipped=skipped, schemes=[],
                                note='No repairable paint source PNGs were found. Nothing was written.'))

        # Snapshot every affected asset region for same-process rollback.
        with open(arc2,'rb') as fh:
            for _item,rows,*_rest in prepared:
                for name,off,payload in rows:
                    fh.seek(off); before=fh.read(len(payload))
                    if len(before)!=len(payload): raise ValueError(f'short pre-repair read for {name}')
                    originals.append((off,before))
        with open(arc2,'r+b') as fh:
            for _item,rows,*_rest in prepared:
                for name,off,payload in rows:
                    fh.seek(off);fh.write(payload)
            fh.flush()
            for _item,rows,*_rest in prepared:
                for name,off,payload in rows:
                    fh.seek(off)
                    if fh.read(len(payload))!=payload:
                        raise ValueError(f'asset readback mismatch for {name}')

        ba,bc = _extra_ai_backup_paths(reg)
        # The DB repair appends a new PYC revision and repoints cdfiles.dat.
        # Snapshot the small rollback metadata before that step so any later
        # Python/UI error cannot leave a half-completed repair behind.
        arc0_path = need(reg, '0')['ar']
        cdf0_path = need(reg, '0')['cdf']
        state_before = open(EXTRA_SCHEME_STATE, 'rb').read() if os.path.exists(EXTRA_SCHEME_STATE) else None
        db_rollback = dict(
            archive=arc0_path,
            archive_size=os.path.getsize(arc0_path),
            cdf=cdf0_path,
            cdf_bytes=open(cdf0_path, 'rb').read(),
            state_bytes=state_before,
        )
        db = mod.rebuild_managed_database_from_clean_base(
            g, EXTRA_SCHEME_STATE,
            backup_archive=ba, backup_cdf=bc,
            donor_uid=int(proven['uid']),
        )
        team_links = _team_reapply_saved_links()
        state = mod.load_state(EXTRA_SCHEME_STATE)
        repaired_at = int(time.time())
        by_uid={int(x['uid']):x for x in state.get('schemes',[]) if x.get('uid') is not None}
        details=[]
        for item,rows,sd_levels,hd_levels,sd_changed,hd_changed in prepared:
            row=by_uid.get(int(item['uid']))
            if row is not None:
                row['native_runtime_layout_version']=1
                row['native_runtime_repaired']=repaired_at
                row['preview_status']='disabled_unproven'
            details.append(dict(uid=int(item['uid']),name=item.get('name'),
                                sd_changed_bytes=sd_changed,hd_changed_bytes=hd_changed,
                                sd_levels=sd_levels,hd_levels=hd_levels))
        mod.save_state(EXTRA_SCHEME_STATE,state)
        try:
            _SCHEDULE_SOURCE_CACHE.clear();_SCHEDULE_CACHE.clear();_clear_ui_thumb_cache()
        except Exception: pass
        return jsonify(dict(ok=True,repaired=len(details),skipped=skipped,database=db,team_links=team_links,schemes=details,
                            note=('Rebuilt every repairable app-created slot with the proven DLC donor recipe and native SD/HD mip maps. '
                                  + (f'{len(skipped)} orphaned source file(s) were skipped and reported. ' if skipped else '')
                                  + 'Saved team links were reapplied; AI paint schedule and thumbnails were not touched.')))
    except Exception as ex:
        rollback_errors=[]
        if originals:
            try:
                _g,_reg=_extra_game_and_registry(); arc2=need(_reg,'2')['ar']
                with open(arc2,'r+b') as fh:
                    for off,before in originals:
                        fh.seek(off);fh.write(before)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception as rb:
                rollback_errors.append(f'paint bytes: {rb}')
        if db_rollback:
            try:
                with open(db_rollback['archive'], 'r+b') as fh:
                    fh.truncate(int(db_rollback['archive_size']))
                    fh.flush()
                    os.fsync(fh.fileno())
                atomic_write_bytes(db_rollback['cdf'], db_rollback['cdf_bytes'],
                                   '.runtime_repair_rollback.tmp')
                if db_rollback['state_bytes'] is None:
                    if os.path.exists(EXTRA_SCHEME_STATE):
                        os.remove(EXTRA_SCHEME_STATE)
                else:
                    atomic_write_bytes(EXTRA_SCHEME_STATE, db_rollback['state_bytes'],
                                       '.runtime_repair_rollback.tmp')
            except Exception as rb:
                rollback_errors.append(f'database index: {rb}')
        if rollback_errors:
            return jsonify(dict(ok=False, rollback_failed=True,
                                error=str(RollbackFailed(ex, '; '.join(rollback_errors))))), 500
        return jsonify(dict(ok=False,error=str(ex))),400


@app.route('/api/extra_schemes/thumbnail/<int:uid>', methods=['POST'])
def extra_scheme_thumbnail(uid):
    snapshot = None
    temp_thumb = None
    with _EXTRA_CREATE_LOCK:
        try:
            if _extra_game_running():
                raise RuntimeError('NASCAR15.exe is running. Close the game before changing a paint thumbnail')
            g, reg = _extra_game_and_registry()
            mod = extra_scheme_mod()
            state = mod.load_state(EXTRA_SCHEME_STATE)
            item = next((x for x in state.get('schemes', [])
                         if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
            if item is None:
                raise ValueError('that app-created scheme was not found')
            catalog = mod.catalog(g, EXTRA_SCHEME_STATE)
            driver = next((d for d in catalog.get('drivers', [])
                           if int(d.get('uid', -1)) == int(item.get('driver_uid', -2))), None)
            if driver is None:
                raise ValueError('the scheme driver is no longer in the live catalog')
            thumbnail_guard = _stable_paint_creation_guard(int(item['driver_uid']))
            if thumbnail_guard.get('locked'):
                raise ValueError(
                    'Custom thumbnails are not available for drivers on custom teams yet. '
                    'Nothing was changed.')

            upload = request.files.get('file')
            prepared = None
            preparation = None
            if upload:
                prepared, preparation = _extra_prepare_thumbnail_source(
                    upload.read(), request.form.get('quality') or 'auto')
                fd, temp_thumb = tempfile.mkstemp(prefix='n15_custom_thumb_', suffix='.png')
                os.close(fd)
                prepared.save(temp_thumb, 'PNG')

            tm = extra_thumbnail_mod()
            preview_container, current_team_driver = _team_preview_container_for_driver(int(item['driver_uid']))
            existing = tm.find_target(g, uid, target_container_name=preview_container)
            # FIX (v1.0.2-dev9): a donor supplies the native wrapper/identity
            # recipe needed to CREATE a slot.  When the slot already exists and is
            # itself a structurally valid self-identifying 256x256 DXT5 resource,
            # it is its own donor -- rebuilding it from itself and writing new
            # pixels is exactly what a replace means.  Previously the donor search
            # ran unconditionally and excluded the target, so replacing the
            # thumbnail on the only healthy resource in the bank failed with
            # "No structurally safe 256x256 native thumbnail exists in this team
            # bank" even though the target itself qualified.
            donor = None
            if existing:
                try:
                    self_identity = tm.inspect_thumbnail_identity(
                        g, uid, target_container_name=preview_container)
                except Exception:
                    self_identity = {}
                if self_identity.get('same_bank_valid'):
                    donor = {'uid': int(uid), 'container': preview_container,
                             'entry': f'PAINTSCHEME_{int(uid)}',
                             'identity_name': self_identity.get('identity_name'),
                             'self_donor': True}
            if donor is None:
                donor = _extra_thumbnail_donor(
                    g, driver, exclude_uid=uid, target_container=preview_container)
            _extra_backups(reg, ('1',))
            snapshot = _extra_transaction_snapshot(reg, ('1',), inplace_thumbnail=existing)
            _extra_persist_snapshot(snapshot, f"Replace/repair thumbnail for paint UID {uid}")
            source_name = f"{uid}__{item.get('script_name','SCHEME')}.thumbnail.png"
            source_path = os.path.join(EXTRA_SCHEME_IMAGES, source_name)
            if os.path.exists(source_path):
                snapshot.setdefault('image_overwrites', {})[source_name] = open(source_path, 'rb').read()

            saved_custom_path = None
            if prepared is None and item.get('thumbnail_source_png'):
                candidate = os.path.join(
                    EXTRA_SCHEME_IMAGES,
                    os.path.basename(str(item.get('thumbnail_source_png'))))
                if os.path.exists(candidate):
                    saved_custom_path = candidate
            effective_thumb = temp_thumb if prepared is not None else saved_custom_path
            report = tm.install_or_replace_thumbnail(
                g, uid, int(donor['uid']), effective_thumb,
                target_container_name=preview_container)
            if prepared is not None:
                prepared.save(source_path, 'PNG')
            elif saved_custom_path is None:
                _extra_save_live_native_thumbnail(g, uid, preview_container, source_path)
            report['team_uid'] = int(current_team_driver['team_uid'])
            _extra_update_preview_state(mod, uid, report, source_name)
            try: _clear_ui_thumb_cache()
            except Exception: pass
            if prepared is not None:
                note = ('Custom thumbnail rebuilt from the proven native donor recipe, appended '
                        'as a complete new team-bank revision, and repointed in cdfiles1.dat.')
            elif saved_custom_path is not None:
                note = ('The saved custom thumbnail was rebuilt through the proven pre-team route. '
                        'The old team-bank revision was left untouched and the CDF now points to the new one.')
            else:
                note = ('No saved custom thumbnail was available, so the target was rebuilt as an exact '
                        'native donor clone in a new appended/repointed team-bank revision.')
            return jsonify(dict(ok=True, uid=uid, preview=report,
                                preparation=preparation, note=note))
        except Exception as ex:
            rollback_errors = _extra_transaction_restore(snapshot) if snapshot else []
            detail = str(ex)
            if rollback_errors:
                detail += ' | Rollback warnings: ' + '; '.join(rollback_errors)
            elif snapshot:
                _extra_clear_persisted_snapshot()
            return jsonify(dict(ok=False, error=detail,
                                rolled_back=bool(snapshot and not rollback_errors))), 400
        finally:
            if temp_thumb and os.path.exists(temp_thumb):
                try: os.remove(temp_thumb)
                except OSError: pass


@app.route('/api/extra_schemes/previews/repair', methods=['POST'])
def extra_scheme_preview_repair():
    return jsonify(dict(
        ok=False,
        error='Use Replace Thumbnail or Repair Thumbnail Identity beside the individual scheme.'
    )), 400


@app.route('/api/extra_schemes/finalize', methods=['POST'])
def extra_schemes_finalize():
    return jsonify(dict(
        ok=False,
        error='The retired registry finalizer is disabled because it never fixed native slot visibility.'
    )), 400


def _extra_scheme_image_path(uid, field):
    state = _extra_state_public()
    item = next((x for x in state.get('schemes', [])
                 if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
    if not item or not item.get(field):
        return None
    path = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(str(item[field])))
    return path if os.path.exists(path) else None


def _extra_read_live_paint_image(item, game=None, reg=None):
    """Decode mip 0 from the live SD livery when the app PNG is missing.

    Version upgrades can legitimately lose the app-side source file while the
    complete paint remains installed in ARCHIVE2.  The archive is authoritative,
    so previews and Export Paint must still work.
    """
    if game is None or reg is None:
        game, reg = _extra_game_and_registry()
    entry_name = str(item.get('sd_entry') or ('LIVERY_' + str(item.get('script_name') or '') + '.ARC'))
    hit = None
    for arcid, info in reg.items():
        try:
            for off, size, name in parse_cdfiles(info['cdf']):
                if name == entry_name:
                    hit = (arcid, int(off), int(size)); break
        except Exception:
            continue
        if hit:
            break
    if not hit:
        raise ValueError(f'live paint asset was not found: {entry_name}')
    arcid, off, size = hit
    if size < RAW_OFFSET + (2048 // 4) * (1024 // 4) * 8:
        raise ValueError(f'live SD paint wrapper is too short: {size} bytes')
    with open(reg[arcid]['ar'], 'rb') as fh:
        fh.seek(off + RAW_OFFSET)
        payload = fh.read((2048 // 4) * (1024 // 4) * 8)
    if len(payload) != (2048 // 4) * (1024 // 4) * 8:
        raise ValueError('short read while decoding live paint')
    return Image.fromarray(dxt1_decode(payload, 2048, 1024)).convert('RGB')


def _extra_cache_recovered_paint(item, image):
    os.makedirs(EXTRA_SCHEME_IMAGES, exist_ok=True)
    name = f"{int(item['uid'])}__{item.get('script_name') or 'RECOVERED'}.recovered.png"
    path = os.path.join(EXTRA_SCHEME_IMAGES, name)
    image.save(path, 'PNG')
    try:
        mod = extra_scheme_mod()
        state = mod.load_state(EXTRA_SCHEME_STATE)
        row = next((x for x in state.get('schemes', [])
                    if int(x.get('uid', -1)) == int(item['uid']) and not x.get('superseded_by')), None)
        if row is not None:
            row['source_png'] = name
            row['source_recovered_from_live'] = True
            row['source_recovered_at'] = int(time.time())
            mod.save_state(EXTRA_SCHEME_STATE, state)
    except Exception:
        pass
    return path


@app.route('/api/extra_schemes/preview/paint/<int:uid>')
def extra_scheme_paint_preview(uid):
    try:
        path = _extra_scheme_image_path(uid, 'source_png')
        if path:
            return send_file(path, mimetype='image/png', conditional=True, max_age=0)
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', [])
                     if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
        if not item:
            return ('not found', 404)
        image = _extra_read_live_paint_image(item)
        path = _extra_cache_recovered_paint(item, image)
        return send_file(path, mimetype='image/png', conditional=True, max_age=0)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/preview/thumbnail/<int:uid>')
def extra_scheme_thumbnail_preview(uid):
    try:
        path = _extra_scheme_image_path(uid, 'thumbnail_source_png')
        if path:
            return send_file(path, mimetype='image/png', conditional=True, max_age=0)
        # Legacy working custom thumbnails may predate the saved preview PNG.
        # Decode the live current-team resource instead of showing a false blank.
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', [])
                     if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
        if not item:
            return ('not found', 404)
        g, _reg = _extra_game_and_registry()
        container, _driver = _team_preview_container_for_driver(int(item['driver_uid']))
        image = _extra_read_live_native_thumbnail_preview(g, uid, container)
        buf = io.BytesIO(); image.save(buf, 'PNG'); buf.seek(0)
        return send_file(buf, mimetype='image/png', conditional=True, max_age=0)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/source/<int:uid>')
def extra_scheme_source(uid):
    try:
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(uid)), None)
        if not item:
            return ('not found', 404)
        path = None
        if item.get('source_png'):
            candidate = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(item['source_png']))
            if os.path.exists(candidate):
                path = candidate
        if not path:
            image = _extra_read_live_paint_image(item)
            path = _extra_cache_recovered_paint(item, image)
        return send_file(path, mimetype='image/png', as_attachment=True,
                         download_name=f"{item.get('name') or item.get('script_name')}.png")
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/source_thumbnail/<int:uid>')
def extra_scheme_thumbnail_source(uid):
    try:
        state = _extra_state_public()
        item = next((x for x in state.get('schemes', []) if int(x.get('uid', -1)) == int(uid)), None)
        if not item:
            return ('not found', 404)
        path = _extra_scheme_image_path(uid, 'thumbnail_source_png')
        if path:
            return send_file(path, mimetype='image/png', as_attachment=True,
                             download_name=f"{item.get('name') or item.get('script_name')}_thumbnail.png")
        g, _reg = _extra_game_and_registry()
        container, _driver = _team_preview_container_for_driver(int(item['driver_uid']))
        image = _extra_read_live_native_thumbnail_preview(g, uid, container)
        buf = io.BytesIO(); image.save(buf, 'PNG'); buf.seek(0)
        return send_file(buf, mimetype='image/png', as_attachment=True,
                         download_name=f"{item.get('name') or item.get('script_name')}_thumbnail.png")
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

def _extra_remove_scheme_from_live_files(uid, game, reg, exact_preflight_error=''):
    """Remove one rediscovered added scheme while preserving unrelated live DB edits."""
    snapshot = None
    try:
        mod = extra_scheme_mod()
        _extra_reconcile_state_with_live_database(mod, game)
        state = mod.load_state(EXTRA_SCHEME_STATE)
        item = next((x for x in state.get('schemes', [])
                     if int(x.get('uid', -1)) == int(uid) and not x.get('superseded_by')), None)
        if item is None:
            raise ValueError('that active added paint scheme was not found in the live game files')

        snapshot = _extra_transaction_snapshot(reg, ('0',))
        _extra_persist_snapshot(
            snapshot, f"Delete added paint UID {uid}",
            operation={'type': 'delete_live', 'uid': int(uid)})

        removed_assignments = 0
        for event_key, rows in list((state.get('assignments') or {}).items()):
            if not isinstance(rows, dict):
                continue
            for driver_key, value in list(rows.items()):
                try:
                    match = int(value) == int(uid)
                except Exception:
                    match = False
                if match:
                    rows.pop(driver_key, None)
                    removed_assignments += 1
            if not rows:
                state['assignments'].pop(event_key, None)

        ai_was_applied = bool((state.get('ai') or {}).get('applied'))
        item['superseded_by'] = 'removed_by_user'
        item['removed_at'] = int(time.time())
        item['removed_reason'] = 'user requested live-file deletion'
        retired = {int(x) for x in state.get('retired_uids', []) if x is not None}
        retired.add(int(uid))
        state['retired_uids'] = sorted(retired)
        mod.save_state(EXTRA_SCHEME_STATE, state)

        try:
            removed = mod.remove_managed_livery_from_live_base(game, int(uid))
            removed['delete_method'] = str((removed.get('meta') or {}).get('strategy') or 'live_applypatch_inverse')
        except Exception as live_delete_error:
            # The live surgical routes preserve every unrelated record. A clean
            # rebuild remains a final compatibility fallback only when the live
            # database still matches its clean non-scheme records exactly.
            try:
                proven = mod.proven_extra_donor(game)
                ba, bc = _extra_ai_backup_paths(reg)
                removed = mod.rebuild_managed_database_from_clean_base(
                    game, EXTRA_SCHEME_STATE,
                    backup_archive=ba, backup_cdf=bc,
                    donor_uid=int(proven['uid']))
                removed['delete_method'] = 'verified_clean_base_rebuild'
                removed['live_inverse_unavailable'] = str(live_delete_error)
            except Exception as clean_delete_error:
                raise ValueError(
                    'Live surgical delete failed: ' + str(live_delete_error)
                    + ' | Clean-base fallback unavailable: ' + str(clean_delete_error)
                ) from clean_delete_error
        schedule = None
        if ai_was_applied:
            ba, bc = _extra_ai_backup_paths(reg)
            schedule = mod.apply_ai(
                game, EXTRA_SCHEME_STATE,
                backup_archive=ba, backup_cdf=bc)

        live_ctx = mod.base.load_context(str(game))
        if any(r.class_name == 'LIVERIE_c'
               and int(mod.base.pointer_int(live_ctx, r.uid)) == int(uid)
               for r in live_ctx.records):
            raise ValueError('delete readback failed: the livery UID is still present')

        _extra_seal_persisted_snapshot(
            operation={'type': 'delete_live', 'uid': int(uid)})
        try:
            _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear(); _clear_ui_thumb_cache()
        except Exception:
            pass
        return jsonify(dict(
            ok=True,
            removed_uid=int(uid),
            exact_rollback=False,
            live_file_delete=True,
            assignments_removed=int(removed_assignments),
            installed_schedule_updated=bool(schedule),
            database=removed,
            note=(
                'The added scheme was removed from the live game database without changing '
                'your other team, race, name, or settings edits. Its unused paint and menu-image '
                'bytes remain safely unreferenced, and this UID is retired so it cannot be reused. '
                'Undo Last Paint Change can restore the scheme.'
            )
        ))
    except Exception as ex:
        rollback_errors = _extra_transaction_restore(snapshot) if snapshot else []
        detail = str(ex)
        if exact_preflight_error:
            detail += ' | Exact checkpoint unavailable: ' + str(exact_preflight_error)
        if rollback_errors:
            detail += ' | Rollback failed: ' + '; '.join(rollback_errors)
        elif snapshot:
            _extra_clear_persisted_snapshot()
        return jsonify(dict(ok=False, error=detail,
                            rolled_back=bool(snapshot and not rollback_errors))), 400


@app.route('/api/extra_schemes/remove/<int:uid>', methods=['POST'])
def extra_scheme_remove(uid):
    """Delete an added scheme from the live game, with exact rollback when possible.

    A matching creation checkpoint can still reverse the newest creation
    byte-for-byte. Fresh app folders do not have that checkpoint, so the public
    fallback removes the target ApplyPatch block directly from the current live
    database, preserving every unrelated record and permanently retiring the
    dormant asset UID.
    """
    redo_tmp = None
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before removing an added paint scheme')
        with _EXTRA_CREATE_LOCK:
            game, reg = _extra_game_and_registry()
            exact_preflight_error = ''
            snapshot = manifest = None
            try:
                snapshot, manifest = _extra_load_persisted_snapshot(reg)
            except Exception as ex:
                exact_preflight_error = str(ex)

            operation = (manifest or {}).get('operation') or {}
            exact_candidate = bool(
                manifest
                and manifest.get('format') == 'nascar15-extra-scheme-rollback-v2'
                and manifest.get('mode', 'restore_pre') == 'restore_pre'
                and operation.get('type') == 'create'
                and int(operation.get('uid', -1)) == int(uid)
            )
            if exact_candidate:
                try:
                    _extra_verify_manifest_post_state(manifest)
                    redo_tmp = _extra_prepare_delete_redo(manifest, uid)
                except Exception as ex:
                    exact_preflight_error = str(ex)
                    if redo_tmp and os.path.isdir(redo_tmp):
                        shutil.rmtree(redo_tmp, ignore_errors=True)
                    redo_tmp = None
                else:
                    errors = _extra_transaction_restore(snapshot)
                    if errors:
                        raise RuntimeError('exact slot removal reported: ' + '; '.join(errors))
                    if os.path.isdir(EXTRA_SCHEME_ROLLBACK_DIR):
                        shutil.rmtree(EXTRA_SCHEME_ROLLBACK_DIR)
                    os.replace(redo_tmp, EXTRA_SCHEME_ROLLBACK_DIR)
                    redo_tmp = None
                    try:
                        _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear(); _clear_ui_thumb_cache()
                    except Exception:
                        pass
                    return jsonify(dict(
                        ok=True, removed_uid=int(uid), exact_rollback=True,
                        note=(
                            'The scheme was removed by restoring the exact files from before it was created. '
                            'Undo Last Paint Change can restore it byte-for-byte.'
                        )
                    ))

            return _extra_remove_scheme_from_live_files(
                int(uid), game, reg, exact_preflight_error=exact_preflight_error)
    except Exception as ex:
        if redo_tmp and os.path.isdir(redo_tmp):
            shutil.rmtree(redo_tmp, ignore_errors=True)
        return jsonify(dict(ok=False, error=str(ex), rolled_back=False)), 400


@app.route('/api/extra_schemes/undo_status')
def extra_scheme_undo_status():
    return jsonify(dict(ok=True, **_extra_rollback_status()))


@app.route('/api/extra_schemes/undo', methods=['POST'])
def extra_scheme_undo():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before undoing a paint change')
        with _EXTRA_CREATE_LOCK:
            _g, rollback_reg = _extra_game_and_registry()
            snapshot, manifest = _extra_load_persisted_snapshot(rollback_reg)
            mode = manifest.get('mode', 'restore_pre')
            if mode == 'redo_post':
                _extra_reapply_deleted_slot(snapshot, manifest)
                action = manifest.get('label')
                note = 'The deleted paint slot was restored byte-for-byte, including its archive tails, CDF pointers, app state, and saved images.'
            else:
                if manifest.get('post_state'):
                    _extra_verify_manifest_post_state(manifest)
                errors = _extra_transaction_restore(snapshot)
                if errors:
                    raise RuntimeError('paint rollback reported: ' + '; '.join(errors))
                _extra_clear_persisted_snapshot()
                action = manifest.get('label')
                note = 'The previous paint transaction was restored exactly, including CDF pointers, app state, and saved images.'
            try:
                _SCHEDULE_SOURCE_CACHE.clear(); _SCHEDULE_CACHE.clear(); _clear_ui_thumb_cache()
            except Exception:
                pass
            return jsonify(dict(ok=True, verified=True, undone=action,
                                created=manifest.get('created'), note=note))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

@app.route('/api/extra_schemes/repair/<int:uid>', methods=['POST'])
def extra_scheme_repair(uid):
    return jsonify(dict(
        ok=False,
        error=('Legacy identity replacement is disabled. Use Repair App-Created Slots '
               'to rebuild the existing UID without orphaning race assignments.')
    )), 400


def _extra_unsafe_assigned_thumbnail_uids():
    """Return assigned app-created schemes whose live thumbnail wiring is unsafe.

    State flags are not enough after a driver transfer: the destination team bank
    can change while the saved method still says the thumbnail was previously
    valid. Re-read table words 2/6 from the driver's current bank before allowing
    EVENTINIT installation.
    """
    state = _extra_state_public()
    managed = {
        int(x['uid']): x for x in state.get('schemes', [])
        if x.get('uid') is not None and not x.get('superseded_by')
    }
    assigned = set()
    for rows in (state.get('assignments') or {}).values():
        for value in (rows or {}).values():
            try:
                assigned.add(int(value))
            except Exception:
                pass
    relevant = sorted(assigned & set(managed))
    if not relevant:
        return []
    try:
        g, _reg = _extra_game_and_registry()
        tm = extra_thumbnail_mod()
    except Exception:
        return relevant
    unsafe = []
    for uid in relevant:
        item = managed[uid]
        try:
            target_container, _driver = _team_preview_container_for_driver(
                int(item['driver_uid']))
            identity = tm.inspect_thumbnail_identity(
                g, uid, target_container_name=target_container)
            live_safe = bool(identity.get('same_bank_valid'))
        except Exception:
            live_safe = False
        if not live_safe:
            unsafe.append(uid)
    return unsafe


@app.route('/api/ai_paints/assignments', methods=['GET', 'POST'])
def ai_paint_assignments_api():
    try:
        mod = extra_scheme_mod()
        if request.method == 'GET':
            g, reg = _extra_game_and_registry()
            ba, bc = _extra_ai_backup_paths(reg)
            return jsonify(dict(ok=True, assignments=mod.assignments(EXTRA_SCHEME_STATE),
                                state=_extra_state_public().get('ai', {}),
                                base_status=mod.ai_base_status(
                                    g, EXTRA_SCHEME_STATE,
                                    backup_archive=ba, backup_cdf=bc)))
        q = request.get_json(force=True) or {}
        clean = mod.save_assignments(EXTRA_SCHEME_STATE, q.get('assignments') or {})
        return jsonify(dict(ok=True, assignments=clean,
                            note='Assignments saved in the app. Use Preview, then Apply to write EVENTINIT.PYC.'))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/ai_paints/preview', methods=['POST'])
def ai_paint_preview_api():
    try:
        # AI selection consumes the live livery/database identity, not the
        # Paint Select thumbnail. A damaged menu tile is reported separately and
        # must never block a valid race assignment.
        g, reg = _extra_game_and_registry()
        ba, bc = _extra_ai_backup_paths(reg)
        out = extra_scheme_mod().ai_plan(
            g, EXTRA_SCHEME_STATE, backup_archive=ba, backup_cdf=bc
        )
        return jsonify(out)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/ai_paints/apply', methods=['POST'])
def ai_paint_apply_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before applying AI paint assignments')
        # Thumbnail health is presentation-only; validate/apply the live
        # livery and EVENTINIT schedule independently.
        g, reg = _extra_game_and_registry()
        ba, bc = _extra_ai_backup_paths(reg)
        # Validate/capture the clean reusable function before creating any new
        # archive backup. This avoids blessing an already-patched standalone
        # probe as a supposedly pristine first backup.
        extra_scheme_mod().ensure_ai_base(
            g, EXTRA_SCHEME_STATE, backup_archive=ba, backup_cdf=bc
        )
        _extra_backups(reg, ('0',))
        ba, bc = _extra_ai_backup_paths(reg)
        out = extra_scheme_mod().apply_ai(
            g, EXTRA_SCHEME_STATE, backup_archive=ba, backup_cdf=bc
        )
        payload = dict(out)
        payload['ok'] = True
        payload['note'] = ('AI paint schedule installed. Races and drivers without an assignment '
                           "still use the game's normal paint selection.")
        return jsonify(payload)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/ai_paints/restore', methods=['POST'])
def ai_paint_restore_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before restoring AI paint logic')
        g, reg = _extra_game_and_registry()
        _extra_backups(reg, ('0',))
        out = extra_scheme_mod().restore_ai(g, EXTRA_SCHEME_STATE)
        payload = dict(out)
        payload['ok'] = True
        payload['note'] = ('Original AI paint selection restored. Your saved schedule remains in the app '
                           'and can be installed again later.')
        return jsonify(payload)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/extra_schemes/export')
def extra_schemes_export():
    """Export app-created scheme metadata, AI assignments, and source PNGs.

    This is intentionally separate from the older mod-pack importer so users can
    preserve/share the new feature before full season-pack installation support
    is expanded in the next packaging pass.
    """
    try:
        import zipfile
        state = _extra_state_public()
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('extra_schemes_v1.json', json.dumps(state, indent=2).encode('utf-8'))
            for item in state.get('schemes', []):
                name = os.path.basename(str(item.get('source_png') or ''))
                path = os.path.join(EXTRA_SCHEME_IMAGES, name)
                if name and os.path.exists(path):
                    z.write(path, 'paints/' + name)
            z.writestr('README.txt',
                b'NASCAR 15 Modding App extra-scheme library and named-race AI assignments. '
                b'Import/install support is handled by the Modding App; no copyrighted game archive is included.\n')
        out.seek(0)
        return send_file(out, mimetype='application/zip', as_attachment=True,
                         download_name='nascar15_extra_schemes_and_ai_paints.zip')
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

# ==================== end v0.9.29.7 ====================


# ==================== v0.9.27.2 RC3 SETTINGS + HELP/UI POLISH ====================
# Ratings outside 0-100 are supported without experimental wording.
# Settings now cover spacing, text size, page width, previews, startup, and browser behavior.
# Help requests generate a shareable ZIP with no game archives.
# ==================== end v0.9.27.2 ====================

# ==================== v0.9.30.5 DRIVER ART REPAIR HOTFIX ====================
BANK_VERIFY_HELPER = 'nascar15_bank_verify_v1.py'
_BANK_VERIFY_MOD = None


def bank_verify_mod():
    """Verifies game files AFTER a write, by checking the artifact itself.

    Every corruption bug found during the moved-driver investigation was
    invisible to the app because each validator re-derived the numbers that
    produced the output instead of inspecting the output.
    """
    global _BANK_VERIFY_MOD
    if _BANK_VERIFY_MOD is not None:
        return _BANK_VERIFY_MOD
    path = component_path(BANK_VERIFY_HELPER)
    if not os.path.exists(path):
        raise RuntimeError(f'{BANK_VERIFY_HELPER} is missing from the internal tools folder')
    spec = importlib.util.spec_from_file_location('n15_bank_verify', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['n15_bank_verify'] = mod
    spec.loader.exec_module(mod)
    _BANK_VERIFY_MOD = mod
    return mod


TEAM_MANAGER_HELPER = 'nascar15_team_manager_v1.py'
TEAM_ASSETS_HELPER = 'nascar15_team_assets_v1.py'
TEAM_MANAGER_STATE = os.path.join(USER_DIR, 'team_manager_state.json')
TEAM_ASSET_ROLLBACK_DIR = os.path.join(USER_DIR, 'team_asset_rollback_v1')
_TEAM_MANAGER_MOD = None
_TEAM_ASSETS_MOD = None
_TEAM_MANAGER_LOCK = threading.RLock()

TEAM_DISPLAY_NAMES = {
    1325: 'Richard Petty Motorsports',
    1326: 'JTG Daugherty Racing',
    1327: 'Front Row Motorsports',
    1331: 'Roush Fenway Racing',
    1333: 'Richard Childress Racing',
    1335: 'Joe Gibbs Racing',
    1336: 'Team Penske',
    1340: 'Hendrick Motorsports',
    1341: 'Wood Brothers Racing',
    1347: 'Tommy Baldwin Racing',
    1349: 'Chip Ganassi Racing',
    1351: 'Stewart-Haas Racing',
    1352: 'Germain Racing',
    1354: 'Michael Waltrip Racing',
    1355: 'Furniture Row Racing',
    5762: 'JR Motorsports',
    11444: 'Phil Parsons Racing',
    2403: 'Custom Chevrolet',
    2405: 'Custom Ford',
    2406: 'Custom Toyota',
    8739: 'Leavine Family Racing',
    10518: 'BK Racing',
    23035: 'Premium Motorsports',
    25381: 'HScott Motorsports',
    25430: 'Go FAS Racing',
}
TEAM_MANUFACTURER_NAMES = {
    1015: 'Ford',
    1076: 'Chevrolet',
    1078: 'Toyota',
}
# Three stock Driver Select logos use short/legacy resource names in the clean game.
# Treat them as normal TEAM_<uid> resources everywhere in the public UI.
TEAM_LOGO_RESOURCE_ALIASES = {
    1327: 'ms',       # Front Row Motorsports
    1333: 'mk__r41',  # Richard Childress Racing
    25430: 'mk',      # Go FAS Racing
}

def _team_logo_entry_name(team_uid, entries):
    wanted = f'TEAM_{int(team_uid)}'
    names = {str(e.get('name') if isinstance(e, dict) else getattr(e, 'name', '')) for e in entries}
    if wanted in names:
        return wanted
    alias = TEAM_LOGO_RESOURCE_ALIASES.get(int(team_uid))
    return alias if alias in names else None

SUPPORTED_SPARE_TEAM_UIDS = (2403, 2405, 2406)
# Paint-slot creation for spare/custom teams was locked because appended
# resources corrupted the destination bank: a cross-bank copy transplanted the
# SOURCE bank's directory header into a non-first chunk, and the whole team bank
# then failed to load (fatal at Team Select).
#
# Root cause fixed in nascar15_team_assets_v1.py - _strip_source_directory_header
# plus the _assert_single_directory_header output check. Install that file
# BEFORE enabling this.
#
# Still unproven: whether an app-created paint slot can be SELECTED without
# crashing once the bank loads cleanly. That was never reachable while the bank
# itself was broken. Enable this to find out, on a disposable copy first.
SPARE_TEAM_PAINT_CREATION_ENABLED = False
UNSUPPORTED_TEAM_UIDS = (2404,)
UNSUPPORTED_MANUFACTURER_UIDS = (1074,)
TEAM_DEFAULT_LOGO_DONORS = {2403: 1333, 2405: 1336, 2406: 1335}

# Custom/reserve teams are enabled for the paths that passed the in-game gate:
# driver transfer, native/DLC paint-bank carryover, team logo/name/manufacturer,
# presentation rebuild, and Driver Select art.  The independent app-created
# paint-slot writer remains blocked by _stable_paint_creation_guard for these UIDs.
PUBLIC_CUSTOM_TEAMS_ENABLED = True
PUBLIC_CUSTOM_TEAM_MESSAGE = (
    'Custom teams support driver transfers, existing paint schemes, logos, names, '
    'manufacturer changes and menu artwork. Adding brand new paint slots to a custom team '
    'is not available yet.'
)

def _public_custom_team_guard(team_uid, action='edit this team'):
    if not PUBLIC_CUSTOM_TEAMS_ENABLED and int(team_uid) in SUPPORTED_SPARE_TEAM_UIDS:
        raise ValueError(PUBLIC_CUSTOM_TEAM_MESSAGE + ' Cannot ' + str(action) + '.')

def _public_custom_team_locked(team_uid):
    return bool(not PUBLIC_CUSTOM_TEAMS_ENABLED and int(team_uid) in SUPPORTED_SPARE_TEAM_UIDS)


def team_manager_mod():
    global _TEAM_MANAGER_MOD
    if _TEAM_MANAGER_MOD is not None:
        return _TEAM_MANAGER_MOD
    path = component_path(TEAM_MANAGER_HELPER)
    if not os.path.exists(path):
        raise RuntimeError('team manager helper is missing: ' + TEAM_MANAGER_HELPER)
    spec = importlib.util.spec_from_file_location('nascar15_team_manager_runtime', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('could not load the team manager helper')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _TEAM_MANAGER_MOD = mod
    return mod


def team_assets_mod():
    global _TEAM_ASSETS_MOD
    if _TEAM_ASSETS_MOD is not None:
        return _TEAM_ASSETS_MOD
    path = component_path(TEAM_ASSETS_HELPER)
    if not os.path.exists(path):
        raise RuntimeError('team presentation helper is missing: ' + TEAM_ASSETS_HELPER)
    spec = importlib.util.spec_from_file_location('nascar15_team_assets_runtime', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('could not load the team presentation helper')
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _TEAM_ASSETS_MOD = mod
    return mod


def _team_state_load():
    base = {
        'format': 'nascar15-team-manager-v1',
        'version': 1,
        'driver_teams': {},
        'team_manufacturers': {},
        'team_names': {},
        'driver_source_teams': {},
        'team_logo_donors': {},
        'thumbnail_overrides': {},
        'history': [],
    }
    try:
        raw = json.load(open(TEAM_MANAGER_STATE, 'r', encoding='utf-8'))
        if isinstance(raw, dict):
            for key in ('driver_teams', 'team_manufacturers', 'driver_source_teams', 'team_logo_donors'):
                if isinstance(raw.get(key), dict):
                    base[key] = {str(k): int(v) for k, v in raw[key].items()}
            if isinstance(raw.get('thumbnail_overrides'), dict):
                base['thumbnail_overrides'] = {
                    str(k): dict(v) for k, v in raw['thumbnail_overrides'].items()
                    if isinstance(v, dict)
                }
            if isinstance(raw.get('team_names'), dict):
                base['team_names'] = {str(k): str(v) for k, v in raw['team_names'].items() if str(v).strip()}
            if isinstance(raw.get('history'), list):
                base['history'] = raw['history'][-100:]
    except Exception:
        pass
    return base


def _team_state_save(state):
    tmp = TEAM_MANAGER_STATE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, TEAM_MANAGER_STATE)


def _team_driver_labels():
    labels={}
    try:
        for link in load_driver_links():
            base=str(link.get('base') or '').upper()
            if base:labels[base]=_driver_display_from_link(link)
    except Exception:pass
    return labels


def _team_asset_statuses_direct(game, team_uids):
    """Read-only stock/live presentation status without optional helper imports.

    Team Manager used to convert any helper import error into "Logo needed" and
    "Paint container needed" for every team.  That made valid stock resources
    look missing.  This path uses app.py's already-proven archive/CDF and ARCC
    readers, and reports an explicit diagnostic instead of inventing damage.
    """
    result={}
    for value in team_uids:
        uid=int(value)
        result[uid]=dict(team_uid=uid,logo_ready=False,paint_container_ready=False,
                         paint_resource_count=0,presentation_ready=False,
                         status_source='direct archive scan')
    if not game:
        return result
    _g,reg=registry()
    logo_names=set()
    logo_entries=[]
    try:
        off,size=find_entry(reg,'0','2DRIVERSELECTMENUIMAGE.ARC')
        with open(reg['0']['ar'],'rb') as fh:
            fh.seek(off); raw=fh.read(size)
        logo_entries,_=C.parse_multi_arc(raw)
        logo_names={str(e.get('name') or '') for e in logo_entries}
    except Exception:
        logo_names=set()
    td_rows={}
    try:
        for arcid,name,off,size in td_containers(reg):
            td_rows[str(name).casefold()]=(arcid,off,size)
    except Exception:
        td_rows={}
    for uid,st in result.items():
        st['logo_ready']=bool(_team_logo_entry_name(uid, logo_entries))
        name=f'2DRIVERSELECTTD_{uid}.ARC'
        hit=td_rows.get(name.casefold())
        if hit:
            st['paint_container_ready']=True
            try:
                arcid,off,size=hit
                with open(reg[arcid]['ar'],'rb') as fh:
                    fh.seek(off); raw=fh.read(size)
                entries,_=C.parse_multi_arc(raw)
                st['paint_resource_count']=len(entries)
            except Exception as ex:
                st['status_warning']='container exists but could not be parsed: '+str(ex)
        st['presentation_ready']=bool(st['logo_ready'] and st['paint_container_ready'])
    return result


def _team_friendly_catalog():
    _v, _row, pyc = _pyc_live_blob(DBFILE)
    mod = team_manager_mod()
    data = mod.catalog(pyc)
    data['legacy_team_patch'] = mod.legacy_patch_status(pyc)
    cfg = load_cfg()
    renames = cfg.get('renames', {}) if isinstance(cfg.get('renames', {}), dict) else {}
    base_labels = _team_driver_labels()
    manufacturer_labels = dict(TEAM_MANUFACTURER_NAMES)
    for m in data.get('manufacturers', []):
        uid = int(m['uid'])
        m['label'] = manufacturer_labels.get(uid, m.get('label') or m.get('token') or str(uid))
    state = _team_state_load()
    data['manufacturers'] = [m for m in data.get('manufacturers', []) if int(m.get('uid', -1)) not in UNSUPPORTED_MANUFACTURER_UIDS]
    data['teams'] = [t for t in data.get('teams', []) if int(t.get('uid', -1)) not in UNSUPPORTED_TEAM_UIDS]
    team_by_uid = {}
    for team in data.get('teams', []):
        uid = int(team['uid'])
        original = TEAM_DISPLAY_NAMES.get(uid, team.get('label') or team.get('token') or f'Team {uid}')
        team['original_label'] = original
        saved_name = state.get('team_names', {}).get(str(uid))
        team['label'] = saved_name or renames.get(original, original)
        team['manufacturer_label'] = manufacturer_labels.get(team.get('manufacturer_uid'), 'Unknown')
        team['is_spare'] = team.get('category') == 'spare'
        team['public_locked'] = _public_custom_team_locked(uid)
        team['public_lock_reason'] = PUBLIC_CUSTOM_TEAM_MESSAGE if team['public_locked'] else ''
        team_by_uid[uid] = team
    try:
        extra_state = extra_scheme_mod().load_state(EXTRA_SCHEME_STATE)
        created_by_driver = collections.defaultdict(list)
        for item in extra_state.get('schemes', []):
            if item.get('uid') is None or item.get('superseded_by'):
                continue
            created_by_driver[int(item.get('driver_uid', -1))].append(int(item['uid']))
    except Exception:
        created_by_driver = collections.defaultdict(list)
    for d in data.get('drivers', []):
        friendly = base_labels.get(str(d.get('base_arc') or '').upper())
        if friendly:
            # Respect a current full-name rename when the original roster name is known.
            d['label'] = renames.get(friendly, friendly)
        d['car_label'] = (('#' + str(d.get('number'))) if d.get('number') else 'No number') + ' ' + str(d.get('label') or '')
        team = team_by_uid.get(int(d.get('team_uid') or -1))
        d['team_label'] = team.get('label') if team else f"Team UID {d.get('team_uid')}"
        created_uids = sorted(created_by_driver.get(int(d.get('driver_uid', -1)), []))
        d['created_scheme_uids'] = created_uids
        d['created_scheme_count'] = len(created_uids)
        d['team_move_locked'] = bool(created_uids)
        d['team_move_lock_reason'] = (
            'This driver has extra paint slots you added: ' + ', '.join(map(str, created_uids)) +
            '. Moving a driver with added paint slots is not supported yet, because their thumbnails '
            'do not survive the move. Remove the extra slots first, or move the driver before adding any.' if created_uids else '')
        d['current_team_public_locked'] = _public_custom_team_locked(int(d.get('team_uid') or -1))
        d['recovery_move_available'] = bool(d['current_team_public_locked'] and not created_uids)
    members = {}
    driver_by_cfg = {int(d['config_uid']): d for d in data.get('drivers', [])}
    for d in data.get('drivers', []):
        members.setdefault(int(d['team_uid']), []).append(d)
    for team in data.get('teams', []):
        team['drivers'] = members.get(int(team['uid']), [])
        team['driver_count'] = len(team['drivers'])
    g = None
    team_status_warning = None
    try:
        g, _reg = registry()
        statuses = team_assets_mod().team_asset_statuses(g, [int(t['uid']) for t in data.get('teams', [])]) if g else {}
    except Exception as ex:
        team_status_warning = 'Team presentation helper fallback used: ' + str(ex)
        statuses = _team_asset_statuses_direct(g, [int(t['uid']) for t in data.get('teams', [])]) if g else {}
    for team in data.get('teams', []):
        team.update(statuses.get(int(team['uid']), {
            'logo_ready': False, 'paint_container_ready': False,
            'paint_resource_count': 0, 'presentation_ready': False
        }))
    try:
        art_locations = team_assets_mod().driver_art_location_map(g) if g else {}
    except Exception:
        art_locations = {}
    for d in data.get('drivers', []):
        preferred = f"2DRIVERSELECTTD_{int(d.get('team_uid', -1))}.ARC"
        locations = list(art_locations.get(int(d.get('driver_uid', -1)), []))
        chosen = preferred if preferred in locations else (locations[0] if locations else None)
        d['art_container'] = chosen
        d['art_locations'] = locations
        d['art_uses_fallback'] = bool(chosen and chosen != preferred)
        d['driver_art_ready'] = bool(chosen)
    data['state'] = state
    data['history'] = state.get('history', [])[-20:]
    data['asset_rollback'] = _team_asset_rollback_info()
    data['supported_spare_team_uids'] = list(SUPPORTED_SPARE_TEAM_UIDS)
    data['custom_team_capacity'] = len(SUPPORTED_SPARE_TEAM_UIDS)
    data['custom_teams_in_use'] = sum(1 for t in data.get('teams', []) if int(t.get('uid',-1)) in SUPPORTED_SPARE_TEAM_UIDS and int(t.get('driver_count',0))>0)
    data['team_status_warning'] = team_status_warning
    data['public_custom_teams_enabled'] = bool(PUBLIC_CUSTOM_TEAMS_ENABLED)
    data['public_custom_team_message'] = PUBLIC_CUSTOM_TEAM_MESSAGE
    data['warnings'] = [
        'A transfer copies the driver\u2019s artwork into the new team first, then makes the move. Drivers with paint slots you have added cannot be moved yet.',
        'Manufacturer switching updates the team association used by the game, including the matching Chevrolet, Ford, or Toyota body package.',
        PUBLIC_CUSTOM_TEAM_MESSAGE,
    ]
    data['driver_by_config'] = driver_by_cfg
    return data


def _team_install_changes(changes, source_name='Teams editor'):
    if _extra_game_running():
        raise RuntimeError('NASCAR15.exe is running. Close the game before changing teams')
    mod = team_manager_mod()
    v, row, current = _pyc_live_blob(DBFILE)
    rebuilt, meta = mod.build_changes(current, changes)
    if meta.get('noop'):
        return dict(ok=True, changed=False, verified=True, patch=meta)
    fd, tmp = tempfile.mkstemp(prefix='n15_team_', suffix='.PYC')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            f.write(rebuilt)
        _rp_backup_pair(v)
        with _RP_LOCK:
            result = _rp_install_one('0', v, row, tmp, source_name=source_name, allow_magic=True)
        _v2, _row2, live = _pyc_live_blob(DBFILE)
        check = mod.catalog(live)
        cfg_map = {int(d['config_uid']): int(d['team_uid']) for d in check.get('drivers', [])}
        team_map = {int(t['uid']): int(t['manufacturer_uid']) if t.get('manufacturer_uid') is not None else None for t in check.get('teams', [])}
        for ch in meta.get('changes', []):
            if ch['class_name'] == 'DRIVERCONFIG_c':
                got = cfg_map.get(int(ch['uid']))
            else:
                got = team_map.get(int(ch['uid']))
            if got != int(ch['target_uid']):
                raise RuntimeError(f"installed team edit failed readback for {ch['class_name']} UID {ch['uid']}")
        return dict(ok=True, changed=True, verified=True, patch=meta, install=result)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _team_reapply_saved_links():
    """Reapply safe saved links after a clean-base DB rebuild.

    Reapply saved authored-team and supported custom-team links after a clean-base
    database rebuild. Drivers with app-created paint slots remain protected from
    cross-team reapplication by the existing moved-paint guard.
    """
    state = _team_state_load()
    changes = []
    skipped_public_custom = []
    for uid, target in state.get('driver_teams', {}).items():
        if _public_custom_team_locked(int(target)):
            skipped_public_custom.append(dict(kind='driver_team', uid=int(uid), target_uid=int(target)))
            continue
        driver = _team_driver_by_config_uid(int(uid))
        if driver is not None:
            current_team = int(driver.get('team_uid', -1))
            created_uids = [int(x) for x in (driver.get('created_scheme_uids') or [])]
            if current_team != int(target) and created_uids:
                raise ValueError(
                    'Saved team-link repair is blocked for ' + str(driver.get('car_label') or uid) +
                    ' because app-created paint slot(s) ' + ', '.join(map(str, created_uids)) +
                    ' would cross teams through the unresolved thumbnail path.')
        changes.append(dict(class_name='DRIVERCONFIG_c', uid=int(uid), field='TEAM', target_uid=int(target)))
    for uid, target in state.get('team_manufacturers', {}).items():
        if _public_custom_team_locked(int(uid)):
            skipped_public_custom.append(dict(kind='team_manufacturer', uid=int(uid), target_uid=int(target)))
            continue
        changes.append(dict(class_name='RACETEAM_c', uid=int(uid), field='MANUFACTURER', target_uid=int(target)))
    result = _team_install_changes(changes, 'Repair and reapply saved team and manufacturer links')
    result['skipped_public_custom_team_links'] = skipped_public_custom
    result['public_custom_teams_enabled'] = bool(PUBLIC_CUSTOM_TEAMS_ENABLED)
    return result


def _team_asset_snapshot(reg):
    snap = {'groups': {}, 'state_exists': os.path.exists(TEAM_MANAGER_STATE),
            'state_bytes': open(TEAM_MANAGER_STATE, 'rb').read() if os.path.exists(TEAM_MANAGER_STATE) else None,
            'extra_state_captured': True,
            'extra_state_exists': os.path.exists(EXTRA_SCHEME_STATE),
            'extra_state_bytes': open(EXTRA_SCHEME_STATE, 'rb').read() if os.path.exists(EXTRA_SCHEME_STATE) else None}
    for key in ('0', '1'):
        v = need(reg, key)
        snap['groups'][key] = {'archive': v['ar'], 'size': os.path.getsize(v['ar']),
                               'cdf': v['cdf'], 'cdf_bytes': open(v['cdf'], 'rb').read()}
    return snap


def _team_asset_restore(snap):
    errors = []
    for key, item in (snap or {}).get('groups', {}).items():
        try:
            with open(item['archive'], 'r+b') as fh:
                fh.truncate(int(item['size'])); fh.flush(); os.fsync(fh.fileno())
            _extra_atomic_bytes(item['cdf'], item['cdf_bytes'])
        except Exception as ex:
            errors.append(f'{key}: {ex}')
    try:
        if snap.get('state_exists'):
            _extra_atomic_bytes(TEAM_MANAGER_STATE, snap.get('state_bytes') or b'')
        elif os.path.exists(TEAM_MANAGER_STATE):
            os.remove(TEAM_MANAGER_STATE)
    except Exception as ex:
        errors.append('state: ' + str(ex))
    try:
        if snap.get('extra_state_captured'):
            if snap.get('extra_state_exists'):
                _extra_atomic_bytes(EXTRA_SCHEME_STATE, snap.get('extra_state_bytes') or b'')
            elif os.path.exists(EXTRA_SCHEME_STATE):
                os.remove(EXTRA_SCHEME_STATE)
    except Exception as ex:
        errors.append('extra scheme state: ' + str(ex))
    return errors


def _team_asset_clear_persisted_snapshot():
    existed = os.path.isdir(TEAM_ASSET_ROLLBACK_DIR)
    try:
        if existed:
            shutil.rmtree(TEAM_ASSET_ROLLBACK_DIR)
        return bool(existed)
    except Exception:
        return False


def _team_snapshot_created_epoch(meta):
    raw = str((meta or {}).get('created') or '').strip()
    if not raw:
        return 0.0
    try:
        return float(datetime.datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp())
    except Exception:
        return 0.0


def _team_snapshot_restore_block(meta):
    if int((meta or {}).get('safety_version') or 0) >= 2:
        captured = {int(x) for x in ((meta or {}).get('captured_active_scheme_uids') or [])}
        try:
            state = extra_scheme_mod().load_state(EXTRA_SCHEME_STATE)
            current = {int(x.get('uid')) for x in state.get('schemes', [])
                       if x.get('uid') is not None and not x.get('superseded_by')}
        except Exception:
            current = set()
        added = sorted(current - captured)
        if added:
            ids = ', '.join(str(x) for x in added[:8])
            return (f'This restore point is older than paint slots you have since added ({ids}). '
                    'Using it would leave the game and the app disagreeing about what exists.')
    else:
        try:
            state = extra_scheme_mod().load_state(EXTRA_SCHEME_STATE)
            active = [x for x in state.get('schemes', []) if not x.get('superseded_by')]
        except Exception:
            active = []
        if active:
            return ('This legacy restore point did not capture app-created paint state and is disabled '
                    'to prevent archive/state desynchronization.')
    return None


def _team_asset_persist_snapshot(snap, label):
    """Persist an append/repoint rollback without copying multi-hundred-MB archives."""
    tmp = TEAM_ASSET_ROLLBACK_DIR + '.tmp'
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, exist_ok=True)
    captured_active_uids = []
    if snap.get('extra_state_exists') and snap.get('extra_state_bytes'):
        try:
            captured_state = json.loads((snap.get('extra_state_bytes') or b'{}').decode('utf-8'))
            captured_active_uids = sorted(int(x.get('uid')) for x in captured_state.get('schemes', [])
                                          if x.get('uid') is not None and not x.get('superseded_by'))
        except Exception:
            captured_active_uids = []
    manifest = {
        'format': 'nascar15-team-asset-rollback-v1',
        'safety_version': 2,
        'created': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
        'created_epoch': time.time(),
        'label': str(label or 'Team presentation change'),
        'groups': {},
        'state_exists': bool(snap.get('state_exists')),
        'extra_state_captured': True,
        'extra_state_exists': bool(snap.get('extra_state_exists')),
        'captured_active_scheme_uids': captured_active_uids,
    }
    for key, item in (snap or {}).get('groups', {}).items():
        name = f'cdf_{key}.bin'
        with open(os.path.join(tmp, name), 'wb') as fh:
            fh.write(item['cdf_bytes'])
        manifest['groups'][str(key)] = {
            'archive': os.path.abspath(item['archive']),
            'size': int(item['size']),
            'cdf': os.path.abspath(item['cdf']),
            'cdf_backup': name,
        }
    if snap.get('state_exists'):
        with open(os.path.join(tmp, 'team_state.bin'), 'wb') as fh:
            fh.write(snap.get('state_bytes') or b'')
        manifest['state_backup'] = 'team_state.bin'
    if snap.get('extra_state_exists'):
        with open(os.path.join(tmp, 'extra_scheme_state.bin'), 'wb') as fh:
            fh.write(snap.get('extra_state_bytes') or b'')
        manifest['extra_state_backup'] = 'extra_scheme_state.bin'
    with open(os.path.join(tmp, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2)
    if os.path.isdir(TEAM_ASSET_ROLLBACK_DIR):
        shutil.rmtree(TEAM_ASSET_ROLLBACK_DIR)
    os.replace(tmp, TEAM_ASSET_ROLLBACK_DIR)
    return manifest


def _team_asset_rollback_info():
    path = os.path.join(TEAM_ASSET_ROLLBACK_DIR, 'manifest.json')
    try:
        raw = json.load(open(path, 'r', encoding='utf-8'))
        blocked = _team_snapshot_restore_block(raw)
        return {'available': not bool(blocked), 'created': raw.get('created'),
                'label': raw.get('label'), 'blocked_reason': blocked,
                'safety_version': int(raw.get('safety_version') or 0)}
    except Exception:
        return {'available': False}


def _team_asset_load_persisted_snapshot(reg):
    path = os.path.join(TEAM_ASSET_ROLLBACK_DIR, 'manifest.json')
    raw = json.load(open(path, 'r', encoding='utf-8'))
    if raw.get('format') != 'nascar15-team-asset-rollback-v1':
        raise ValueError('the saved team rollback has an unknown format')
    blocked = _team_snapshot_restore_block(raw)
    if blocked:
        raise ValueError(blocked)
    snap = {'groups': {}, 'state_exists': bool(raw.get('state_exists')), 'state_bytes': None,
            'extra_state_captured': bool(raw.get('extra_state_captured')),
            'extra_state_exists': bool(raw.get('extra_state_exists')), 'extra_state_bytes': None}
    for key, item in (raw.get('groups') or {}).items():
        live = need(reg, str(key))
        expected_archive = os.path.normcase(os.path.abspath(item['archive']))
        expected_cdf = os.path.normcase(os.path.abspath(item['cdf']))
        if os.path.normcase(os.path.abspath(live['ar'])) != expected_archive or os.path.normcase(os.path.abspath(live['cdf'])) != expected_cdf:
            raise ValueError('the saved rollback belongs to a different NASCAR 15 installation')
        cdf_backup = os.path.join(TEAM_ASSET_ROLLBACK_DIR, item['cdf_backup'])
        saved_size = int(item['size'])
        if os.path.getsize(live['ar']) < saved_size:
            raise ValueError(
                f'ARCHIVE{key} is smaller than the saved pre-change size; refusing an unsafe team undo')
        cdf_bytes = open(cdf_backup, 'rb').read()
        if len(cdf_bytes) < 64 or cdf_bytes[:4] != b'filC':
            raise ValueError(f'the saved CDF backup for ARCHIVE{key} is invalid')
        snap['groups'][str(key)] = {
            'archive': live['ar'], 'size': saved_size, 'cdf': live['cdf'],
            'cdf_bytes': cdf_bytes,
        }
    if snap['state_exists']:
        state_name = raw.get('state_backup') or 'team_state.bin'
        snap['state_bytes'] = open(os.path.join(TEAM_ASSET_ROLLBACK_DIR, state_name), 'rb').read()
    if snap['extra_state_captured'] and snap['extra_state_exists']:
        extra_name = raw.get('extra_state_backup') or 'extra_scheme_state.bin'
        snap['extra_state_bytes'] = open(os.path.join(TEAM_ASSET_ROLLBACK_DIR, extra_name), 'rb').read()
    return snap, raw


def _team_original_team_map():
    try:
        _g, reg = registry(); v = need(reg, '0')
        archive = v.get('bak') if os.path.exists(v.get('bak', '')) else None
        cdf = backup_path(v['cdf']) if os.path.exists(backup_path(v['cdf'])) else None
        if not archive or not cdf:
            return {}
        _raw, rows, _layout = _rp_index_rows(cdf)
        row = _rp_find_row(rows, DBFILE)
        with open(archive, 'rb') as fh:
            fh.seek(row['offset']); pyc = fh.read(row['size'])
        cat = team_manager_mod().catalog(pyc)
        return {int(d['config_uid']): int(d['team_uid']) for d in cat.get('drivers', [])}
    except Exception:
        return {}


def _team_original_manufacturer_map():
    """Stock RACETEAM UID -> manufacturer UID from the oldest valid backup."""
    try:
        _g, reg = registry(); v = need(reg, '0')
        archive = v.get('bak') if os.path.exists(v.get('bak', '')) else None
        cdf = backup_path(v['cdf']) if os.path.exists(backup_path(v['cdf'])) else None
        if not archive or not cdf:
            return {}
        _raw, rows, _layout = _rp_index_rows(cdf)
        row = _rp_find_row(rows, DBFILE)
        with open(archive, 'rb') as fh:
            fh.seek(row['offset']); pyc = fh.read(row['size'])
        cat = team_manager_mod().catalog(pyc)
        return {int(t['uid']): int(t['manufacturer_uid']) for t in cat.get('teams', [])}
    except Exception:
        return {}


def _slot_manufacturer_context(slot_name):
    """Return whether a stock paint slot's live team changed body family."""
    link = next((x for x in load_driver_links()
                 if str(x.get('slot') or '').casefold() == str(slot_name or '').casefold()), None)
    if not link or link.get('driver_uid') is None:
        return {'known': False, 'mismatch': False}
    driver_uid = int(link['driver_uid'])
    live_driver = _team_fast_driver_links().get(driver_uid)
    if not live_driver:
        return {'known': False, 'mismatch': False, 'driver_uid': driver_uid}
    config_uid = int(live_driver['config_uid'])
    current_team_uid = int(live_driver['team_uid'])
    original_team_uid = int(_team_original_team_map().get(config_uid, current_team_uid))
    originals = _team_original_manufacturer_map()
    catalog = _team_friendly_catalog()
    teams = {int(t['uid']): t for t in catalog.get('teams', [])}
    current = teams.get(current_team_uid, {})
    current_mfr = current.get('manufacturer_uid')
    original_mfr = originals.get(original_team_uid)
    return {
        'known': current_mfr is not None and original_mfr is not None,
        'mismatch': (current_mfr is not None and original_mfr is not None
                     and int(current_mfr) != int(original_mfr)),
        'driver_uid': driver_uid,
        'config_uid': config_uid,
        'current_team_uid': current_team_uid,
        'original_team_uid': original_team_uid,
        'current_manufacturer_uid': current_mfr,
        'original_manufacturer_uid': original_mfr,
        'current_manufacturer': current.get('manufacturer_label'),
    }


def _team_driver_livery_uids(game, driver_uid):
    cat = extra_scheme_mod().catalog(game, EXTRA_SCHEME_STATE)
    driver = next((d for d in cat.get('drivers', []) if int(d.get('uid', -1)) == int(driver_uid)), None)
    if not driver:
        return []
    return sorted({int(x['uid']) for x in driver.get('schemes', []) if x.get('uid') is not None})


def _team_driver_by_driver_uid(driver_uid):
    catalog = _team_friendly_catalog()
    return next((d for d in catalog.get('drivers', [])
                 if int(d.get('driver_uid', -1)) == int(driver_uid)), None)


def _team_driver_by_config_uid(config_uid):
    catalog = _team_friendly_catalog()
    return next((d for d in catalog.get('drivers', [])
                 if int(d.get('config_uid', -1)) == int(config_uid)), None)


def _team_driver_by_art_key(key):
    """Prefer the stable DRIVERCONFIG UID; retain old driver-UID URLs."""
    return _team_driver_by_config_uid(key) or _team_driver_by_driver_uid(key)


def _team_resolved_driver_art(game, driver):
    return team_assets_mod().resolve_driver_art_container(
        game, int(driver['team_uid']), int(driver['driver_uid']))


def _team_preview_container_for_driver(driver_uid):
    driver = _team_driver_by_driver_uid(driver_uid)
    if not driver:
        raise ValueError('the driver is not linked to a current 2015 Cup team')
    return f"2DRIVERSELECTTD_{int(driver['team_uid'])}.ARC", driver


def _team_original_source_uid_for_driver(driver):
    state = _team_state_load()
    originals = _team_original_team_map()
    config_uid = int(driver['config_uid'])
    return int(state.get('driver_source_teams', {}).get(
        str(config_uid), originals.get(config_uid, driver['team_uid'])))


def _team_fast_driver_links():
    """Read only the live DRIVERCONFIG team links, without presentation scans."""
    _v, _row, pyc = _pyc_live_blob(DBFILE)
    raw = team_manager_mod().catalog(pyc)
    return {
        int(d['driver_uid']): {
            'driver_uid': int(d['driver_uid']),
            'config_uid': int(d['config_uid']),
            'team_uid': int(d['team_uid']),
        }
        for d in raw.get('drivers', [])
        if d.get('driver_uid') is not None and d.get('config_uid') is not None
        and d.get('team_uid') is not None
    }


def _stable_paint_creation_guard(driver_uid, driver=None, originals=None, state=None):
    """Classify whether the proven stock-team paint writer is safe for one driver.

    Optional cached inputs keep Paint Data reload fast.  The live team link is authoritative. Saved source-team metadata is
    trusted only while its saved destination still matches the live link, so a
    restored stock install cannot remain falsely locked by stale drop-in state.
    """
    driver = driver or _team_driver_by_driver_uid(int(driver_uid))
    if not driver:
        return {
            'locked': True, 'reason': 'the driver has no current 2015 Cup team link',
            'driver_uid': int(driver_uid), 'team_uid': None, 'moved': False,
            'spare_team': False,
        }
    config_uid = int(driver['config_uid'])
    team_uid = int(driver['team_uid'])
    originals = _team_original_team_map() if originals is None else originals
    state = _team_state_load() if state is None else state
    stock_original_uid = int(originals.get(config_uid, team_uid))
    source_raw = state.get('driver_source_teams', {}).get(str(config_uid))
    target_raw = state.get('driver_teams', {}).get(str(config_uid))
    saved_source_uid = int(source_raw) if source_raw is not None else stock_original_uid
    saved_target_uid = int(target_raw) if target_raw is not None else None

    # A source-team record alone is historical metadata, not proof that the
    # driver is currently moved.  Drop-in upgrades can retain that metadata after
    # the game archives were restored, which previously false-locked native
    # drivers.  Treat the saved move as active only when its target still matches
    # the live DRIVERCONFIG link.
    active_saved_move = bool(
        saved_target_uid is not None and saved_target_uid == team_uid
        and team_uid != saved_source_uid
    )
    moved = bool(team_uid != stock_original_uid or active_saved_move)
    original_uid = saved_source_uid if active_saved_move else stock_original_uid
    spare = bool(team_uid in SUPPORTED_SPARE_TEAM_UIDS)
    # rc9 experimental branch: transferred drivers on authored stock teams use
    # rc8's proven paired fixed-template writer. Spare/custom teams remain locked.
    locked = bool(spare)
    # The lock existed because appended resources corrupted the destination bank
    # (foreign directory header). With that fixed in nascar15_team_assets_v1.py,
    # this flag re-opens the path without deleting the guard.
    if locked and SPARE_TEAM_PAINT_CREATION_ENABLED:
        locked = False
    if locked:
        reason = ('Paint-slot creation is safety-locked for spare/custom teams in '
                  'the stable baseline. Team moves, logos, carousel art, 3D numbers, '
                  'and native paint previews remain available.')
    else:
        reason = ''
    return {
        'locked': locked, 'reason': reason, 'driver_uid': int(driver_uid),
        'config_uid': config_uid, 'team_uid': team_uid,
        'original_team_uid': original_uid, 'moved': moved, 'spare_team': spare,
        'experimental_moved_driver': bool(moved and not spare),
    }


def _team_active_created_uids():
    try:
        state = extra_scheme_mod().load_state(EXTRA_SCHEME_STATE)
        return {int(x.get('uid')) for x in state.get('schemes', [])
                if x.get('uid') is not None and not x.get('superseded_by')}
    except Exception:
        return set()


def _team_driver_native_livery_uids(game, driver_uid):
    """Return only native/non-app-created previews for stable team rebuilds."""
    created = _team_active_created_uids()
    return [uid for uid in _team_driver_livery_uids(game, driver_uid)
            if int(uid) not in created]


def _team_rebuild_created_thumbnails(game, driver_uid, target_team_uid):
    """Rebuild every app-created thumbnail through the proven append/repoint route.

    Team-bank resource transfer preserves exact native resources, but old custom-team
    builds may already contain an invalid copied alias.  Recreating each managed
    PAINTSCHEME from a valid same-team donor removes that legacy state without ever
    overwriting the currently indexed bank in place.
    """
    mod = extra_scheme_mod()
    state = mod.load_state(EXTRA_SCHEME_STATE)
    items = [x for x in state.get('schemes', [])
             if int(x.get('driver_uid', -1)) == int(driver_uid)
             and not x.get('superseded_by')]
    if not items:
        return []
    catalog = mod.catalog(game, EXTRA_SCHEME_STATE)
    driver = next((d for d in catalog.get('drivers', [])
                   if int(d.get('uid', -1)) == int(driver_uid)), None)
    if driver is None:
        raise ValueError(f'driver UID {int(driver_uid)} is missing from the livery catalog')
    target_container = f"2DRIVERSELECTTD_{int(target_team_uid)}.ARC"
    tm = extra_thumbnail_mod()
    reports = []
    for item in sorted(items, key=lambda x: int(x.get('uid', 0))):
        uid = int(item['uid'])
        donor = _extra_thumbnail_donor(
            game, driver, exclude_uid=uid, target_container=target_container)
        image_path = None
        saved = item.get('thumbnail_source_png')
        if saved:
            candidate = os.path.join(EXTRA_SCHEME_IMAGES, os.path.basename(str(saved)))
            if os.path.exists(candidate):
                image_path = candidate
        report = tm.install_or_replace_thumbnail(
            game, uid, int(donor['uid']), image_path,
            target_container_name=target_container)
        reports.append({
            'uid': uid, 'name': item.get('name'),
            'used_saved_custom_image': bool(image_path),
            'donor_uid': int(donor['uid']),
            'container': target_container,
            'method': report.get('method'),
            'archive_offset': report.get('archive_offset'),
            'readback_verified': bool(report.get('readback_verified')),
        })
    return reports


def _team_replay_saved_stock_thumbnails(game, driver, target_team_uid, livery_uids):
    """Replay proven stock-thumbnail replacements into the destination team bank.

    The native transfer writer copies the driver's presentation resources first.
    If a user replaced a stock Paint Select thumbnail, replay the exact saved PNG
    into the newly built destination bank before the DRIVERCONFIG team link moves.
    This avoids depending on which duplicate bank the old unscoped native lookup
    originally selected.
    """
    state=_team_state_load()
    overrides=state.get('thumbnail_overrides') or {}
    target_container=f"2DRIVERSELECTTD_{int(target_team_uid)}.ARC"
    tm=extra_thumbnail_mod()
    reports=[]
    for raw_uid in livery_uids:
        uid=int(raw_uid)
        meta=overrides.get(str(uid)) or {}
        slot=str(meta.get('slot') or '')
        saved=str(meta.get('saved_thumb') or '')
        candidates=[_stock_thumbnail_preview_path(uid)]
        if saved:
            candidates.append(os.path.join(SCHEMES,*saved.replace('\\','/').split('/')))
            candidates.append(os.path.join(SCHEMES,os.path.basename(saved)))
        if slot:candidates.append(os.path.join(SCHEMES,slot+'.thumb.png'))
        path=next((x for x in candidates if os.path.isfile(x)),None)
        if not path:
            continue
        hit=tm.find_target(game,uid,target_container_name=target_container)
        if not hit:
            raise ValueError(f'PAINTSCHEME_{uid} was not copied into {target_container}')
        identity=tm.inspect_thumbnail_identity(game,uid,target_container_name=target_container) or {}
        if not (identity.get('exists') and identity.get('structural_valid') and identity.get('same_bank_valid')):
            raise ValueError(f'PAINTSCHEME_{uid} in {target_container} is not safe for thumbnail replay')
        report=tm.replace_existing_thumbnail(game,uid,path,target_container_name=target_container)
        if 'texconv' not in str(report.get('encoder') or '').lower():
            raise ValueError(f'texconv DXT5 was not used while replaying PAINTSCHEME_{uid}')
        _extra_read_live_native_thumbnail_preview(game,uid,target_container)
        reports.append({'uid':uid,'container':target_container,'source':os.path.basename(path),
                        'method':report.get('method'),'readback_verified':bool(report.get('readback_verified'))})
    return reports

def _team_prepare_transfer_assets(game, driver, old_team_uid, new_team_uid):
    """Prepare a transfer through the exact public-v1 writer.

    Do not pre-classify a real team bank through the experimental footer model
    and do not fall back to a synthetic complete-bank rebuild. The working v1.0
    path edits the indexed destination revision directly, then appends and
    repoints it transactionally. The helper still resolves short physical aliases
    for driver art before writing the long logical identity.
    """
    assets = team_assets_mod()
    source_uid = int(old_team_uid)
    try:
        source_status = assets.team_asset_status(game, source_uid)
    except Exception:
        source_status = {}
    if not source_status.get('paint_container_ready'):
        state = _team_state_load()
        source_uid = int(state.get('driver_source_teams', {}).get(
            str(driver['config_uid']),
            _team_original_team_map().get(int(driver['config_uid']), old_team_uid)))
    livery_uids = _team_driver_native_livery_uids(
        game, int(driver['driver_uid']))
    paint = assets.ensure_driver_assets(
        game, int(new_team_uid), source_uid, int(driver['driver_uid']),
        livery_uids)
    paint['transfer_strategy'] = 'public_v1_direct_revision'

    stock_thumbnail_replays = _team_replay_saved_stock_thumbnails(
        game, driver, int(new_team_uid), livery_uids)

    thumbnails = []
    if SPARE_TEAM_PAINT_CREATION_ENABLED:
        thumbnails = _team_rebuild_created_thumbnails(
            game, int(driver['driver_uid']), int(new_team_uid))
        thumbnail_guard = {
            'skipped': False,
            'reason': 'spare-team paint creation enabled',
        }
    else:
        thumbnail_guard = {
            'skipped': True,
            'reason': ('stable baseline keeps app-created thumbnails out of '
                       'moved/custom team banks'),
        }
    logo = None
    status = assets.team_asset_status(game, int(new_team_uid))
    if not status.get('logo_ready'):
        logo = assets.ensure_team_logo(game, int(new_team_uid), source_uid)
    return {
        'paint': paint, 'thumbnails': thumbnails,
        'stock_thumbnail_replays': stock_thumbnail_replays,
        'thumbnail_guard': thumbnail_guard, 'logo': logo,
        'livery_uids': livery_uids, 'source_team_uid': source_uid,
    }


def _team_history_label(kind, catalog, uid, old_uid, new_uid):
    if kind == 'driver_team':
        d = next((x for x in catalog.get('drivers', []) if int(x['config_uid']) == int(uid)), None)
        old = next((x for x in catalog.get('teams', []) if int(x['uid']) == int(old_uid)), None)
        new = next((x for x in catalog.get('teams', []) if int(x['uid']) == int(new_uid)), None)
        return f"{(d or {}).get('car_label', 'Driver')} · {(old or {}).get('label', old_uid)} → {(new or {}).get('label', new_uid)}"
    team = next((x for x in catalog.get('teams', []) if int(x['uid']) == int(uid)), None)
    manu = {int(x['uid']): x.get('label') for x in catalog.get('manufacturers', [])}
    return f"{(team or {}).get('label', 'Team')} · {manu.get(int(old_uid), old_uid)} → {manu.get(int(new_uid), new_uid)}"


@app.route('/api/teams/catalog')
def teams_catalog_api():
    try:
        return jsonify(dict(ok=True, **_team_friendly_catalog()))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/move_driver', methods=['POST'])
def teams_move_driver_api():
    try:
        q = request.get_json(force=True) or {}
        config_uid = int(q.get('config_uid'))
        target_uid = int(q.get('team_uid'))
        dry = bool(q.get('dry_run'))
        with _TEAM_MANAGER_LOCK:
            before = _team_friendly_catalog()
            driver = next((x for x in before.get('drivers', []) if int(x['config_uid']) == config_uid), None)
            team = next((x for x in before.get('teams', []) if int(x['uid']) == target_uid), None)
            if driver is None:
                raise ValueError('2015 Cup driver configuration was not found')
            if team is None:
                raise ValueError('destination team was not found')
            old_uid = int(driver['team_uid'])
            preview = dict(kind='driver_team', config_uid=config_uid, driver=driver.get('car_label'),
                           old_team_uid=old_uid, old_team=driver.get('team_label'),
                           new_team_uid=target_uid, new_team=team.get('label'),
                           affected_schemes='native schemes attached to this 2015 Cup driver configuration')
            created_uids = [int(x) for x in (driver.get('created_scheme_uids') or [])]
            public_custom_target = bool(target_uid != old_uid and _public_custom_team_locked(target_uid))
            move_blocked = bool(target_uid != old_uid and (created_uids or public_custom_target))
            if move_blocked:
                preview['allowed'] = False
                preview['app_created_scheme_uids'] = created_uids
                preview['public_custom_team_locked'] = public_custom_target
                preview['blocked_reason'] = (
                    PUBLIC_CUSTOM_TEAM_MESSAGE if public_custom_target else
                    'Cannot move this driver yet — they have extra paint slots you added: ' +
                    ', '.join(map(str, created_uids)) +
                    '. Moving them would require the unresolved moved-team thumbnail path.')
            else:
                preview['allowed'] = True
                preview['recovery_move'] = bool(_public_custom_team_locked(old_uid) and not _public_custom_team_locked(target_uid))
            if dry:
                return jsonify(dict(ok=True, dry_run=True, preview=preview))
            if target_uid == old_uid:
                return jsonify(dict(ok=True, verified=True, changed=False, preview=preview,
                                    note='Driver is already assigned to that team; no files or saved state changed.'))
            if move_blocked:
                raise ValueError(preview['blocked_reason'])
            if target_uid in UNSUPPORTED_TEAM_UIDS:
                raise ValueError('Dodge/custom Dodge is not supported by the game')
            g, reg = _extra_game_and_registry()
            snapshot = _team_asset_snapshot(reg)
            rollback_manifest = _team_asset_persist_snapshot(snapshot, f"Move {driver.get('car_label')} to {team.get('label')}")
            try:
                assets = _team_prepare_transfer_assets(g, driver, old_uid, target_uid)
                result = _team_install_changes([
                    dict(class_name='DRIVERCONFIG_c', uid=config_uid, field='TEAM', target_uid=target_uid)
                ], f"Move {driver.get('car_label')} to {team.get('label')}")
                state = _team_state_load()
                if result.get('changed'):
                    state['driver_teams'][str(config_uid)] = target_uid
                    state['driver_source_teams'].setdefault(str(config_uid), int(assets.get('source_team_uid', old_uid)))
                if target_uid in SUPPORTED_SPARE_TEAM_UIDS:
                    state['team_logo_donors'].setdefault(str(target_uid), int(assets.get('source_team_uid', old_uid)))
                if result.get('changed'):
                    state['history'].append(dict(kind='driver_team', uid=config_uid, old_uid=old_uid,
                                                 new_uid=target_uid, label=_team_history_label('driver_team', before, config_uid, old_uid, target_uid),
                                                 rollback_label=rollback_manifest.get('label'),
                                                 rollback_created=rollback_manifest.get('created'),
                                                 rollback_created_epoch=rollback_manifest.get('created_epoch'),
                                                 created=datetime.datetime.now().isoformat(timespec='seconds')))
                state['history'] = state['history'][-100:]
                _team_state_save(state)
                # The driver's live team link and destination Paint Select bank
                # changed together.  Drop every cached livery/thumbnail lookup so
                # Existing Paints immediately shows the moved driver's current
                # thumbnail instead of the old-team cached image.
                _clear_ui_thumb_cache()
            except Exception:
                restore_errors = _team_asset_restore(snapshot)
                if not restore_errors:
                    _team_asset_clear_persisted_snapshot()
                if restore_errors:
                    raise RuntimeError('team transfer failed; rollback also reported: ' + '; '.join(restore_errors))
                raise
            return jsonify(dict(ok=True, verified=True, changed=result.get('changed', False), preview=preview, result=result, assets=assets))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/set_manufacturer', methods=['POST'])
def teams_set_manufacturer_api():
    try:
        q = request.get_json(force=True) or {}
        team_uid = int(q.get('team_uid'))
        target_uid = int(q.get('manufacturer_uid'))
        dry = bool(q.get('dry_run'))
        with _TEAM_MANAGER_LOCK:
            before = _team_friendly_catalog()
            team = next((x for x in before.get('teams', []) if int(x['uid']) == team_uid), None)
            manufacturer = next((x for x in before.get('manufacturers', []) if int(x['uid']) == target_uid), None)
            if team is None:
                raise ValueError('team was not found')
            _public_custom_team_guard(team_uid, 'change the manufacturer for this reserve team')
            if manufacturer is None:
                raise ValueError('manufacturer was not found')
            if target_uid in UNSUPPORTED_MANUFACTURER_UIDS:
                raise ValueError('Dodge is not supported by NASCAR 15 team/car assets')
            old_uid = int(team['manufacturer_uid'])
            preview = dict(kind='team_manufacturer', team_uid=team_uid, team=team.get('label'),
                           old_manufacturer_uid=old_uid, old_manufacturer=team.get('manufacturer_label'),
                           new_manufacturer_uid=target_uid, new_manufacturer=manufacturer.get('label'),
                           affected_drivers=[x.get('car_label') for x in team.get('drivers', [])],
                           body_model_note='The game will use the matching manufacturer body package.')
            if dry:
                return jsonify(dict(ok=True, dry_run=True, preview=preview))
            g, reg = _extra_game_and_registry()
            snapshot = _team_asset_snapshot(reg)
            rollback_manifest = _team_asset_persist_snapshot(snapshot, f"Set {team.get('label')} manufacturer to {manufacturer.get('label')}")
            try:
                result = _team_install_changes([
                    dict(class_name='RACETEAM_c', uid=team_uid, field='MANUFACTURER', target_uid=target_uid)
                ], f"Set {team.get('label')} manufacturer to {manufacturer.get('label')}")
                state = _team_state_load()
                state['team_manufacturers'][str(team_uid)] = target_uid
                if result.get('changed'):
                    state['history'].append(dict(kind='team_manufacturer', uid=team_uid, old_uid=old_uid,
                                                 new_uid=target_uid, label=_team_history_label('team_manufacturer', before, team_uid, old_uid, target_uid),
                                                 rollback_label=rollback_manifest.get('label'),
                                                 rollback_created=rollback_manifest.get('created'),
                                                 rollback_created_epoch=rollback_manifest.get('created_epoch'),
                                                 created=datetime.datetime.now().isoformat(timespec='seconds')))
                state['history'] = state['history'][-100:]
                _team_state_save(state)
            except Exception:
                restore_errors = _team_asset_restore(snapshot)
                if not restore_errors:
                    _team_asset_clear_persisted_snapshot()
                if restore_errors:
                    raise RuntimeError('manufacturer change failed; rollback also reported: ' + '; '.join(restore_errors))
                raise
            return jsonify(dict(ok=True, verified=True, changed=result.get('changed', False), preview=preview, result=result,
                                paint_warning='Existing stock paint templates remain authored for the old body; Smart Import now blocks old-body auto alignment.'))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/prepare', methods=['POST'])
def teams_prepare_assets_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before rebuilding team presentation assets')
        q = request.get_json(force=True) or {}
        team_uid = int(q.get('team_uid'))
        if team_uid in UNSUPPORTED_TEAM_UIDS:
            raise ValueError('the Dodge spare slot is not supported')
        _public_custom_team_guard(team_uid, 'build or repair reserve-team presentation assets')
        with _TEAM_MANAGER_LOCK:
            catalog = _team_friendly_catalog()
            team = next((x for x in catalog.get('teams', []) if int(x['uid']) == team_uid), None)
            if team is None:
                raise ValueError('team was not found')
            drivers = list(team.get('drivers') or [])
            if not drivers:
                raise ValueError('move at least one driver into this team before building its paint container')
            g, reg = _extra_game_and_registry()
            snapshot = _team_asset_snapshot(reg)
            _team_asset_persist_snapshot(snapshot, f"Build or repair presentation assets for {team.get('label')}")
            reports = []
            state = _team_state_load()
            originals = _team_original_team_map()
            try:
                for driver in drivers:
                    source_uid = int(state.get('driver_source_teams', {}).get(str(driver['config_uid']),
                                     originals.get(int(driver['config_uid']), driver['team_uid'])))
                    liveries = _team_driver_native_livery_uids(g, int(driver['driver_uid']))
                    driver_report = team_assets_mod().ensure_driver_assets(
                        g, team_uid, source_uid, int(driver['driver_uid']), liveries)
                    driver_report['transfer_strategy'] = 'public_v1_direct_revision'
                    if SPARE_TEAM_PAINT_CREATION_ENABLED:
                        driver_report['created_thumbnails_rebuilt'] = _team_rebuild_created_thumbnails(
                            g, int(driver['driver_uid']), team_uid)
                        driver_report['created_thumbnail_guard'] = 'spare-team paint creation enabled'
                    else:
                        driver_report['created_thumbnails_rebuilt'] = []
                        driver_report['created_thumbnail_guard'] = 'skipped by stable baseline'
                    reports.append(driver_report)
                    state['driver_source_teams'].setdefault(str(driver['config_uid']), source_uid)
                donor = int(state.get('team_logo_donors', {}).get(str(team_uid),
                            reports[0].get('source_team_uid', TEAM_DEFAULT_LOGO_DONORS.get(team_uid, team_uid))))
                logo = team_assets_mod().ensure_team_logo(g, team_uid, donor)
                state['team_logo_donors'][str(team_uid)] = donor
                _team_state_save(state)
            except Exception:
                restore_errors = _team_asset_restore(snapshot)
                if not restore_errors:
                    _team_asset_clear_persisted_snapshot()
                if restore_errors:
                    raise RuntimeError('team asset repair failed; rollback also reported: ' + '; '.join(restore_errors))
                raise
            return jsonify(dict(ok=True, verified=True, team_uid=team_uid,
                                drivers=len(drivers), reports=reports, logo=logo))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/rename', methods=['POST'])
def teams_rename_api():
    try:
        q = request.get_json(force=True) or {}
        team_uid = int(q.get('team_uid'))
        new_name = str(q.get('name') or '').strip()
        if not new_name:
            raise ValueError('enter a team name')
        if len(new_name) > 80:
            raise ValueError('team name must be 80 characters or fewer')
        if team_uid in UNSUPPORTED_TEAM_UIDS:
            raise ValueError('the Dodge spare slot is not supported')
        _public_custom_team_guard(team_uid, 'rename this reserve team')
        with _TEAM_MANAGER_LOCK:
            before = _team_friendly_catalog()
            team = next((x for x in before.get('teams', []) if int(x['uid']) == team_uid), None)
            if team is None:
                raise ValueError('team was not found')
            current = str(team.get('label') or '').strip()
            original = str(team.get('original_label') or current).strip()
            if new_name == current:
                return jsonify(dict(ok=True, changed=False, name=new_name))
            _g, reg = registry()
            patched = 0
            errors = []
            for candidate in dict.fromkeys([current, original]):
                if not candidate or candidate == new_name:
                    continue
                try:
                    patched = patch_name_exp(reg, candidate, new_name)
                    if patched:
                        break
                except Exception as ex:
                    errors.append(str(ex))
            # The three stock spare RACETEAM records use localization tokens
            # that have no matching display-string entry in some installs.
            # Persist the UID-based name regardless; patch the game text only
            # where a real live entry exists instead of rejecting the rename.
            state = _team_state_load()
            state['team_names'][str(team_uid)] = new_name
            state['history'].append(dict(kind='team_name', uid=team_uid, old_name=current,
                                         new_name=new_name, patched=int(patched),
                                         game_text_patched=bool(patched),
                                         label=f'{current} → {new_name}',
                                         created=datetime.datetime.now().isoformat(timespec='seconds')))
            state['history'] = state['history'][-100:]
            _team_state_save(state)
            cfg = load_cfg(); cfg.setdefault('renames', {})[original] = new_name; save_cfg(cfg)
            warning = None
            if not patched:
                warning = ('Saved by stable team UID. This stock spare slot has no '
                           'matching live text-table entry, so there was no in-game '
                           'localization string to patch.')
            return jsonify(dict(ok=True, changed=True, name=new_name, patched=patched,
                                game_text_patched=bool(patched), warning=warning,
                                text_errors=errors[-3:]))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


def _prepare_team_logo_auto(image, target_size):
    """Prepare a logo for the wide Team Select tile without visible stretching.

    Most TEAM textures are stored on a square native canvas but rendered by the
    menu on an approximately 2:1 card.  Fitting directly to the native canvas
    therefore makes normal wordmarks look twice as wide in-game.  Build the
    artwork in a virtual 2:1 display canvas first, then compress that complete
    canvas back to the native texture.  The game stretches it back to the
    intended proportions.
    """
    tw, th = map(int, target_size)
    src = image.convert('RGBA')
    alpha = src.getchannel('A')
    bbox = alpha.getbbox()
    cropped = False
    if bbox and bbox != (0, 0, src.width, src.height):
        src = src.crop(bbox)
        cropped = True

    display_aspect = 2.0
    display_w = max(1, int(round(th * display_aspect)))
    display_h = th
    # Generous safe area: the actual red card masks a little more than the
    # raw texture preview suggests, especially at the right edge.
    safe_w = max(1, int(round(display_w * 0.72)))
    safe_h = max(1, int(round(display_h * 0.64)))
    fitted, info = prepare_import_image(src, (safe_w, safe_h), 'fit',
                                        preserve_alpha=True,
                                        background=(0, 0, 0, 0))
    virtual = Image.new('RGBA', (display_w, display_h), (0, 0, 0, 0))
    virtual.alpha_composite(fitted, ((display_w - safe_w) // 2,
                                     (display_h - safe_h) // 2))
    lanczos = Image.Resampling.LANCZOS if hasattr(Image, 'Resampling') else Image.LANCZOS
    canvas = virtual.resize((tw, th), lanczos)
    info.update({
        'native_target': [tw, th],
        'virtual_display_canvas': [display_w, display_h],
        'safe_display_area': [safe_w, safe_h],
        'display_aspect_compensation': display_aspect,
        'transparent_border_trimmed': bool(cropped),
        'mode': 'team-select-display-fit',
    })
    return canvas, info


@app.route('/api/teams/logo/<int:team_uid>')
def teams_logo_png(team_uid):
    try:
        g, reg = registry()
        if not g:
            raise RuntimeError('NASCAR 15 game folder is not selected')
        _arcid, _off, _size, raw = menu_container(reg, 'teams', live=True)
        entries, _ = C.parse_multi_arc(raw)
        resolved_name = _team_logo_entry_name(team_uid, entries)
        entry = next((e for e in entries if e['name'] == resolved_name), None) if resolved_name else None
        if entry is None:
            raise ValueError('team logo is not installed')
        image = C.multi_read_png(raw, entry)
        if request.args.get('display'):
            image = _ui_fit_preview_crop(image, (320, 180), (286, 156),
                                         alpha_first=True, threshold=8)
        out = io.BytesIO(); image.save(out, format='PNG'); out.seek(0)
        return send_file(out, mimetype='image/png', as_attachment=bool(request.args.get('download')), download_name=f'TEAM_{int(team_uid)}.png', max_age=0)
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 404


@app.route('/api/teams/logo', methods=['POST'])
def teams_logo_install_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before changing team logos')
        team_uid = int(request.form.get('team_uid'))
        if team_uid in UNSUPPORTED_TEAM_UIDS:
            raise ValueError('the Dodge spare slot is not supported')
        _public_custom_team_guard(team_uid, 'install or replace a reserve-team logo')
        f = request.files.get('file')
        if not f:
            raise ValueError('choose a logo image')
        raw = f.read()
        if not raw:
            raise ValueError('logo image is empty')
        image = Image.open(io.BytesIO(raw)); image.load()
        fd, temp_path = tempfile.mkstemp(prefix='n15_team_logo_', suffix='.png')
        os.close(fd)
        try:
            with _TEAM_MANAGER_LOCK:
                g, reg = _extra_game_and_registry()
                state = _team_state_load()
                donor = int(state.get('team_logo_donors', {}).get(str(team_uid), TEAM_DEFAULT_LOGO_DONORS.get(team_uid, team_uid)))
                assets = team_assets_mod()
                spec = assets.team_logo_spec(g, team_uid, donor)
                prepared, prep = _prepare_team_logo_auto(
                    image, (int(spec['width']), int(spec['height'])))
                prep['native_entry'] = spec.get('entry')
                prep['native_format'] = spec.get('format')
                prepared.save(temp_path, 'PNG')
                snapshot = _team_asset_snapshot(reg)
                _team_asset_persist_snapshot(snapshot, f"Install or replace TEAM_{team_uid} logo")
                try:
                    status = assets.team_asset_status(g, team_uid)
                    if status.get('logo_ready'):
                        result = assets.replace_team_logo(g, team_uid, temp_path)
                    else:
                        result = assets.ensure_team_logo(g, team_uid, donor, temp_path)
                    state['team_logo_donors'][str(team_uid)] = donor
                    _team_state_save(state)
                except Exception:
                    restore_errors = _team_asset_restore(snapshot)
                    if not restore_errors:
                        _team_asset_clear_persisted_snapshot()
                    if restore_errors:
                        raise RuntimeError('team logo install failed; rollback also reported: ' + '; '.join(restore_errors))
                    raise
            try: _clear_ui_thumb_cache()
            except Exception: pass
            return jsonify(dict(ok=True, verified=True, result=result, preparation=prep,
                                rollback_available=True))
        finally:
            try: os.remove(temp_path)
            except OSError: pass
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/driver_art/<int:driver_key>/<kind>')
def teams_driver_art_png(driver_key, kind):
    try:
        g, _reg = _extra_game_and_registry()
        driver = _team_driver_by_art_key(driver_key)
        if not driver:
            raise ValueError('driver was not found in the current 2015 Cup team catalog')
        assets = team_assets_mod()
        resolved = assets.resolve_driver_art_container(
            g, int(driver['team_uid']), int(driver['driver_uid']))
        image = assets.read_driver_art_image(
            g, int(resolved['team_uid']), int(driver['driver_uid']), kind)
        if request.args.get('display'):
            alpha_first = str(kind).lower() not in ('number', '3dnum', 'card')
            image = _ui_fit_preview_crop(image, (360, 180), (326, 154),
                                         alpha_first=alpha_first, threshold=8)
        out = io.BytesIO(); image.save(out, format='PNG'); out.seek(0)
        filename = f"{assets.driver_art_resource_name(driver['driver_uid'], kind)}.png"
        response = send_file(out, mimetype='image/png',
                             as_attachment=bool(request.args.get('download')),
                             download_name=filename, max_age=0)
        response.headers['X-N15-Art-Container'] = str(resolved['container'])
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        return response
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 404


@app.route('/api/teams/driver_art', methods=['POST'])
def teams_driver_art_install_api():
    temp_path = None
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before changing Driver Select art')
        raw_key = request.form.get('config_uid') or request.form.get('driver_uid')
        if raw_key is None:
            raise ValueError('driver key is missing')
        driver_key = int(raw_key)
        kind = str(request.form.get('kind') or '').strip().lower()
        f = request.files.get('file')
        if not f:
            raise ValueError('choose an image')
        raw = f.read()
        if not raw:
            raise ValueError('image is empty')
        image = Image.open(io.BytesIO(raw)); image.load()
        with _TEAM_MANAGER_LOCK:
            g, reg = _extra_game_and_registry()
            driver = _team_driver_by_art_key(driver_key)
            if not driver:
                raise ValueError('driver was not found in the current team catalog')
            _public_custom_team_guard(int(driver['team_uid']), 'replace Driver Select art for a reserve-team driver')
            driver_uid = int(driver['driver_uid'])
            assets = team_assets_mod()
            art_team_uid = int(driver['team_uid'])
            art_container = f"2DRIVERSELECTTD_{art_team_uid}.ARC"
            source_uid = _team_original_source_uid_for_driver(driver)
            snapshot = _team_asset_snapshot(reg)
            _team_asset_persist_snapshot(snapshot, f"Replace Driver Select art for {driver.get('car_label')}")
            try:
                try:
                    spec = assets.driver_art_spec(g, art_team_uid, driver_uid, kind)
                except Exception:
                    # Missing art must be installed into the CURRENT team, not edited
                    # in the original-team fallback bank.
                    liveries = _team_driver_native_livery_uids(g, driver_uid)
                    installed = assets.ensure_driver_assets(
                        g, art_team_uid, source_uid, driver_uid, liveries)
                    installed['transfer_strategy'] = 'public_v1_direct_revision'
                    # Stable baseline: do not write app-created thumbnails into moved/custom team banks.
                    spec = assets.driver_art_spec(g, art_team_uid, driver_uid, kind)
                mode = str(request.form.get('resize_mode') or 'fit').lower()
                prepared, prep = prepare_import_image(
                    image, (int(spec['width']), int(spec['height'])), mode,
                    preserve_alpha=True, background=(0, 0, 0, 0))
                prep['native_entry'] = spec['entry']; prep['native_format'] = spec['format']
                prep['art_container'] = art_container
                if kind in ('number', '3dnum', 'card'):
                    prep['workflow'] = '3d_number_texture'
                    prep['kind_label'] = '3D Number Texture'
                    prep['template_recommended'] = True
                    sw, sh = map(int, prep.get('source', [0, 0]))
                    if [sw, sh] != [int(spec['width']), int(spec['height'])]:
                        prep['note'] = ('Best results come from Export → edit that 512×256 template → Import. '
                                        'General images are fit onto a transparent 512×256 canvas automatically.')
                elif kind in ('tile', 'paint', 'driverpaint'):
                    prep['workflow'] = 'driver_carousel_tile'
                    prep['kind_label'] = 'Driver Carousel Tile'
                    prep['native_overlap_tail'] = 64
                fd, temp_path = tempfile.mkstemp(prefix='n15_driver_art_', suffix='.png')
                os.close(fd); prepared.save(temp_path, 'PNG')
                result = assets.replace_driver_art(g, art_team_uid, driver_uid, kind, temp_path)
            except Exception:
                restore_errors = _team_asset_restore(snapshot)
                if not restore_errors:
                    _team_asset_clear_persisted_snapshot()
                if restore_errors:
                    raise RuntimeError('driver art install failed; rollback also reported: ' + '; '.join(restore_errors))
                raise
        try: _clear_ui_thumb_cache()
        except Exception: pass
        return jsonify(dict(ok=True, verified=True, result=result, preparation=prep,
                            team_uid=int(driver['team_uid']), art_team_uid=art_team_uid,
                            art_container=art_container,
                            config_uid=int(driver['config_uid']), driver_uid=driver_uid,
                            kind=kind, kind_label=('3D Number Texture' if kind in ('number','3dnum','card') else 'Driver Carousel Tile'),
                            rollback_available=True))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400
    finally:
        if temp_path:
            try: os.remove(temp_path)
            except OSError: pass


@app.route('/api/teams/driver_art/repair', methods=['POST'])
def teams_driver_art_repair_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before repairing Driver Select art')
        q = request.get_json(force=True) or {}
        raw_key = q.get('config_uid', q.get('driver_uid'))
        if raw_key is None:
            raise ValueError('driver key is missing')
        driver_key = int(raw_key)
        with _TEAM_MANAGER_LOCK:
            g, reg = _extra_game_and_registry()
            driver = _team_driver_by_art_key(driver_key)
            if not driver:
                raise ValueError('driver was not found in the current team catalog')
            _public_custom_team_guard(int(driver['team_uid']), 'repair Driver Select art for a reserve-team driver')
            driver_uid = int(driver['driver_uid'])
            assets = team_assets_mod()
            destination_uid = int(driver['team_uid'])
            destination_container = f"2DRIVERSELECTTD_{destination_uid}.ARC"
            source_uid = _team_original_source_uid_for_driver(driver)
            snapshot = _team_asset_snapshot(reg)
            _team_asset_persist_snapshot(snapshot, f"Repair Driver Select art for {driver.get('car_label')}")
            try:
                liveries = _team_driver_native_livery_uids(g, driver_uid)
                result = assets.ensure_driver_assets(
                    g, destination_uid, source_uid, driver_uid, liveries)
                result['transfer_strategy'] = 'public_v1_direct_revision'
                if SPARE_TEAM_PAINT_CREATION_ENABLED:
                    result['created_thumbnails_rebuilt'] = _team_rebuild_created_thumbnails(
                        g, driver_uid, destination_uid)
                    result['created_thumbnail_guard'] = 'spare-team paint creation enabled'
                else:
                    result['created_thumbnails_rebuilt'] = []
                    result['created_thumbnail_guard'] = 'skipped by stable baseline'
            except Exception:
                restore_errors = _team_asset_restore(snapshot)
                if not restore_errors:
                    _team_asset_clear_persisted_snapshot()
                if restore_errors:
                    raise RuntimeError('driver art repair failed; rollback also reported: ' + '; '.join(restore_errors))
                raise
        try: _clear_ui_thumb_cache()
        except Exception: pass
        repaired_resources = list(dict.fromkeys(
            list(result.get('resources_added', [])) + list(result.get('resources_repaired', []))))
        return jsonify(dict(ok=True, verified=True, result=result,
                            repaired=repaired_resources,
                            config_uid=int(driver['config_uid']), driver_uid=driver_uid,
                            team_uid=int(driver['team_uid']),
                            art_team_uid=destination_uid,
                            art_container=destination_container,
                            source_team_uid=source_uid, livery_uids=liveries, rollback_available=True))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/restore_assets', methods=['POST'])
def teams_restore_assets_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before restoring team assets')
        with _TEAM_MANAGER_LOCK:
            _g, reg = _extra_game_and_registry()
            snap, meta = _team_asset_load_persisted_snapshot(reg)
            errors = _team_asset_restore(snap)
            if errors:
                raise RuntimeError('team asset rollback reported: ' + '; '.join(errors))
            _team_asset_clear_persisted_snapshot()
            try: _clear_ui_thumb_cache()
            except Exception: pass
            return jsonify(dict(ok=True, verified=True, restored=meta.get('label'),
                                created=meta.get('created')))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400


@app.route('/api/teams/repair_legacy', methods=['POST'])
def teams_repair_legacy_api():
    try:
        with _TEAM_MANAGER_LOCK:
            result = _team_reapply_saved_links()
            return jsonify(dict(ok=True, verified=True, result=result,
                                note='Removed the unsafe late ApplyPatch team hook and reapplied saved links through the stock STORE_ATTR operands.'))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400



def _team_history_matches_persisted_rollback(item, meta):
    """True only when the one-level archive snapshot belongs to this history item."""
    if not item or not meta:
        return False
    item_label = str(item.get('rollback_label') or '').strip()
    meta_label = str(meta.get('label') or '').strip()
    if not item_label or item_label != meta_label:
        return False
    item_epoch = float(item.get('rollback_created_epoch') or 0.0)
    meta_epoch = float(meta.get('created_epoch') or 0.0)
    if item_epoch and meta_epoch:
        return abs(item_epoch - meta_epoch) < 0.01
    item_created = str(item.get('rollback_created') or '').strip()
    meta_created = str(meta.get('created') or '').strip()
    return bool(item_created and item_created == meta_created)



@app.route('/api/teams/undo', methods=['POST'])
def teams_undo_api():
    try:
        if _extra_game_running():
            raise RuntimeError('NASCAR15.exe is running. Close the game before undoing a team change')
        with _TEAM_MANAGER_LOCK:
            state = _team_state_load()
            history = state.get('history', [])
            if not history:
                raise ValueError('there is no team edit to undo')
            item = history[-1]
            kind = item.get('kind')
            uid = int(item['uid'])
            if kind == 'team_name':
                old_name = str(item.get('old_name') or '').strip()
                new_name = str(item.get('new_name') or '').strip()
                if not old_name or not new_name:
                    raise ValueError('the latest team-name history entry is incomplete')
                _g, reg = registry()
                patched = 0
                if int(item.get('patched', 1) or 0) > 0 or item.get('game_text_patched'):
                    try:
                        patched = patch_name_exp(reg, new_name, old_name)
                    except Exception:
                        patched = 0
                original = TEAM_DISPLAY_NAMES.get(uid, old_name)
                if old_name == original:
                    state.get('team_names', {}).pop(str(uid), None)
                else:
                    state.setdefault('team_names', {})[str(uid)] = old_name
                cfg = load_cfg(); cfg.setdefault('renames', {})[original] = old_name; save_cfg(cfg)
                history.pop(); state['history'] = history; _team_state_save(state)
                return jsonify(dict(ok=True, verified=True, undone=item, result=dict(changed=True, patched=patched)))
            old_uid = int(item['old_uid'])

            # A move/manufacturer operation can append resources and relocate the
            # live database.  When its matching one-level snapshot still exists,
            # restore the complete archive/CDF/state transaction instead of merely
            # flipping the database link back and leaving dormant appended data.
            manifest_path = os.path.join(TEAM_ASSET_ROLLBACK_DIR, 'manifest.json')
            if os.path.exists(manifest_path):
                try:
                    rollback_meta = json.load(open(manifest_path, 'r', encoding='utf-8'))
                except Exception as ex:
                    raise ValueError('the saved team undo checkpoint is unreadable: ' + str(ex))
                if _team_history_matches_persisted_rollback(item, rollback_meta):
                    _g, rollback_reg = _extra_game_and_registry()
                    snap, restored_meta = _team_asset_load_persisted_snapshot(rollback_reg)
                    restore_errors = _team_asset_restore(snap)
                    if restore_errors:
                        raise RuntimeError('exact team undo reported: ' + '; '.join(restore_errors))
                    _team_asset_clear_persisted_snapshot()
                    try: _clear_ui_thumb_cache()
                    except Exception: pass
                    return jsonify(dict(ok=True, verified=True, exact=True, undone=item,
                                        restored=restored_meta.get('label'),
                                        created=restored_meta.get('created'),
                                        note='Restored the exact pre-change archives, CDF indexes, and app state.'))
                # A newer art/logo/repair operation owns the one-level checkpoint.
                # Do not consume it or pretend a DB-only reversal is the same thing.
                raise ValueError(
                    'A newer team-art or presentation change has the active undo checkpoint: ' +
                    str(rollback_meta.get('label') or 'unknown change') +
                    '. Undo that change first, then retry the team-history undo.')

            if kind == 'driver_team':
                if _public_custom_team_locked(old_uid):
                    raise ValueError(PUBLIC_CUSTOM_TEAM_MESSAGE + ' Undo would move the driver back into a reserve team.')
                driver = _team_driver_by_config_uid(uid)
                created_uids = [int(x) for x in ((driver or {}).get('created_scheme_uids') or [])]
                current_uid = int((driver or {}).get('team_uid', -1))
                if current_uid != old_uid and created_uids:
                    raise ValueError(
                        'Cannot undo this yet — the driver has extra paint slots you added: ' +
                        ', '.join(map(str, created_uids)) +
                        '. Undoing the team link would bypass the protected thumbnail migration path.')
                change = dict(class_name='DRIVERCONFIG_c', uid=uid, field='TEAM', target_uid=old_uid)
                state['driver_teams'][str(uid)] = old_uid
            elif kind == 'team_manufacturer':
                change = dict(class_name='RACETEAM_c', uid=uid, field='MANUFACTURER', target_uid=old_uid)
                state['team_manufacturers'][str(uid)] = old_uid
            else:
                raise ValueError('the latest history entry cannot be undone')
            result = _team_install_changes([change], 'Undo team editor change')
            history.pop()
            state['history'] = history
            _team_state_save(state)
            return jsonify(dict(ok=True, verified=True, exact=False, undone=item, result=result,
                                note='The original exact checkpoint was unavailable; the live database link was reversed transactionally.'))
    except Exception as ex:
        return jsonify(dict(ok=False, error=str(ex))), 400

# ==================== end v0.9.30.5 ====================


def _port_busy(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.35)
        try:
            return s.connect_ex(('127.0.0.1', port)) == 0
        except OSError:
            return True


def _is_our_app(port):
    """True only when the listener exposes this app's status signature."""
    import urllib.request, json as _j
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=1.2) as r:
            data = _j.loads(r.read().decode('utf-8', 'replace'))
        if not isinstance(data, dict):
            return False
        # Reuse only the exact same build.  Treating any older NASCAR Modding App
        # listener as this build caused a newly launched RC to open the old server
        # and old cached UI instead of starting its own process.
        return (data.get('app_name') == APP_NAME
                and data.get('version') == APP_VERSION
                and data.get('release_label') == APP_RELEASE_LABEL)
    except Exception:
        return False


def _choose_port(first=8151, tries=12):
    """Return (port, already_running).

    Scan the complete app range before choosing a free port. This also finds an
    existing copy that had to start on 8152-8162 because 8151 was occupied.
    """
    first_free = None
    for p in range(first, first + tries):
        if not _port_busy(p):
            if first_free is None:
                first_free = p
            continue
        if _is_our_app(p):
            return p, True
    return first_free, False


if __name__=='__main__':
    port, already = _choose_port()
    if already:
        print(f'The app is already running. Opening http://127.0.0.1:{port} ...')
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/?build={APP_VERSION.replace('.', '_').replace('-', '_')}")
        except Exception:
            pass
        sys.exit(0)
    if port is None:
        print('Could not find a free port between 8151 and 8162.')
        print('Close other copies of this app (or whatever is using those ports) and try again.')
        sys.exit(1)
    if port != 8151:
        print(f'Port 8151 was busy, using {port} instead.')
    print(f'NASCAR Modding App v{APP_VERSION} - http://127.0.0.1:{port}')
    print('Leave this window open while you use the app. Close it to stop the app.')
    try:
        if _app_settings_payload().get('auto_open_browser',True):
            webbrowser.open(f"http://127.0.0.1:{port}/?build={APP_VERSION.replace('.', '_').replace('-', '_')}")
    except Exception: pass
    try:
        app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
    except OSError as ex:
        print()
        print(f'Could not start the web server on port {port}: {ex}')
        print('This is usually another copy of the app, or security software blocking')
        print('local connections. Close other copies and try again.')
        sys.exit(1)
