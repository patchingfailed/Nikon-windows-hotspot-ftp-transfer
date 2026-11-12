"""
Nikon Windows Hotspot FTP Transfer
==================================
Receives images over Wi-Fi FTP from a Nikon camera (e.g. Z8/Z9) on a Windows laptop,
without a router and without cables - built for cameras in underwater housings.

Flow:
  1. Disconnect the internal Wi-Fi adapter from any network (otherwise the hotspot sticks to its channel)
  2. Start the Windows hotspot on 5 GHz (sharing source: internet > other connection > loopback)
  3. FTP server on the hotspot IP, receive files as .part, verify, discard duplicates
  4. Web dashboard at http://127.0.0.1:8765 with live status, progress and diagnostics
  5. On exit: hotspot off, restore Wi-Fi and power settings

Settings: config.json next to the program (created on first start).
Start:  python nikon_ftp_transfer.py   (or Start.cmd / NikonFTPTransfer.exe)
Stop:   "Quit" in the web dashboard or Ctrl+C in the console window

All user-visible texts come from locales/de.json and locales/en.json (see tr()).
"""
import collections
import ctypes
import hashlib
import json
import logging
import os
import re
import secrets
import string
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import DTPHandler, FTPHandler
    from pyftpdlib.servers import FTPServer
except ImportError:
    # runs before the translation layer exists, so this message is English only
    print("pyftpdlib is missing. Install it with:  python -m pip install pyftpdlib")
    input("Press Enter to exit ...")
    sys.exit(1)

APP_NAME = "Nikon Windows Hotspot FTP Transfer"
APP_VERSION = "1.0.0"
APP_CODENAME = "Manta"                # see .github/release-names.json
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))

# ============================================================== Translations
LANGS = ("de", "en")
LANG = "en"                 # effective language, set after the config is loaded
_STRINGS = {}               # lang -> {key: template}


def _strings(lang):
    """Load locales/<lang>.json (bundled copy first, then next to the program). Cached."""
    if lang not in _STRINGS:
        data = {}
        for d in (RES_DIR / "locales", APP_DIR / "locales"):
            f = d / f"{lang}.json"
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8-sig"))
                    break
                except (OSError, ValueError):
                    pass
        _STRINGS[lang] = data
    return _STRINGS[lang]


def ref(key, **params):
    """A translatable value that can be passed as a parameter of another text (rendered by tr())."""
    return {"key": key, "params": params}


def tr(key, lang=None, **params):
    """Text for `key` in `lang` (default: current language); falls back to English, then to the key.
    Parameters that are ref() values are rendered in the same language. Never raises."""
    if not key:
        return ""
    lang = lang or LANG
    tpl = _strings(lang).get(key)
    if tpl is None:
        tpl = _strings("en").get(key)
    if tpl is None:
        return str(key)
    try:
        vals = {k: render(v, lang) for k, v in params.items()}
        return tpl.format(**vals)
    except Exception:
        return tpl


def render(value, lang=None):
    """Render a ref() value to text; plain values (str, None) pass through."""
    if isinstance(value, dict) and "key" in value:
        return tr(value["key"], lang, **(value.get("params") or {}))
    return value


def detect_language():
    """'de' if the Windows UI language (or the locale elsewhere) is German, else 'en'."""
    try:
        if sys.platform == "win32":
            return "de" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x07 else "en"
        import locale
        loc = locale.getlocale()[0] or os.environ.get("LANG") or ""
        return "de" if loc.lower().startswith("de") else "en"
    except Exception:
        return "en"


def resolve_language(setting):
    return setting if setting in LANGS else detect_language()


def _rand(n, alphabet=string.ascii_letters + string.digits):
    alphabet = "".join(c for c in alphabet if c not in "0O1lI")
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ============================================================== Settings
# Defaults. Overridden by config.json (next to the program).
DEFAULTS = {
    "ssid": "nikon-ftp",               # hotspot name
    "passphrase": "",                  # Wi-Fi password; empty = keep the Windows password
    "hotspot_adapter_match": "",       # empty = pick the internal (non-USB) Wi-Fi adapter automatically
    "hotspot_ip": "192.168.137.1",     # default IP of the Windows hotspot
    "ftp_port": 2121,                  # 21 is often taken already
    "pasv_ports": [40000, 40100],      # inside the range named by Nikon, 32768-61000
    "user": "nikon",
    "password": "",                    # empty = generated randomly on first start
    "target": "~/Pictures/Nikon-FTP-Import",   # ~ = user folder
    "ui_port": 8765,
    "auto_network": True,              # disconnect the adapter and control the hotspot ourselves
    "open_browser": True,
    "status_interval": 4,              # seconds between network checks
    "scan_interval": 30,               # seconds between channel scans
    "slow_mbps": 5.0,                  # below this there is a hint
    "keep_awake": True,                # block sleep while transferring
    "awake_idle_min": 10,              # ... and for this many minutes afterwards (0 = always)
    "lid_keep_running": False,         # lid closed = keep running
    "file_pattern": r"^(?P<prefix>_?[A-Za-z0-9]{3}_?)(?P<num>\d{4})\.",   # Nikon: DSC_0001.NEF, _NZ81234.NEF
    "number_max": 9999,                # after 9999 comes 0001 again
    "session_end_idle_s": 60,          # this long without transfer = upload probably finished
    "language": "auto",                # auto | de | en (dashboard language)
}
TEST_MODE = sys.platform != "win32" or os.environ.get("NFT_TEST") == "1"
DEMO_MODE = TEST_MODE and os.environ.get("NFT_DEMO") == "1"   # sample data for the dashboard
CONFIG_FILE = Path(os.environ.get("NFT_CONFIG") or (APP_DIR / "config.json"))


def load_config():
    cfg = dict(DEFAULTS)
    user = {}
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            print(tr("msg.config_unreadable", "en", error=e))
            user = {"_invalid": True}
    cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    changed = False
    if not cfg["password"]:
        cfg["password"] = _rand(10)
        changed = True
    if not CONFIG_FILE.exists() or changed:
        if not user.get("_invalid"):
            out = {k: cfg[k] for k in DEFAULTS}
            try:
                CONFIG_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                print(tr("msg.config_write_failed", "en", error=e))
    return cfg


def set_language(value):
    """Switch the dashboard language and persist only the `language` key in config.json."""
    global LANG
    value = value if value in LANGS or value == "auto" else "auto"
    CONFIG["language"] = value
    LANG = resolve_language(value)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")) if CONFIG_FILE.exists() else {}
        if isinstance(data, dict):
            data["language"] = value
            CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError) as e:
        print(tr("msg.config_write_failed", "en", error=e))
    return LANG


CONFIG = load_config()
LANG = os.environ.get("NFT_LANG") if os.environ.get("NFT_LANG") in LANGS else resolve_language(CONFIG["language"])
CONFIG["pasv_ports"] = tuple(CONFIG["pasv_ports"])
CONFIG["target"] = Path(os.path.expandvars(os.path.expanduser(str(CONFIG["target"]))))
FILE_RE = re.compile(CONFIG["file_pattern"])
SCAN_INTERVAL = CONFIG["scan_interval"]
if TEST_MODE:
    CONFIG["hotspot_ip"] = os.environ.get("NFT_IP", "127.0.0.1")
    CONFIG["auto_network"] = False
    if os.environ.get("NFT_TARGET"):
        CONFIG["target"] = Path(os.environ["NFT_TARGET"])
    if os.environ.get("NFT_NO_BROWSER"):
        CONFIG["open_browser"] = False

DISPLAY_IP = DEFAULTS["hotspot_ip"] if DEMO_MODE else CONFIG["hotspot_ip"]   # demo: show the real address
TARGET: Path = CONFIG["target"]
TOOL_DIR = TARGET / "_transfer"
PART = ".part"
TARGET.mkdir(parents=True, exist_ok=True)
TOOL_DIR.mkdir(parents=True, exist_ok=True)
NET_STATE_FILE = TOOL_DIR / "network_state.json"

# ------------------------------------------------------------------ Logging (always English)
log =logging.getLogger("nft")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
_fh = logging.FileHandler(TOOL_DIR / "transfer.log", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
log.addHandler(_fh)
log.addHandler(_sh)

# FTP command log (every line from the camera), separate for error analysis
ftplog = logging.getLogger("pyftpdlib")
ftplog.setLevel(logging.DEBUG)
ftplog.propagate = False
_ffh = logging.FileHandler(TOOL_DIR / "ftp_debug.log", encoding="utf-8")
_ffh.setFormatter(_fmt)
ftplog.addHandler(_ffh)


# ------------------------------------------------------------------ State
def new_session():
    return {"start": time.time(), "names": set(), "items": [], "item_set": set(), "new_items": set(),
            "file_ts": collections.deque(maxlen=60), "item_ts": collections.deque(maxlen=60)}


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.events = collections.deque(maxlen=500)
        self.files = collections.deque(maxlen=500)
        self.active = {}
        self.done_files = 0
        self.done_bytes = 0
        self.done_secs = 0.0
        self.dups = 0
        self.aborted = 0
        self.renamed = 0
        self.warn_files = 0
        self.rates = collections.deque(maxlen=60)
        self.rate_hist = collections.deque(maxlen=4000)   # (t, MB/s) per second, a bit over 1 h
        self.scan = {}
        self.scan_ts = 0.0
        self.scan_error = None
        self.scan_now = threading.Event()
        self.power = {"awake": False, "lid": False, "awake_eff": False, "lid_eff": False,
                      "lid_orig": None, "error": None, "left": None}
        self.last_activity = time.time()
        self.power_poke = threading.Event()
        self.started = time.time()
        self.net = {}
        self.net_ts = 0.0
        self.net_error = None
        self.ftp = {"running": False, "addr": None, "error": None, "sessions": 0,
                    "connected": 0, "last_connect": None, "login_fails": 0}
        self.first_client_ts = None
        self.last_hotspot_action = 0.0
        self.hotspot_restarts = 0
        self.rebind = False
        self.stop = threading.Event()
        self.index = {}
        self.index_ready = False
        self.nums = {}            # prefix -> {number: {names of incomplete files}}
        self.sess = new_session()
        self.expect = None        # expectation for progress/remaining time
        self.expect_events = set()


S = State()


def touch():
    """Remember the last transfer activity (for the keep-awake timer)."""
    S.last_activity = time.time()


def event(level, key, **params):
    """Log an event. Stored as key + params and rendered when read, so a language switch re-renders
    the history; the log file and console always get the English text."""
    getattr(log, level if level != "ok" else "info")(tr(key, "en", **params))
    with S.lock:
        S.events.append({"ts": time.time(), "level": level, "key": key, "params": params})


# ------------------------------------------------------------------ Files
def quick_hash(path: Path, size: int) -> str:
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def build_index():
    n = 0
    for p in TARGET.iterdir():
        if not p.is_file() or p.name.endswith(PART):
            continue
        try:
            size = p.stat().st_size
            key = (size, quick_hash(p, size))
            k = num_key(p.name)
            with S.lock:
                S.index.setdefault(key, p.name)
                if k:
                    S.nums.setdefault(k[0], {}).setdefault(k[1], set())
            n += 1
        except OSError:
            pass
    with S.lock:
        S.index_ready = True
    event("info", "ev.dup_index_done", count=n)


_TSZ = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4, 16: 8}
_TFMT = {3: "H", 4: "I", 13: "I", 16: "Q"}


def tiff_data_end(path: Path, size: int):
    """End of the image data according to the TIFF/NEF structure (strips, tiles, JPEG preview incl. SubIFDs).
    Returns None if the structure is not readable."""
    with open(path, "rb") as f:
        hdr = f.read(8)
        if hdr[:2] == b"II":
            e = "<"
        elif hdr[:2] == b"MM":
            e = ">"
        else:
            return None
        if len(hdr) < 8 or struct.unpack(e + "H", hdr[2:4])[0] != 42:
            return None
        todo, seen, ends = [struct.unpack(e + "I", hdr[4:8])[0]], set(), []

        def values(typ, cnt, raw):
            fmt = _TFMT.get(typ)
            if not fmt or cnt > 200000:
                return []
            nbytes = cnt * _TSZ[typ]
            if nbytes <= 4:
                data = raw[:nbytes]
            else:
                off = struct.unpack(e + "I", raw)[0]
                if off + nbytes > size:
                    return []
                pos = f.tell()
                f.seek(off)
                data = f.read(nbytes)
                f.seek(pos)
            return list(struct.unpack(e + fmt * cnt, data[: cnt * struct.calcsize(fmt)]))

        while todo and len(seen) < 64:
            off = todo.pop()
            if off in seen or off < 8 or off + 2 > size:
                continue
            seen.add(off)
            f.seek(off)
            n = struct.unpack(e + "H", f.read(2))[0]
            if n == 0 or n > 1000:
                return None
            ents = f.read(12 * n)
            if len(ents) < 12 * n:
                return None
            nxt_raw = f.read(4)
            tags = {}
            for i in range(n):
                tag, typ, cnt = struct.unpack(e + "HHI", ents[i * 12: i * 12 + 8])
                if tag in (273, 279, 324, 325, 330, 513, 514):
                    tags[tag] = values(typ, cnt, ents[i * 12 + 8: i * 12 + 12])
            for a, b in ((273, 279), (324, 325)):
                if tags.get(a) and tags.get(b):
                    ends += [o + c for o, c in zip(tags[a], tags[b])]
            if tags.get(513) and tags.get(514):
                ends.append(tags[513][0] + tags[514][0])
            todo += tags.get(330, [])
            if len(nxt_raw) == 4:
                nxt = struct.unpack(e + "I", nxt_raw)[0]
                if nxt:
                    todo.append(nxt)
        return max(ends) if ends else None


def verify(path: Path):
    """(True|False|None, key, params). None = file type is not checked. key is a locale key (None when fine)."""
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
        if ext in (".nef", ".nrw", ".tif", ".tiff"):
            end = tiff_data_end(path, size)
            if end is None:
                return False, "verify.tiff_unreadable", {}
            if end > size:
                return False, "verify.truncated", {"end_mb": f"{end / 1e6:.1f}", "size_mb": f"{size / 1e6:.1f}"}
            return True, None, {}
        if ext in (".jpg", ".jpeg"):
            with open(path, "rb") as f:
                head = f.read(2)
                f.seek(-2, os.SEEK_END)
                tail = f.read(2)
            if head != b"\xff\xd8":
                return False, "verify.no_jpeg_head", {}
            if tail != b"\xff\xd9":
                return False, "verify.jpeg_no_end", {}
            return True, None, {}
        if ext in (".mov", ".mp4"):
            with open(path, "rb") as f:
                ok = f.read(12)[4:8] == b"ftyp"
            return (True, None, {}) if ok else (False, "verify.no_mp4_head", {})
    except (OSError, struct.error) as e:
        return False, "verify.check_failed", {"error": str(e)}
    return None, None, {}


def is_prefix(small: Path, big: Path, n: int) -> bool:
    """True if the first n bytes of small and big are identical."""
    with open(small, "rb") as a, open(big, "rb") as b:
        left = n
        while left > 0:
            k = min(left, 4 << 20)
            if a.read(k) != b.read(k):
                return False
            left -= k
    return True


def drop_from_index(name: str):
    with S.lock:
        for k in [k for k, v in S.index.items() if v == name]:
            S.index.pop(k, None)


def unique_name(dest: Path) -> Path:
    i = 2
    while True:
        cand = dest.with_name(f"{dest.stem}_{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def finalize(part_path: str, info: dict):
    part = Path(part_path)
    final = Path(part_path[: -len(PART)]) if part_path.endswith(PART) else part
    secs = max(time.time() - info.get("t0", time.time()), 1e-6)
    try:
        size = part.stat().st_size
        qh = quick_hash(part, size)
    except OSError as e:
        event("error", "ev.unreadable_after_receive", name=final.name, error=str(e))
        return
    rec = {"ts": time.time(), "name": final.name, "size": size, "secs": secs,
           "mbps": size / 1e6 / secs, "status": "ok", "note_key": None, "note_params": {}}
    with S.lock:
        dup = S.index.get((size, qh))
    old_size = final.stat().st_size if final.exists() else None
    if dup and (TARGET / dup).exists():
        part.unlink(missing_ok=True)
        rec["status"], rec["note_key"], rec["note_params"] = "dup", "file.identical", {"dup": dup}
        with S.lock:
            S.dups += 1
        event("info", "ev.dup_discarded", name=final.name, dup=dup)
    elif old_size is not None and size < old_size and is_prefix(part, final, size):
        # shorter partial copy of an existing file
        part.unlink(missing_ok=True)
        rec["status"], rec["note_key"], rec["note_params"] = "dup", "file.partial_dropped", {"name": final.name}
        with S.lock:
            S.dups += 1
        event("warning", "ev.partial_discarded", name=final.name, size_mb=f"{size / 1e6:.1f}",
              old_mb=f"{old_size / 1e6:.1f}")
    else:
        dest = final
        if old_size is not None and old_size < size and is_prefix(final, part, old_size):
            drop_from_index(final.name)
            with S.lock:
                for old in S.files:
                    if old["name"] == final.name and old["status"] in ("ok", "warn"):
                        old["status"], old["note_key"], old["note_params"] = "replaced", "file.replaced_by_full", {}
            rec["status"], rec["note_key"], rec["note_params"] = "ok", "file.replaces_truncated", {"old_mb": f"{old_size / 1e6:.1f}"}
            event("warning", "ev.truncated_replaced", name=final.name, old_mb=f"{old_size / 1e6:.1f}")
        elif old_size is not None:
            dest = unique_name(final)
            rec["status"], rec["note_key"], rec["note_params"] = "renamed", "file.name_taken", {"dest": dest.name}
            with S.lock:
                S.renamed += 1
            event("warning", "ev.name_clash", name=final.name, dest=dest.name)
        os.replace(part, dest)
        rec["name"] = dest.name
        with S.lock:
            S.index[(size, qh)] = dest.name
        ok, vkey, vparams = verify(dest)
        rec["verified"] = ok
        if ok is False:
            rec["status"], rec["note_key"], rec["note_params"] = "warn", vkey, vparams
            with S.lock:
                S.warn_files += 1
            event("warning", "ev.verify_warn", name=dest.name, note=ref(vkey, **vparams))
    touch()
    note_received(final.name, rec["status"])
    with S.lock:
        if rec["status"] != "dup":
            S.done_files += 1
        S.done_bytes += size
        S.done_secs += secs
        S.files.appendleft(rec)
    if rec["status"] in ("ok", "renamed"):
        log.info("OK  %-26s %7.1f MB  %5.1f s  %5.1f MB/s", rec["name"], size / 1e6, secs, rec["mbps"])


# ------------------------------------------------------------------ Image numbers & progress
NMAX = int(CONFIG["number_max"])


def num_key(name):
    """('NZ8', 6911) from 'NZ8_6911.NEF' or '_NZ86911.NEF'; None if the pattern does not match."""
    m = FILE_RE.match(Path(name).name)
    if not m:
        return None
    num = int(m.group("num"))
    if not 1 <= num <= NMAX:
        return None
    return m.group("prefix").replace("_", "").upper(), num


def nstep(n, k):
    """k steps further in the number sequence 1..NMAX (after NMAX comes 1)."""
    return (n - 1 + k) % NMAX + 1


def ndist(a, b):
    """Steps from a forward to b (0..NMAX-1)."""
    return (b - a) % NMAX


def circ_max(nums):
    """Newest number of a set (possibly wrapped past 9999): the one before the largest gap."""
    srt = sorted(set(nums))
    if not srt:
        return None
    if len(srt) == 1:
        return srt[0]
    best, gap = srt[-1], -1
    for i, a in enumerate(srt):
        g = ndist(a, srt[(i + 1) % len(srt)]) or NMAX
        if g > gap:
            best, gap = a, g
    return best


def parse_number(text):
    """'7100', 'NZ8_7100.NEF', '_NZ87100' -> (prefix or None, 7100)."""
    t = str(text).strip()
    k = num_key(t if "." in t else t + ".x")
    if k:
        return k
    m = re.search(r"(\d{1,4})\D*$", t)
    if m and 1 <= int(m.group(1)) <= NMAX:
        return None, int(m.group(1))
    return None


def note_received(name, status):
    """After every received file: update the session and the number index."""
    k = num_key(name)
    now = time.time()
    with S.lock:
        ss = S.sess
        if name not in ss["names"]:
            ss["names"].add(name)
            ss["file_ts"].append(now)
        if not k:
            return
        d = S.nums.setdefault(k[0], {})
        was_present = k[1] in d
        bad = d.setdefault(k[1], set())
        if status == "warn":
            bad.add(name)
        elif status in ("ok", "renamed"):
            bad.discard(name)
        if k not in ss["item_set"]:
            ss["item_set"].add(k)
            ss["items"].append(k)
            ss["item_ts"].append(now)
            if not was_present:
                ss["new_items"].add(k)


def _prefix_for(e):
    with S.lock:
        if e.get("prefix"):
            return e["prefix"]
        if S.sess["items"]:
            return S.sess["items"][0][0]
        if S.nums:
            return max(S.nums, key=lambda p: len(S.nums[p]))
    return None


def _direction():
    with S.lock:
        seq = [n for _, n in S.sess["items"][:6]]
    ups = sum(1 for a, b in zip(seq, seq[1:]) if 0 < ndist(a, b) < 200)
    downs = sum(1 for a, b in zip(seq, seq[1:]) if 0 < ndist(b, a) < 200)
    if ups > downs:
        return "up"
    if downs > ups:
        return "down"
    return None


def _sec_per_item(ts):
    ts = list(ts)[-16:]
    diffs = [b - a for a, b in zip(ts, ts[1:]) if 0 < b - a < 60]
    return sum(diffs) / len(diffs) if len(diffs) >= 2 else None


def progress():
    """Progress against the expectation. Without an expectation only the session counters are returned.
    Texts are codes (unit, anchor, waiting, notes) that map to locale keys prog.<kind>.<code>."""
    e = S.expect
    with S.lock:
        files_sess = len(S.sess["names"])
        items_sess = len(S.sess["items"])
        file_ts, item_ts = list(S.sess["file_ts"]), list(S.sess["item_ts"])
        active = bool(S.active)
    base = {"session_files": files_sess, "session_images": items_sess, "session_start": S.sess["start"]}
    if not e:
        return base
    out = dict(base, mode=e["mode"], value=e["value"], set_ts=e["ts"], notes=[], missing=[], missing_more=0)
    pre = _prefix_for(e)
    with S.lock:
        d = dict(S.nums.get(pre, {})) if pre else {}
        new_items = {n for p, n in S.sess["new_items"] if p == pre}
    good = {n for n, bad in d.items() if not bad}
    bad_nums = sorted(n for n, bad in d.items() if bad)
    out["prefix"] = pre
    if e["mode"] == "files":
        total, done = e["value"], files_sess
        spi = _sec_per_item(file_ts)
        out["unit"] = "files"
    else:
        out["unit"] = "images"
        spi = _sec_per_item(item_ts)
        base_nums = set(d) - new_items
        if e["mode"] == "last":
            last = e["value"]
            start = e.get("first")
            if start is None:
                cm = circ_max(base_nums)
                if cm is not None and ndist(cm, last) < NMAX // 2 and cm != last:
                    start, out["anchor"] = nstep(cm, 1), "after_last_existing"
                else:
                    with S.lock:
                        first_new = next((n for p, n in S.sess["items"] if p == pre), None)
                    start, out["anchor"] = first_new, "first_of_session"
            else:
                out["anchor"] = "given"
            if start is None:
                out["waiting"] = "need_first_file_range"
                return out
            span = ndist(start, last) + 1
            if span > 3000:
                out["notes"].append({"code": "range_too_big",
                                     "params": {"start": f"{start:04d}", "last": f"{last:04d}", "span": span}})
            rng = [nstep(start, i) for i in range(span)]
            total = span
            done = sum(1 for n in rng if n in good)
            missing = [n for n in rng if n not in good]
            out.update(first=start, last=last)
        else:  # card: N images on the card
            n_card = e["value"]
            first = e.get("first")
            if first is None:
                with S.lock:
                    seq = [n for p, n in S.sess["items"] if p == pre]
                if not seq:
                    out["waiting"] = "need_first_file_card"
                    return out
                direction = _direction()
                x0 = seq[0]
                if direction == "down":
                    first, out["anchor"] = nstep(x0, -(n_card - 1)), "estimated_newest_first"
                elif x0 in base_nums:
                    first, out["anchor"] = x0, "first_was_present"
                else:
                    first, misses = x0, 0
                    for i in range(1, n_card):
                        c = nstep(x0, -i)
                        if c in base_nums:
                            first, misses = c, 0
                        else:
                            misses += 1
                            if misses > 5:
                                break
                    out["anchor"] = "estimated_from_existing"
                out["first_estimated"] = True
            else:
                out["anchor"] = "given"
            present_after = [n for n in d if ndist(first, n) < NMAX // 2]
            newest = circ_max(present_after + [first])
            win = ndist(first, newest) + 1
            rng = [nstep(first, i) for i in range(win)]
            total = n_card
            done = sum(1 for n in rng if n in good)
            missing = [n for n in rng if n not in good]
            out.update(first=first, last=newest)
            if win > n_card + 2000:
                out["notes"].append({"code": "range_vs_card", "params": {}})
        out["missing"] = missing[:60]
        out["missing_more"] = max(0, len(missing) - 60)
        out["missing_count"] = len(missing)
        out["bad"] = bad_nums[:20]
    remaining = max(0, total - done)
    out.update(total=total, done=done, remaining=remaining, extra=max(0, done - total))
    out["direction"] = _direction()
    idle = time.time() - S.last_activity
    out["eta_s"] = remaining * spi if (spi and remaining and not (idle > CONFIG["session_end_idle_s"] and not active)) else None
    out["sec_per_item"] = spi
    state = "running"
    if total and done >= total:
        state = "complete"
    elif not active and idle > CONFIG["session_end_idle_s"] and (files_sess or items_sess):
        state = "stalled"
    ack = e.get("ack")
    if ack and e.get("ack_items") is not None and items_sess + files_sess > e["ack_items"]:
        e["ack"] = ack = None
    out["state"], out["ack"] = state, ack
    key = (e["ts"], state)
    if state in ("complete", "stalled") and key not in S.expect_events:
        S.expect_events.add(key)
        unit = ref(UNIT_KEYS[out["unit"]])
        if state == "complete":
            event("ok", "ev.expect_met", done=done, total=total, unit=unit)
        else:
            event("warning", "ev.expect_stalled", remaining=remaining, unit=unit)
    return out


UNIT_KEYS = {"files": "prog.unit.files", "images": "prog.unit.images"}
MODE_KEYS = {"files": "prog.mode.files", "last": "prog.mode.last", "card": "prog.mode.card"}


def set_expectation(body):
    """Returns (True, "") or (False, locale key of the error)."""
    mode = body.get("mode")
    raw = str(body.get("value", "")).strip()
    if mode not in ("files", "last", "card"):
        return False, "prog.err.unknown_mode"
    e = {"mode": mode, "ts": time.time(), "ack": None, "prefix": None}
    if mode == "last":
        pn = parse_number(raw)
        if not pn:
            return False, "prog.err.number_not_recognized"
        e["prefix"], e["value"] = pn
    else:
        m = re.findall(r"\d+", raw)
        if not m:
            return False, "prog.err.need_number"
        e["value"] = int(m[-1])   # '1 of 1900' -> 1900
        if e["value"] <= 0:
            return False, "prog.err.number_positive"
    first = str(body.get("first") or "").strip()
    if first:
        pn = parse_number(first)
        if not pn:
            return False, "prog.err.first_not_recognized"
        e["first"] = pn[1]
        e["prefix"] = e["prefix"] or pn[0]
    S.expect = e
    if first:
        event("info", "ev.expect_set_first", label=ref(MODE_KEYS[mode]), value=raw, first=first)
    else:
        event("info", "ev.expect_set", label=ref(MODE_KEYS[mode]), value=raw)
    return True, ""


# ------------------------------------------------------------------ FTP
class FastDTP(DTPHandler):
    ac_in_buffer_size = 1024 * 1024
    ac_out_buffer_size = 1024 * 1024


class CamHandler(FTPHandler):
    dtp_handler = FastDTP
    passive_ports = range(CONFIG["pasv_ports"][0], CONFIG["pasv_ports"][1] + 1)
    timeout = 600
    banner = "Nikon FTP Transfer ready."

    def on_connect(self):
        with S.lock:
            S.ftp["sessions"] += 1
            S.ftp["connected"] += 1
            S.ftp["last_connect"] = time.time()
            S.ftp["last_ip"] = self.remote_ip
        touch()
        event("info", "ev.ftp_connect", ip=self.remote_ip)

    def on_disconnect(self):
        with S.lock:
            S.ftp["connected"] = max(0, S.ftp["connected"] - 1)
            for k in [k for k, v in S.active.items() if v.get("h") == id(self)]:
                S.active.pop(k, None)
        event("info", "ev.ftp_disconnect")

    def on_login(self, username):
        event("ok", "ev.login_ok", user=username)

    def on_login_failed(self, username, password):
        with S.lock:
            S.ftp["login_fails"] += 1
        event("error", "ev.login_failed", user=username)

    def ftp_STOR(self, file, mode="w"):
        part = file if file.endswith(PART) else file + PART
        res = super().ftp_STOR(part, mode)
        touch()
        if res:
            with S.lock:
                S.active[part] = {"name": Path(file).name, "t0": time.time(), "h": id(self)}
        return res

    def on_file_received(self, file):
        with S.lock:
            info = S.active.pop(file, {})
        try:
            finalize(file, info)
        except Exception as e:  # never kill the FTP loop
            event("error", "ev.finalize_error", name=Path(file).name, error=str(e))

    def on_incomplete_file_received(self, file):
        with S.lock:
            info = S.active.pop(file, {})
            S.aborted += 1
        try:
            size = Path(file).stat().st_size
        except OSError:
            size = 0
        name = info.get("name", Path(file).name)
        with S.lock:
            S.files.appendleft({"ts": time.time(), "name": name, "size": size,
                                "secs": time.time() - info.get("t0", time.time()), "mbps": 0,
                                "status": "aborted", "note_key": "file.aborted", "note_params": {}})
        event("warning", "ev.aborted", name=name, size_mb=f"{size / 1e6:.1f}")


def make_server():
    auth = DummyAuthorizer()
    auth.add_user(CONFIG["user"], CONFIG["password"], str(TARGET), perm="elradfmwMT")
    CamHandler.authorizer = auth
    srv = FTPServer((CONFIG["hotspot_ip"], CONFIG["ftp_port"]), CamHandler)
    srv.max_cons = 8
    return srv


def ftp_loop():
    server = None
    last_try = 0.0
    last_err = None
    while not S.stop.is_set():
        if server is not None and S.rebind:
            event("warning", "ev.ftp_rebind")
            try:
                server.close_all()
            except Exception:
                pass
            server = None
            with S.lock:
                S.ftp["running"] = False
        if server is None:
            if time.time() - last_try > 3:
                last_try = time.time()
                try:
                    server = make_server()
                    with S.lock:
                        S.rebind = False
                        S.ftp.update(running=True, error=None,
                                     addr=f"{DISPLAY_IP}:{CONFIG['ftp_port']}")
                    event("ok", "ev.ftp_running", addr=f"{DISPLAY_IP}:{CONFIG['ftp_port']}")
                    last_err = None
                except OSError as e:
                    code = getattr(e, "winerror", None) or e.errno
                    if code in (10049, 99):
                        msg = ref("ev.ftp_waiting_ip", ip=CONFIG["hotspot_ip"])
                    elif code in (10013, 10048, 98, 13):
                        msg = ref("ev.ftp_port_busy", port=CONFIG["ftp_port"], error=str(e))
                    else:
                        msg = ref("ev.ftp_start_failed", error=str(e))
                    with S.lock:
                        S.ftp.update(running=False, error=msg)   # error is a ref(), rendered in api_state
                    if msg != last_err:
                        event("warning" if code in (10049, 99) else "error", msg["key"], **msg["params"])
                        last_err = msg
            time.sleep(0.5)
            continue
        try:
            server.serve_forever(timeout=0.5, blocking=False, handle_exit=False)
        except Exception as e:
            event("error", "ev.ftp_loop_error", error=str(e))
            try:
                server.close_all()
            except Exception:
                pass
            server = None
            with S.lock:
                S.ftp["running"] = False
    if server is not None:
        try:
            server.close_all()
        except Exception:
            pass


# ------------------------------------------------------------------ Network (PowerShell/WinRT)
PS_SCRIPT = r'''
param(
  [string]$Action = "status",
  [string]$Ssid = "nikon-ftp",
  [string]$Pass = "",
  [string]$HotspotMatch = "",
  [string]$HotspotIp = "192.168.137.1",
  [string]$Profiles = "",
  [string]$ScanIface = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$out = [ordered]@{ ok = $true; action = $Action; messages = @() }
function Say([string]$k, $p = @{}) { $script:out.messages += [ordered]@{ key = $k; params = $p } }

function Get-WlanInfo {
  $res = @{}; $cur = $null
  foreach ($l in (netsh wlan show interfaces)) {
    if ($l -match '^\s*Name\s*:\s*(.+?)\s*$') { $cur = $Matches[1]; $res[$cur] = [ordered]@{ name = $cur }; continue }
    if (-not $cur) { continue }
    $i = $res[$cur]
    if ($l -match '^\s*SSID\s*:\s*(.*?)\s*$') { $i.ssid = $Matches[1] }
    elseif ($l -match '^\s*Profile?\s*:\s*(.+?)\s*$') { $i.profile = $Matches[1] }
    elseif ($l -match '^\s*(Bereich|Band)\s*:\s*(.+?)\s*$') { $i.band = $Matches[2] }
    elseif ($l -match '^\s*(Kanal|Channel)\s*:\s*(\d+)') { $i.channel = [int]$Matches[2] }
    elseif ($l -match '^\s*Signal\s*:\s*(\d+)') { $i.signal = [int]$Matches[1] }
    elseif ($l -match '^\s*(Funktyp|Radio type)\s*:\s*(.+?)\s*$') { $i.radio = $Matches[2] }
    elseif ($l -match '^\s*(Empfangsrate|Receive rate).*:\s*([\d\.]+)') { $i.rx_mbit = [double]$Matches[2] }
  }
  return $res
}

function Get-Adapters {
  # Real Wi-Fi adapters only: netsh does not list the virtual Wi-Fi Direct / hotspot adapters
  $real = @((Get-WlanInfo).Keys)
  $wifi = @(Get-NetAdapter | Where-Object {
    $_.NdisPhysicalMedium -eq 9 -and
    $_.InterfaceDescription -notmatch 'Wi-Fi Direct|Virtual' -and
    ($real.Count -eq 0 -or $real -contains $_.Name) })
  if ($HotspotMatch) {
    $hs = $wifi | Where-Object { $_.InterfaceDescription -like "*$HotspotMatch*" -or $_.Name -eq $HotspotMatch } | Select-Object -First 1
  } else {
    # Internal adapter = not attached via USB; Windows hosts the hotspot there
    $hs = $wifi | Where-Object { "$($_.PnPDeviceID)" -notlike 'USB\*' } | Select-Object -First 1
    if (-not $hs) { $hs = $wifi | Select-Object -First 1 }
  }
  $up = $wifi | Where-Object { -not $hs -or $_.Name -ne $hs.Name } | Select-Object -First 1
  return @{ hs = $hs; up = $up }
}

function Init-WinRT {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
  $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
  try { $null = [Windows.Networking.NetworkOperators.TetheringWiFiBand,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] } catch {}
  $m = [System.WindowsRuntimeSystemExtensions].GetMethods()
  $script:asTaskOp = $m | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
  $script:asTaskAct = $m | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' } | Select-Object -First 1
}
function AwaitOp($op, [Type]$t) { $task = $script:asTaskOp.MakeGenericMethod($t).Invoke($null, @($op)); [void]$task.Wait(-1); return $task.Result }
function AwaitAct($a) { $task = $script:asTaskAct.Invoke($null, @($a)); [void]$task.Wait(-1) }

function Norm([string]$g) { return $g.Trim('{','}').ToLower() }

function Get-ShareSource($hsAdapter) {
  $ni = [Windows.Networking.Connectivity.NetworkInformation]
  $skip = @()
  if ($hsAdapter) { $skip += (Norm $hsAdapter.InterfaceGuid) }
  $vip = Get-NetIPAddress -IPAddress $HotspotIp -ErrorAction SilentlyContinue
  if ($vip) {
    $idx = @($vip)[0].InterfaceIndex
    $va = Get-NetAdapter -IncludeHidden | Where-Object { $_.ifIndex -eq $idx } | Select-Object -First 1
    if ($va) { $skip += (Norm $va.InterfaceGuid) }
  }
  $cands = @()
  $inet = $ni::GetInternetConnectionProfile()
  if ($inet) { $cands += ,@($inet, 'internet') }
  foreach ($p in @($ni::GetConnectionProfiles())) {
    if ($p.GetNetworkConnectivityLevel().ToString() -ne 'None') { $cands += ,@($p, 'local') }
  }
  foreach ($p in @($ni::GetConnectionProfiles())) { $cands += ,@($p, 'disconnected') }
  foreach ($c in $cands) {
    $p = $c[0]
    if (-not $p -or -not $p.NetworkAdapter) { continue }
    if ($skip -contains (Norm $p.NetworkAdapter.NetworkAdapterId.ToString())) { continue }
    $script:srcKind = $c[1]
    return $p
  }
  return $null
}

function Get-Tether($src) {
  if (-not $src) { return $null }
  return [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($src)
}

function Adapter-Info($a, $wl) {
  if (-not $a) { return $null }
  return [ordered]@{ name = $a.Name; desc = $a.InterfaceDescription; status = "$($a.Status)"; wlan = $wl[$a.Name] }
}

try {
  $ad = Get-Adapters
  $hs = $ad.hs; $up = $ad.up

  switch ($Action) {
    "status" {
      $wl = Get-WlanInfo
      $out.hotspot_adapter = Adapter-Info $hs $wl
      $out.uplink_adapter = Adapter-Info $up $wl
      $lb = Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | Where-Object { $_.InterfaceDescription -match 'Loopback|KM-TEST' } | Select-Object -First 1
      $out.loopback = if ($lb) { [ordered]@{ name = $lb.Name; status = "$($lb.Status)" } } else { $null }
      $out.hotspot_ip_present = [bool](Get-NetIPAddress -IPAddress $HotspotIp -ErrorAction SilentlyContinue)
      $ips = @{}
      foreach ($a in @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)) {
        if (-not $ips.ContainsKey($a.InterfaceAlias)) { $ips[$a.InterfaceAlias] = @() }
        $ips[$a.InterfaceAlias] += $a.IPAddress
      }
      $prof = @{}
      foreach ($p in @(Get-NetConnectionProfile -ErrorAction SilentlyContinue)) {
        $prof[$p.InterfaceAlias] = [ordered]@{ name = $p.Name; v4 = "$($p.IPv4Connectivity)" }
      }
      $list = @()
      foreach ($a in @(Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue)) {
        $real = @($ips[$a.Name] | Where-Object { $_ -and $_ -notlike '169.254.*' })
        $d = "$($a.InterfaceDescription)"
        $phys = ($a.NdisPhysicalMedium -eq 9 -or $a.NdisPhysicalMedium -eq 14)
        $isLb = $d -match 'Loopback|KM-TEST'
        if (-not ($phys -or $isLb -or ($a.Status -eq 'Up' -and $real.Count -gt 0))) { continue }
        $kind = if ($real -contains $HotspotIp) { 'hotspot' }
                elseif ($isLb) { 'loopback' }
                elseif ($d -match 'NordLynx|WireGuard|TAP-|OpenVPN|VPN|Tunnel|WLVPN') { 'vpn' }
                elseif ($a.NdisPhysicalMedium -eq 9) { 'wifi' }
                elseif ($a.NdisPhysicalMedium -eq 14) { 'ethernet' }
                elseif ($d -match 'Bluetooth') { 'bluetooth' }
                else { 'other' }
        $list += [ordered]@{ name = $a.Name; desc = $d; status = "$($a.Status)"; speed = "$($a.LinkSpeed)";
                             kind = $kind; ipv4 = $real; profile = $prof[$a.Name] }
      }
      $out.adapters = $list
      Init-WinRT
      $src = Get-ShareSource $hs
      $out.share_source = if ($src) { [ordered]@{ name = $src.ProfileName; kind = $script:srcKind; level = $src.GetNetworkConnectivityLevel().ToString() } } else { $null }
      $t = Get-Tether $src
      if ($t) {
        $cfg = $t.GetCurrentAccessPointConfiguration()
        $h = [ordered]@{ state = $t.TetheringOperationalState.ToString(); clients = $t.ClientCount; ssid = $cfg.Ssid; passphrase = $cfg.Passphrase }
        try { $h.band = $cfg.Band.ToString() } catch { $h.band = "?" }
        try { $h.client_list = @($t.GetTetheringClients() | ForEach-Object { [ordered]@{ mac = $_.MacAddress; names = (@($_.HostNames | ForEach-Object { $_.DisplayName }) -join ', ') } }) } catch { $h.client_list = @() }
        $out.hotspot = $h
      } else { $out.hotspot = $null }
    }
    "prepare" {
      if (-not $hs) { throw "No Wi-Fi adapter found for the hotspot (hotspot_adapter_match='$HotspotMatch')." }
      $wl = Get-WlanInfo
      $i = $wl[$hs.Name]
      if ($i -and $i.ssid -and $i.ssid -ne $Ssid) {
        $prof = if ($i.profile) { $i.profile } else { $i.ssid }
        $r1 = (netsh wlan set profileparameter "name=$prof" "interface=$($hs.Name)" connectionmode=manual | Out-String).Trim()
        $out.manual_set = ($LASTEXITCODE -eq 0)
        $out.manual_msg = $r1
        $r2 = (netsh wlan disconnect "interface=$($hs.Name)" | Out-String).Trim()
        $out.disconnected = $prof
        $out.disconnect_msg = $r2
        Start-Sleep -Seconds 2
      } else { $out.disconnected = $null }
    }
    "restore" {
      if (-not $hs) { throw "No Wi-Fi adapter found for the hotspot (hotspot_adapter_match='$HotspotMatch')." }
      $out.restored = @()
      $out.iface = $hs.Name
      foreach ($p in ($Profiles -split '\|' | Where-Object { $_ })) {
        $r = (netsh wlan set profileparameter "name=$p" "interface=$($hs.Name)" connectionmode=auto | Out-String).Trim()
        $out.restored += [ordered]@{ profile = $p; ok = ($LASTEXITCODE -eq 0); msg = $r }
      }
    }
    "start" {
      Init-WinRT
      $src = Get-ShareSource $hs
      if (-not $src) { throw "NO_SOURCE: No connection to share found." }
      $out.share_source = [ordered]@{ name = $src.ProfileName; kind = $script:srcKind }
      $T = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]
      try { if ($T::IsNoConnectionsTimeoutEnabled()) { AwaitAct ($T::DisableNoConnectionsTimeoutAsync()); Say 'ps.timeout_disabled' } } catch {}
      $t = Get-Tether $src
      $cfg = $t.GetCurrentAccessPointConfiguration()
      $need = $false
      if ($cfg.Ssid -ne $Ssid) { $cfg.Ssid = $Ssid; $need = $true }
      if ($Pass -and $cfg.Passphrase -ne $Pass) { $cfg.Passphrase = $Pass; $need = $true }
      try {
        $five = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::FiveGigahertz
        if ($cfg.Band -ne $five) {
          if ($cfg.IsBandSupported($five)) { $cfg.Band = $five; $need = $true } else { Say 'ps.band_unsupported' }
        }
      } catch { Say 'ps.band_failed' @{ error = $_.Exception.Message } }
      $RT = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]
      $st = $t.TetheringOperationalState.ToString()
      if ($st -eq 'On' -and -not $need) { Say 'ps.already_running' }
      else {
        if ($st -eq 'On') { $null = AwaitOp ($t.StopTetheringAsync()) $RT; Start-Sleep -Seconds 2 }
        if ($need) { AwaitAct ($t.ConfigureAccessPointAsync($cfg)); Say 'ps.configured' @{ ssid = $Ssid } }
        $r = AwaitOp ($t.StartTetheringAsync()) $RT
        $out.start_status = $r.Status.ToString()
        $out.start_msg = $r.AdditionalErrorMessage
        if ($out.start_status -ne 'Success') { $out.ok = $false; $out.error = "Hotspot start: $($out.start_status) $($r.AdditionalErrorMessage)" }
      }
    }
    "stop" {
      Init-WinRT
      $src = Get-ShareSource $hs
      if (-not $src) { $src = @([Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) | Where-Object { $_.NetworkAdapter } | Select-Object -First 1 }
      $t = Get-Tether $src
      if ($t -and $t.TetheringOperationalState.ToString() -eq 'On') {
        $RT = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]
        $r = AwaitOp ($t.StopTetheringAsync()) $RT
        $out.stop_status = $r.Status.ToString()
      } else { Say 'ps.not_active' }
    }
    "scan" {
      if (-not $ScanIface) { throw "No scan adapter given." }
      Init-WinRT
      $out.fresh = $false
      try {
        $null = [Windows.Devices.WiFi.WiFiAdapter,Windows.Devices.WiFi,ContentType=WindowsRuntime]
        $null = [Windows.Devices.WiFi.WiFiAccessStatus,Windows.Devices.WiFi,ContentType=WindowsRuntime]
        $null = AwaitOp ([Windows.Devices.WiFi.WiFiAdapter]::RequestAccessAsync()) ([Windows.Devices.WiFi.WiFiAccessStatus])
        $wl = AwaitOp ([Windows.Devices.WiFi.WiFiAdapter]::FindAllAdaptersAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.WiFi.WiFiAdapter]])
        $g = Norm ((Get-NetAdapter -Name $ScanIface).InterfaceGuid)
        foreach ($w in $wl) {
          if ((Norm $w.NetworkAdapter.NetworkAdapterId.ToString()) -eq $g) { AwaitAct ($w.ScanAsync()); $out.fresh = $true }
        }
      } catch { $out.scan_note = [ordered]@{ key = 'ps.scan_fallback'; params = @{ error = $_.Exception.Message } } }
      $nets = @(); $ssid = $null; $auth = $null; $cur = $null
      foreach ($l in (netsh wlan show networks "interface=$ScanIface" mode=bssid)) {
        if ($l -match '^SSID\s+\d+\s*:\s*(.*)$') { $ssid = $Matches[1].Trim(); $cur = $null; continue }
        if ($l -match '^\s*(Authentifizierung|Authentication)\s*:\s*(.+?)\s*$') { $auth = $Matches[2]; continue }
        if ($l -match '^\s*BSSID\s+\d+\s*:\s*(\S+)') { $cur = [ordered]@{ ssid = $ssid; bssid = $Matches[1]; auth = $auth }; $nets += $cur; continue }
        if (-not $cur) { continue }
        if ($l -match '^\s*Signal\s*:\s*(\d+)') { $cur.signal = [int]$Matches[1] }
        elseif ($l -match '^\s*Band\s*:\s*(.+?)\s*$') { $cur.band = $Matches[1] }
        elseif ($l -match '^\s*(Kanal|Channel)\s*:\s*(\d+)') { $cur.channel = [int]$Matches[2] }
        elseif ($l -match '^\s*(Funktyp|Radio type)\s*:\s*(.+?)\s*$') { $cur.radio = $Matches[2] }
        elseif ($l -match '(Verbundene Stationen|Connected Stations)\s*:\s*(\d+)') { $cur.stations = [int]$Matches[2] }
        elseif ($l -match '(Kanalnutzung|Channel Utilization)\s*:\s*\d+\s*\((\d+)\s*%\)') { $cur.util = [int]$Matches[2] }
      }
      $out.networks = $nets
    }
    "diag" {
      $d = [ordered]@{}
      $d["netsh wlan show interfaces"] = (netsh wlan show interfaces | Out-String)
      $d["netsh wlan show drivers"] = (netsh wlan show drivers | Out-String)
      $d["Get-NetAdapter -IncludeHidden"] = (Get-NetAdapter -IncludeHidden | Format-Table Name, InterfaceDescription, Status, LinkSpeed -AutoSize | Out-String -Width 220)
      $d["IPv4 addresses"] = (Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceAlias, IPAddress, PrefixOrigin -AutoSize | Out-String -Width 200)
      $d["Network profiles"] = (Get-NetConnectionProfile -ErrorAction SilentlyContinue | Format-Table Name, InterfaceAlias, NetworkCategory, IPv4Connectivity -AutoSize | Out-String -Width 200)
      $d["Ports 2121/21"] = (netstat -ano | Select-String ':2121 |:21 ' | Out-String)
      try {
        $d["Firewall rules python"] = (Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $_.Program -match 'python' } | ForEach-Object {
          $r = $_ | Get-NetFirewallRule; "$($r.DisplayName) | $($r.Direction) | $($r.Action) | Profile=$($r.Profile) | Enabled=$($r.Enabled) | $($_.Program)" } | Out-String)
      } catch { $d["Firewall rules python"] = "not readable: $($_.Exception.Message)" }
      $out.diag = $d
    }
    default { throw "Unknown action $Action" }
  }
} catch {
  $out.ok = $false
  $out.error = $_.Exception.Message
}
$out | ConvertTo-Json -Compress -Depth 6
'''


class Net:
    def __init__(self):
        self.ps = Path(tempfile.gettempdir()) / "nikon_ftp_transfer_net.ps1"
        self.ps.write_text(PS_SCRIPT, encoding="utf-8-sig")
        self.lock = threading.Lock()

    def run(self, action, timeout=60, **extra):
        args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(self.ps), "-Action", action, "-Ssid", CONFIG["ssid"],
                "-HotspotIp", CONFIG["hotspot_ip"]]
        if CONFIG["hotspot_adapter_match"]:
            args += ["-HotspotMatch", CONFIG["hotspot_adapter_match"]]
        if CONFIG["passphrase"]:
            args += ["-Pass", CONFIG["passphrase"]]
        if "profiles" in extra:
            args += ["-Profiles", "|".join(extra["profiles"])]
        if extra.get("scan_iface"):
            args += ["-ScanIface", extra["scan_iface"]]
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        with self.lock:
            try:
                p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=flags)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"PowerShell timeout during '{action}'"}
            except FileNotFoundError:
                return {"ok": False, "error": "PowerShell not found"}
        out = p.stdout.decode("utf-8", "replace").strip()
        lines = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
        if lines:
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError:
                pass
        err = p.stderr.decode("utf-8", "replace").strip() or out
        return {"ok": False, "error": f"PowerShell '{action}' returned no valid response: {err[-600:]}"}


NET = None if TEST_MODE else Net()


def load_manual_profiles():
    try:
        return json.loads(NET_STATE_FILE.read_text(encoding="utf-8")).get("manual_profiles", [])
    except (OSError, json.JSONDecodeError):
        return []


def save_manual_profiles(profiles):
    NET_STATE_FILE.write_text(json.dumps({"manual_profiles": sorted(set(profiles))}), encoding="utf-8")


def net_prepare():
    r = NET.run("prepare")
    if not r.get("ok"):
        event("error", "ev.net_prepare_failed", error=r.get("error"))
        return False
    prof = r.get("disconnected")
    if prof:
        if r.get("manual_set"):
            save_manual_profiles(load_manual_profiles() + [prof])
            event("info", "ev.adapter_released", profile=prof)
        else:
            event("warning", "ev.adapter_released_nopause", profile=prof, msg=r.get("manual_msg", "")[:120])
    return True


KIND_KEYS = {"internet": "ev.kind.internet", "local": "ev.kind.local", "disconnected": "ev.kind.disconnected"}


def ps_message(m):
    """A message from the PowerShell script ({key, params} or plain text) as a ref() value."""
    if isinstance(m, dict):
        return ref(m.get("key", ""), **(m.get("params") or {}))
    return ref(str(m))


def net_start_hotspot(reason):
    """reason: a ref() value that says why the hotspot is (re)started."""
    with S.lock:
        S.last_hotspot_action = time.time()
        S.hotspot_restarts += 1
    event("info", "ev.hotspot_starting", reason=reason)
    r = NET.run("start", timeout=90)
    for m in r.get("messages") or []:
        event("info", "ev.hotspot_msg", msg=ps_message(m))
    if r.get("ok"):
        src = r.get("share_source") or {}
        kind = src.get("kind")
        event("ok", "ev.hotspot_active", ssid=CONFIG["ssid"], source=src.get("name"),
              kind=ref(KIND_KEYS[kind]) if kind in KIND_KEYS else kind)
        with S.lock:
            S.rebind = True
        return True
    err = r.get("error", "")
    if "NO_SOURCE" in err:
        event("error", "ev.hotspot_no_source")
    else:
        event("error", "ev.hotspot_start_failed", error=err)
    return False


def supervisor():
    prev = {}
    if load_manual_profiles():
        restore_profiles()        # left over from a crashed run
    if CONFIG["auto_network"]:
        net_prepare()
        net_start_hotspot(ref("ev.reason.startup"))
    while not S.stop.is_set():
        st = NET.run("status")
        now = time.time()
        with S.lock:
            S.net_ts = now
            if st.get("ok"):
                S.net, S.net_error = st, None
            else:
                S.net_error = st.get("error")
        if not st.get("ok"):
            if prev.get("err") != st.get("error"):
                event("error", "ev.net_status_unreadable", error=st.get("error"))
            prev["err"] = st.get("error")
            S.stop.wait(CONFIG["status_interval"])
            continue
        prev["err"] = None
        hs = st.get("hotspot") or {}
        hsa = st.get("hotspot_adapter") or {}
        hs_wlan = hsa.get("wlan") or {}
        src = st.get("share_source") or {}
        cur = {
            "hs_state": hs.get("state"), "clients": hs.get("clients"), "band": hs.get("band"),
            "src": src.get("name"), "ip": st.get("hotspot_ip_present"), "intel_ssid": hs_wlan.get("ssid"),
            "up_ssid": ((st.get("uplink_adapter") or {}).get("wlan") or {}).get("ssid"),
        }
        labels = {"hs_state": "ev.label.hs_state", "clients": "ev.label.clients", "band": "ev.label.band",
                  "src": "ev.label.src", "ip": "ev.label.ip", "intel_ssid": "ev.label.intel_ssid",
                  "up_ssid": "ev.label.up_ssid"}
        for k, v in cur.items():
            if k in prev and prev[k] != v:
                lvl = "warning" if k in ("hs_state", "ip", "intel_ssid") else "info"
                event(lvl, "ev.net_change", label=ref(labels[k]), old=prev[k], new=v)
            prev[k] = v
        if (hs.get("clients") or 0) > 0:
            with S.lock:
                if S.first_client_ts is None:
                    S.first_client_ts = now
        else:
            with S.lock:
                S.first_client_ts = None
        if not st.get("hotspot_ip_present"):
            with S.lock:
                S.rebind = True
        if CONFIG["auto_network"]:
            since = now - S.last_hotspot_action
            if hs_wlan.get("ssid") and hs_wlan.get("ssid") != CONFIG["ssid"]:
                event("warning", "ev.adapter_reconnected", ssid=hs_wlan.get("ssid"))
                net_prepare()
                net_start_hotspot(ref("ev.reason.adapter_connected"))
            elif hs.get("state") not in ("On", "InTransition") and since > 20:
                net_start_hotspot(ref("ev.reason.state", state=hs.get("state")) if hs.get("state")
                                  else ref("ev.reason.state_unknown"))
        S.stop.wait(CONFIG["status_interval"])


def restore_profiles():
    """Set paused Wi-Fi profiles back to auto-connect. Returns True when nothing is left to restore."""
    profs = load_manual_profiles()
    if not profs:
        return True
    r = NET.run("restore", profiles=profs)
    ok = [x["profile"] for x in (r.get("restored") or []) if x.get("ok")]
    if r.get("ok") and len(ok) == len(profs):
        NET_STATE_FILE.unlink(missing_ok=True)
        event("ok", "ev.autoconnect_restored", profiles=", ".join(ok))
        return True
    left = [p for p in profs if p not in ok]
    save_manual_profiles(left)
    event("error", "ev.autoconnect_restore_failed", error=str(r.get("error") or r.get("restored")),
          profile=left[0], iface=r.get("iface") or ((S.net.get("hotspot_adapter") or {}).get("name")) or "Wi-Fi")
    return False


def net_cleanup():
    if TEST_MODE:
        return
    if CONFIG["auto_network"]:
        event("info", "ev.cleanup_start")
        r = NET.run("stop")
        if not r.get("ok"):
            event("warning", "ev.hotspot_stop_error", error=r.get("error"))
    restore_profiles()


# ------------------------------------------------------------------ Rate sampler
def sampler():
    while not S.stop.is_set():
        with S.lock:
            total = S.done_bytes
            if S.active:
                S.last_activity = time.time()
            for p in list(S.active):
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
            now = time.time()
            S.rates.append((now, total))
            if len(S.rates) >= 3:
                t0, b0 = S.rates[-3]
                S.rate_hist.append((round(now, 1), round(max(0.0, (total - b0) / max(now - t0, 1e-6) / 1e6), 2)))
        S.stop.wait(1)


def scanner():
    """Channel scan via the dongle - the hotspot on the internal adapter is not disturbed."""
    while not S.stop.is_set():
        with S.lock:
            up = (S.net.get("uplink_adapter") or {}).get("name")
        if up:
            r = NET.run("scan", timeout=60, scan_iface=up)
            with S.lock:
                if r.get("ok"):
                    S.scan = {"iface": up, "networks": r.get("networks") or [], "fresh": r.get("fresh"),
                              "note": r.get("scan_note")}
                    S.scan_ts, S.scan_error = time.time(), None
                else:
                    S.scan_error = r.get("error")
        S.scan_now.wait(SCAN_INTERVAL if up else 5)
        S.scan_now.clear()


def current_rate():
    """Mean of the last 5 seconds from the same series the chart shows."""
    with S.lock:
        h = list(S.rate_hist)[-6:]
    now = time.time()
    h = [v for t, v in h if now - t <= 6]
    return sum(h) / len(h) if h else 0.0


# ------------------------------------------------------------------ Power: keep awake, lid
POWER_OK = sys.platform == "win32"
LID_STATE_FILE = TOOL_DIR / "lid_state.json"
LID_KEYS = {0: "msg.lid.0", 1: "msg.lid.1", 2: "msg.lid.2", 3: "msg.lid.3"}
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


class TrError(Exception):
    """An error with a translatable text (locale key + params)."""
    def __init__(self, key, **params):
        super().__init__(tr(key, "en", **params))
        self.key, self.params = key, params


def err_ref(e):
    """An exception as a value for tr() parameters: translatable if it is a TrError, else its text."""
    return ref(e.key, **e.params) if isinstance(e, TrError) else str(e)


def lid_ref(v):
    return ref(LID_KEYS[v]) if v in LID_KEYS else v


def power_window(now, last_activity, active, idle_min):
    """(inside, remaining seconds). Inside = while transferring and idle_min minutes afterwards."""
    if active or not idle_min:
        return True, None
    left = idle_min * 60 - (now - last_activity)
    return left > 0, max(0, int(left))


def keep_awake_loop():
    """SetThreadExecutionState is per thread - hence a dedicated, permanently running thread.
    Decides every few seconds: keep awake / lid mode on while transferring
    and until awake_idle_min minutes afterwards."""
    k32 = None
    if POWER_OK:
        k32 = ctypes.windll.kernel32
        k32.SetThreadExecutionState.restype = ctypes.c_uint
    while not S.stop.is_set():
        with S.lock:
            active = bool(S.active)
        inside, left = power_window(time.time(), S.last_activity, active, CONFIG["awake_idle_min"])
        S.power["left"] = left
        want_awake = S.power["awake"] and inside
        want_lid = S.power["lid"] and inside
        if k32:
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want_awake else 0)
            if k32.SetThreadExecutionState(flags) == 0:
                S.power["error"] = ref("msg.awake_denied")
        if want_awake != S.power["awake_eff"]:
            S.power["awake_eff"] = want_awake
            if want_awake:
                event("info", "ev.awake_on")
            elif S.power["awake"]:
                event("info", "ev.awake_idle_end", minutes=CONFIG["awake_idle_min"])
            else:
                event("info", "ev.awake_off")
        if want_lid != S.power["lid_eff"]:
            lid_engage(want_lid)
        S.power_poke.wait(3)
        S.power_poke.clear()
    if k32:
        k32.SetThreadExecutionState(ES_CONTINUOUS)


def _powercfg(*args):
    p = subprocess.run(["powercfg", *args], capture_output=True, timeout=20, creationflags=0x08000000)
    out = (p.stdout or b"").decode("cp850", "replace") + (p.stderr or b"").decode("cp850", "replace")
    return p.returncode, out


def lid_query():
    code, out = _powercfg("/query", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION")
    vals = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})", out)]
    if code != 0 or len(vals) < 2:
        raise TrError("msg.lid_unreadable", out=out.strip()[:160])
    return {"ac": vals[-2], "dc": vals[-1]}


def lid_apply(ac, dc):
    for a, v in (("/setacvalueindex", ac), ("/setdcvalueindex", dc)):
        code, out = _powercfg(a, "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", str(v))
        if code != 0:
            raise TrError("msg.powercfg_failed", arg=a, out=out.strip()[:160])
    _powercfg("/setactive", "SCHEME_CURRENT")


def lid_engage(on: bool):
    """Actually switch the lid action (on) or restore it (off)."""
    if not POWER_OK:
        S.power["lid_eff"] = on
        return
    try:
        if on and not S.power["lid_eff"]:
            orig = lid_query()
            LID_STATE_FILE.write_text(json.dumps(orig), encoding="utf-8")
            lid_apply(0, 0)
            S.power.update(lid_eff=True, lid_orig=orig, error=None)
            event("ok", "ev.lid_on", ac=lid_ref(orig["ac"]), dc=lid_ref(orig["dc"]))
        elif not on and S.power["lid_eff"]:
            orig = S.power["lid_orig"] or json.loads(LID_STATE_FILE.read_text(encoding="utf-8"))
            lid_apply(orig["ac"], orig["dc"])
            LID_STATE_FILE.unlink(missing_ok=True)
            S.power.update(lid_eff=False, lid_orig=None, error=None)
            event("ok", "ev.lid_restored")
    except Exception as e:
        S.power["error"] = err_ref(e)
        S.power["lid"] = S.power["lid_eff"]   # the switch shows the real state
        event("error", "ev.lid_error", error=err_ref(e))


def lid_restore_stale():
    """If an earlier run crashed: restore the old lid setting."""
    if not POWER_OK or not LID_STATE_FILE.exists():
        return
    try:
        orig = json.loads(LID_STATE_FILE.read_text(encoding="utf-8"))
        lid_apply(orig["ac"], orig["dc"])
        LID_STATE_FILE.unlink(missing_ok=True)
        event("warning", "ev.lid_stale_restored")
    except Exception as e:
        event("error", "ev.lid_stale_failed", error=err_ref(e))


class _SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def battery():
    if not POWER_OK:
        return {"percent": 64, "ac": False} if DEMO_MODE else None
    s = _SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) or s.BatteryLifePercent == 255:
        return None
    return {"percent": int(s.BatteryLifePercent), "ac": s.ACLineStatus == 1}


# ------------------------------------------------------------------ Hints
def hints():
    """Current hints as [{level, key, params, action}]; render with render_hints()."""
    out = []

    def add(lvl, key, action=None, **params):
        out.append({"level": lvl, "key": key, "params": params, "action": action})

    with S.lock:
        net, net_err, ftp = dict(S.net), S.net_error, dict(S.ftp)
        aborted, fails, first_client = S.aborted, ftp["login_fails"], S.first_client_ts
        active = len(S.active)
    rate = current_rate()
    if TEST_MODE and not DEMO_MODE:
        add("info", "hint.test_mode", ip=CONFIG["hotspot_ip"])
    elif net_err:
        add("error", "hint.net_error", error=net_err)
    elif net:
        hsa = net.get("hotspot_adapter")
        hs = net.get("hotspot") or {}
        src = net.get("share_source")
        lb = net.get("loopback")
        if not hsa:
            add("error", "hint.no_adapter")
        elif (hsa.get("wlan") or {}).get("ssid") not in (None, "", CONFIG["ssid"]):
            add("warning", "hint.adapter_on_ssid", ssid=hsa["wlan"]["ssid"])
        if not src:
            if lb and lb.get("status") == "Disabled":
                add("error", "hint.loopback_disabled", "loopback_enable", name=lb["name"])
            elif lb and lb.get("status") == "Not Present":
                add("error", "hint.loopback_missing", "loopback_wizard", name=lb["name"])
            elif not lb:
                add("error", "hint.no_loopback", "loopback_wizard")
            else:
                add("error", "hint.no_source")
        elif src.get("kind") != "internet":
            add("info", "hint.source_no_internet", name=src.get("name"))
        if hs:
            if hs.get("state") != "On":
                add("error", "hint.hotspot_state_auto" if CONFIG["auto_network"] else "hint.hotspot_state",
                    state=hs.get("state"))
            if hs.get("band") not in (None, "?", "FiveGigahertz"):
                add("warning", "hint.band", band=hs.get("band"))
            if hs.get("ssid") and hs.get("ssid") != CONFIG["ssid"]:
                add("warning", "hint.ssid_mismatch", ssid=hs.get("ssid"), expected=CONFIG["ssid"])
        if not net.get("hotspot_ip_present"):
            add("warning", "hint.ip_missing", ip=CONFIG["hotspot_ip"])
        up = (net.get("uplink_adapter") or {}).get("wlan") or {}
        if up.get("ssid") and (up.get("signal") or 100) < 40:
            add("info", "hint.uplink_weak", signal=up.get("signal"), ssid=up.get("ssid"))
        ads = net.get("adapters") or []
        vpn = [a for a in ads if a.get("kind") == "vpn" and a.get("status") == "Up"]
        if vpn:
            add("warning", "hint.vpn", names=", ".join(a["name"] for a in vpn))
        eth = [a for a in ads if a.get("kind") == "ethernet" and a.get("status") == "Up"]
        if eth:
            add("info", "hint.ethernet", name=eth[0]["name"])
    with S.lock:
        scan, scan_err, scan_ts = dict(S.scan), S.scan_error, S.scan_ts
    nets = scan.get("networks") or []
    if scan_err:
        add("info", "hint.scan_failed", error=scan_err)
    if nets and net:
        up = (net.get("uplink_adapter") or {}).get("wlan") or {}
        ch = up.get("channel")
        if ch:
            same = [n for n in nets if n.get("channel") == ch]
            util = max([n.get("util") or 0 for n in same] or [0])
            if util >= 50:
                add("info", "hint.uplink_channel_busy", channel=ch, util=util, count=len(same))
        own = [n for n in nets if n.get("ssid") == CONFIG["ssid"]]
        if own:
            hch = own[0].get("channel")
            others = [n for n in nets if n.get("channel") == hch and n.get("ssid") != CONFIG["ssid"]]
            if others:
                u = max([n.get("util") or 0 for n in others] or [0])
                add("warning" if u >= 40 else "info", "hint.hotspot_channel_shared",
                    channel=hch, count=len(others), util=u)
    if ftp.get("error") and not ftp.get("running"):
        err = ftp["error"]    # a ref() value
        add("warning" if err["key"] == "ev.ftp_waiting_ip" else "error", err["key"], **err["params"])
    if first_client and not ftp.get("last_connect") and time.time() - first_client > 30:
        add("warning", "hint.no_ftp_connect", ip=CONFIG["hotspot_ip"], port=CONFIG["ftp_port"])
    if fails:
        add("error", "hint.login_failed", count=fails, user=CONFIG["user"])
    with S.lock:
        bad = [f["name"] for f in S.files if f.get("status") == "warn"]
    if bad:
        add("warning", "hint.bad_files", count=len(bad), names=", ".join(bad[:5]),
            more=" ..." if len(bad) > 5 else "")
    if aborted:
        add("warning", "hint.aborted", count=aborted)
    bat = battery()
    if bat and not bat["ac"] and bat["percent"] <= 20:
        add("warning" if bat["percent"] > 10 else "error", "hint.battery", percent=bat["percent"])
    if active and 0 < rate < CONFIG["slow_mbps"]:
        add("warning", "hint.slow", rate=f"{rate:.1f}")
    return out


def render_hints(raw, lang=None):
    return [{"level": h["level"], "text": tr(h["key"], lang, **h["params"]), "action": h["action"]} for h in raw]


# ------------------------------------------------------------------ Diagnostics (report is always English)
def diag_report():
    lines = [f"{APP_NAME} {APP_VERSION} ({APP_CODENAME}) - diagnostics  {datetime.now():%Y-%m-%d %H:%M:%S}", "=" * 70]
    with S.lock:
        ftp_info = dict(S.ftp, error=render(S.ftp.get("error"), "en"))
        lines += [
            f"Uptime: {int(time.time() - S.started)} s   Test mode: {TEST_MODE}",
            f"Config: SSID={CONFIG['ssid']} IP={CONFIG['hotspot_ip']} Port={CONFIG['ftp_port']} "
            f"PASV={CONFIG['pasv_ports']} Target={TARGET}",
            f"FTP: {json.dumps(ftp_info)}",
            f"Files: {S.done_files} ({S.done_bytes / 1e9:.2f} GB), duplicates {S.dups}, renamed {S.renamed}, "
            f"aborted {S.aborted}, warnings {S.warn_files}, hotspot starts {S.hotspot_restarts}",
            f"Current rate: {current_rate():.1f} MB/s",
            "", "--- Hints", *[f"[{h['level']}] {h['text']}" for h in render_hints(hints(), "en")],
            "", "--- Network status (latest)", json.dumps(S.net, indent=2, ensure_ascii=False),
        ]
        evs = list(S.events)[-250:]
    if NET:
        r = NET.run("diag", timeout=120)
        for k, v in (r.get("diag") or {}).items():
            lines += ["", f"--- {k}", (v or "").rstrip()]
        if not r.get("ok"):
            lines += ["", f"--- Diag error: {r.get('error')}"]
    lines += ["", "--- Events"]
    lines += [f"{datetime.fromtimestamp(e['ts']):%H:%M:%S} {e['level']:<7} {tr(e['key'], 'en', **e['params'])}"
              for e in evs]
    try:
        tail = (TOOL_DIR / "ftp_debug.log").read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        lines += ["", "--- FTP log (last 200 lines)", *tail]
    except OSError:
        pass
    text = "\n".join(lines)
    path = TOOL_DIR / f"diagnose_{datetime.now():%Y%m%d_%H%M%S}.txt"
    path.write_text(text, encoding="utf-8")
    event("info", "ev.diag_saved", path=str(path))
    return str(path), text


# ------------------------------------------------------------------ Web UI
def render_files(files):
    """Per-file records for the UI: note rendered in the current language, internal note_* fields dropped."""
    out = []
    for f in files:
        r = {k: v for k, v in f.items() if k not in ("note_key", "note_params")}
        r["note"] = tr(f.get("note_key"), **(f.get("note_params") or {}))
        out.append(r)
    return out


def api_state():
    with S.lock:
        active = []
        for p, v in S.active.items():
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            el = max(time.time() - v["t0"], 1e-6)
            active.append({"name": v["name"], "size": size, "secs": el, "mbps": size / 1e6 / el})
        stats = {
            "files": S.done_files, "bytes": S.done_bytes, "dups": S.dups, "aborted": S.aborted,
            "renamed": S.renamed, "warn": S.warn_files,
            "avg_mbps": (S.done_bytes / 1e6 / S.done_secs) if S.done_secs else 0,
            "cur_mbps": current_rate(), "uptime": time.time() - S.started,
            "hotspot_restarts": S.hotspot_restarts,
        }
        data = {
            "now": time.time(), "test_mode": TEST_MODE, "version": APP_VERSION, "codename": APP_CODENAME,
            "config": {"ssid": CONFIG["ssid"], "ip": DISPLAY_IP, "port": CONFIG["ftp_port"],
                       "user": CONFIG["user"], "password": CONFIG["password"], "target": str(TARGET),
                       "auto": CONFIG["auto_network"]},
            "ftp": dict(S.ftp, error=render(S.ftp.get("error")),
                        error_key=(S.ftp.get("error") or {}).get("key")),
            "net": S.net, "net_ts": S.net_ts, "net_error": S.net_error,
            "stats": stats, "active": active, "files": render_files(list(S.files)[:120]),
            "events": [{"ts": e["ts"], "level": e["level"], "msg": tr(e["key"], **e["params"])}
                       for e in list(S.events)[-200:][::-1]],
            "rate_hist": list(S.rate_hist)[-3600:],
            "progress": progress(),
            "scan": dict(S.scan, note=render(S.scan.get("note"))) if S.scan else S.scan,
            "scan_ts": S.scan_ts, "scan_error": S.scan_error,
            "power": {"supported": POWER_OK, "awake": S.power["awake"], "lid": S.power["lid"],
                      "awake_eff": S.power["awake_eff"], "lid_eff": S.power["lid_eff"],
                      "left": S.power["left"], "idle_min": CONFIG["awake_idle_min"],
                      "last_activity": S.last_activity,
                      "error": render(S.power["error"]),
                      "battery": battery(),
                      "lid_orig_text": (lambda o: o and tr("msg.lid_orig_text", ac=lid_ref(o["ac"]),
                                                           dc=lid_ref(o["dc"])))(S.power["lid_orig"])},
            "lang": LANG, "lang_setting": CONFIG["language"],
        }
    data["hints"] = render_hints(hints())
    return data


class UI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            self._send(200, load_ui(), "text/html")
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif self.path == "/api/state":
            self._send(200, json.dumps(api_state(), ensure_ascii=False))
        elif self.path == "/api/i18n":
            strings = dict(_strings("en"), **_strings(LANG))
            self._send(200, json.dumps({"lang": LANG, "setting": CONFIG["language"], "strings": strings},
                                       ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        action = body.get("action")
        if action == "power":
            key, val = body.get("key"), bool(body.get("value"))
            if not POWER_OK and not DEMO_MODE:
                self._send(200, json.dumps({"ok": False, "msg": tr("msg.windows_only")}, ensure_ascii=False))
            elif key in ("awake", "lid"):
                S.power[key] = val
                if val:
                    touch()        # switching on restarts the 10-minute window
                S.power_poke.set()
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(400, json.dumps({"ok": False}))
            return
        if action == "expect":
            ok, msg = set_expectation(body)
            self._send(200, json.dumps({"ok": ok, "msg": tr(msg)}, ensure_ascii=False))
            return
        if action == "expect_clear":
            S.expect = None
            event("info", "ev.expect_cleared")
            self._send(200, json.dumps({"ok": True}))
            return
        if action == "expect_ack":
            if S.expect:
                S.expect["ack"] = "ok" if body.get("value") == "ok" else "missing"
                with S.lock:
                    S.expect["ack_items"] = len(S.sess["items"]) + len(S.sess["names"])
                event("info", "ev.expect_ack_ok" if S.expect["ack"] == "ok" else "ev.expect_ack_missing")
            self._send(200, json.dumps({"ok": True}))
            return
        if action == "session_reset":
            with S.lock:
                S.sess = new_session()
            S.expect = None
            event("info", "ev.session_reset")
            self._send(200, json.dumps({"ok": True}))
            return
        if action == "set_language":
            lang = set_language(body.get("value"))
            self._send(200, json.dumps({"ok": True, "lang": lang}))
            return
        if action == "diag":
            path, text = diag_report()
            self._send(200, json.dumps({"ok": True, "path": path, "text": text}, ensure_ascii=False))
        elif action == "restart_hotspot":
            if TEST_MODE:
                self._send(200, json.dumps({"ok": False, "msg": tr("msg.test_mode")}, ensure_ascii=False))
                return
            threading.Thread(target=lambda: (net_prepare(), net_start_hotspot(ref("ev.reason.manual"))),
                             daemon=True).start()
            self._send(200, json.dumps({"ok": True}))
        elif action == "scan_now":
            S.scan_now.set()
            self._send(200, json.dumps({"ok": True}))
        elif action in ("loopback_wizard", "loopback_enable"):
            if TEST_MODE:
                self._send(200, json.dumps({"ok": False, "msg": tr("msg.test_mode")}, ensure_ascii=False))
                return
            if action == "loopback_wizard":
                cmd = "Start-Process hdwwiz.exe -Verb RunAs"
            else:
                name = ((S.net.get("loopback") or {}).get("name") or "Loopback").replace("'", "''")
                inner = f"Enable-NetAdapter -Name '{name}' -Confirm:`$false"
                cmd = f"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-Command',\"{inner}\""
            subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd], creationflags=0x08000000)
            event("info", "ev.loopback_admin")
            self._send(200, json.dumps({"ok": True}))
        elif action == "open_folder":
            if sys.platform == "win32":
                os.startfile(TARGET)
            self._send(200, json.dumps({"ok": True}))
        elif action == "quit":
            self._send(200, json.dumps({"ok": True}))
            S.stop.set()
        else:
            self._send(400, json.dumps({"ok": False, "msg": tr("msg.unknown_action")}, ensure_ascii=False))


def load_ui():
    """ui.html is next to the program or inside the .exe (PyInstaller --add-data)."""
    for d in (RES_DIR, APP_DIR):
        f = d / "ui.html"
        if f.exists():
            return f.read_text(encoding="utf-8")
    return f"<h1>{tr('msg.ui_missing_title')}</h1><p>{tr('msg.ui_missing_body')}</p>"


# ------------------------------------------------------------------ Start/stop
_cleaned = threading.Event()


def cleanup():
    if _cleaned.is_set():
        return
    _cleaned.set()
    S.stop.set()
    if S.power["lid_eff"]:
        lid_engage(False)
    net_cleanup()
    with S.lock:
        if S.done_secs:
            event("info", "ev.finished", files=S.done_files, gb=f"{S.done_bytes / 1e9:.2f}",
                  avg=f"{S.done_bytes / 1e6 / S.done_secs:.1f}", dups=S.dups, aborted=S.aborted)


def install_console_handler():
    if sys.platform != "win32":
        return
    try:
        HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def h(ctrl):
            if ctrl in (2, 5, 6):  # close window, log off, shut down
                cleanup()
                return True
            return False
        install_console_handler._ref = HANDLER(h)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(install_console_handler._ref, True)
    except Exception:
        pass


def demo_feed():
    """Development and screenshots only (NFT_TEST=1 NFT_DEMO=1): made-up sample data."""
    import random
    rnd = random.Random(7)
    now = time.time()
    S.net = {
        "ok": True,
        "hotspot_adapter": {"name": "Wi-Fi", "desc": "Intel(R) Wi-Fi 6E AX211 160MHz", "status": "Up", "wlan": {"name": "Wi-Fi"}},
        "uplink_adapter": {"name": "Wi-Fi 2", "desc": "USB Wireless Adapter", "status": "Up",
                           "wlan": {"name": "Wi-Fi 2", "ssid": "Hotel-Guest", "band": "5 GHz", "channel": 36, "signal": 71}},
        "loopback": {"name": "Loopback", "status": "Not Present"},
        "hotspot_ip_present": True,
        "share_source": {"name": "Hotel-Guest", "kind": "internet", "level": "InternetAccess"},
        "hotspot": {"state": "On", "clients": 1, "ssid": CONFIG["ssid"], "band": "FiveGigahertz", "passphrase": "example-pass",
                    "client_list": [{"mac": "00:11:22:33:44:55", "names": "192.168.137.20"}]},
        "adapters": [
            {"name": "Wi-Fi", "desc": "Intel(R) Wi-Fi 6E AX211 160MHz", "status": "Disconnected", "kind": "wifi", "ipv4": [], "profile": None},
            {"name": "Wi-Fi 2", "desc": "USB Wireless Adapter", "status": "Up", "kind": "wifi", "ipv4": ["10.0.0.23"],
             "profile": {"name": "Hotel-Guest", "v4": "Internet"}},
            {"name": "Local Area Connection* 2", "desc": "Microsoft Wi-Fi Direct Virtual Adapter #2", "status": "Up", "kind": "hotspot",
             "ipv4": ["192.168.137.1"], "profile": None},
            {"name": "Loopback", "desc": "Microsoft KM-TEST Loopback Adapter", "status": "Not Present", "kind": "loopback", "ipv4": [], "profile": None},
        ],
    }
    rows = [("Hotel-Guest", 36, "5 GHz", 71, 12), ("Hotel-Guest", 6, "2,4 GHz", 80, 64), ("Hotel-Guest", 1, "2,4 GHz", 45, 38),
            ("Hotel-Staff", 36, "5 GHz", 66, 12), ("Hotel-Staff", 11, "2,4 GHz", 52, 41), ("DiveShop", 6, "2,4 GHz", 58, 64),
            ("DiveShop-5G", 44, "5 GHz", 49, 3), ("Cafe-WiFi", 11, "2,4 GHz", 40, 41), ("Cafe-WiFi", 149, "5 GHz", 35, 2),
            ("", 6, "2,4 GHz", 31, None), ("Phone-Hotspot", 1, "2,4 GHz", 27, None), ("Resort-IoT", 11, "2,4 GHz", 44, 41),
            ("Resort-IoT", 100, "5 GHz", 38, 1), (CONFIG["ssid"], 149, "5 GHz", 96, None)]
    S.scan = {"iface": "Wi-Fi 2", "fresh": True, "networks": [
        {"ssid": ss, "bssid": f"02:00:00:00:{i:02x}:01", "band": b, "channel": c, "signal": sig, "util": u,
         "stations": rnd.randint(0, 9) if u is not None else None, "radio": "802.11ax"} for i, (ss, c, b, sig, u) in enumerate(rows)]}
    S.scan_ts = now - 12
    # the target folder already had DSC_7000..7039 from the day before
    for n in range(7000, 7040):
        S.nums.setdefault("DSC", {}).setdefault(n, set())
    S.sess["start"] = now - 1300
    t = now - 1300
    num = 7040
    while t < now - 20:
        burst = rnd.uniform(60, 240)
        end = min(now - 20, t + burst)
        lvl = rnd.uniform(12.5, 16.5)
        nxt_file = t + 4.5
        while t < end:
            v = max(0, lvl + rnd.gauss(0, 1.4) - (6 if rnd.random() < .04 else 0))
            S.rate_hist.append((round(t, 1), round(v, 2)))
            if t >= nxt_file:
                st = rnd.choices(["ok", "warn", "aborted"], [96, 2, 2])[0]
                size = rnd.uniform(63.8, 66.4) * 1e6
                mb = 0 if st == "aborted" else rnd.uniform(9.5, 17.5)
                name = f"DSC_{num:04d}.NEF"
                S.files.appendleft({"ts": t, "name": name, "size": size if st != "aborted" else size * .4,
                                    "secs": size / 1e6 / mb if mb else 3.1, "mbps": mb, "status": st,
                                    "note_key": {"warn": "file.demo_truncated",
                                                 "aborted": "file.demo_aborted"}.get(st),
                                    "note_params": {}})
                if st != "aborted":
                    note_received(name, st)
                    S.sess["file_ts"][-1] = t
                    S.sess["item_ts"][-1] = t
                    S.done_files += 1
                    S.done_bytes += size
                    S.done_secs += size / 1e6 / (mb or 10)
                    S.warn_files += st == "warn"
                    num += 1
                else:
                    S.aborted += 1
                    S.events.append({"ts": t, "level": "warning", "key": "ev.demo_aborted", "params": {}})
                nxt_file = t + 64.5e6 / 1e6 / max(lvl, 1)
            t += 1
        t += rnd.uniform(20, 140)
    S.events.append({"ts": now - 1250, "level": "ok", "key": "ev.hotspot_active",
                     "params": {"ssid": CONFIG["ssid"], "source": "Hotel-Guest", "kind": ref("ev.kind.internet")}})
    S.events.append({"ts": now - 640, "level": "warning", "key": "ev.net_change",
                     "params": {"label": ref("ev.label.hs_state"), "old": "On", "new": "InTransition"}})
    S.events = collections.deque(sorted(S.events, key=lambda e: e["ts"]), maxlen=500)
    S.ftp.update(connected=1, last_ip="192.168.137.20", last_connect=now - 300)
    S.started = now - 1500
    set_expectation({"mode": "last", "value": "DSC_7310"})
    S.last_activity = now
    while not S.stop.is_set():
        S.rate_hist.append((round(time.time(), 1), round(max(0, 14 + rnd.gauss(0, 1.5)), 2)))
        S.last_activity = time.time()
        S.stop.wait(1)


def main():
    event("info", "ev.starting_test" if TEST_MODE else "ev.starting",
          app=APP_NAME, version=APP_VERSION, target=str(TARGET))
    stale = [p for p in TARGET.glob("*" + PART)]
    if stale:
        event("warning", "ev.stale_parts", count=len(stale))
    if not TEST_MODE and load_manual_profiles():
        event("warning", "ev.stale_profiles", profiles=", ".join(load_manual_profiles()))
    install_console_handler()
    ui = ThreadingHTTPServer(("127.0.0.1", CONFIG["ui_port"]), UI)
    threading.Thread(target=ui.serve_forever, daemon=True).start()
    lid_restore_stale()
    S.power["awake"] = bool(CONFIG["keep_awake"]) and (POWER_OK or DEMO_MODE)
    S.power["lid"] = bool(CONFIG["lid_keep_running"]) and POWER_OK
    touch()
    threading.Thread(target=keep_awake_loop, daemon=True).start()
    threading.Thread(target=build_index, daemon=True).start()
    threading.Thread(target=demo_feed if DEMO_MODE else sampler, daemon=True).start()
    if not TEST_MODE:
        threading.Thread(target=supervisor, daemon=True).start()
        threading.Thread(target=scanner, daemon=True).start()
    url = f"http://127.0.0.1:{CONFIG['ui_port']}/"
    event("info", "ev.web_ui", url=url)
    if CONFIG["open_browser"]:
        webbrowser.open(url)
    try:
        ftp_loop()
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        ui.shutdown()


def check_folder(folder: str):
    """python nikon_ftp_transfer.py --check <folder>: check existing files, without server/network. English output."""
    files = sorted(p for p in Path(folder).iterdir() if p.is_file())
    bad = 0
    for p in files:
        ok, vkey, vparams = verify(p)
        if ok is None:
            continue
        print(f"{'OK  ' if ok else 'FAIL'}  {p.name:<28} {p.stat().st_size / 1e6:8.1f} MB  {tr(vkey, 'en', **vparams)}")
        bad += 0 if ok else 1
    print(f"\n{len(files)} files, {bad} suspicious")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--check":
        check_folder(sys.argv[2])
    else:
        main()
