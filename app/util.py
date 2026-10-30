"""Shared helpers: logging ring buffer, HTTP session, text normalisation, path mapping."""
import collections
import logging
import os
import re
import threading
import time
import unicodedata

import requests

VERSION = "1.0.0"
USER_AGENT = f"Musicarr/{VERSION} ( https://github.com/Feynos-Lin/musicarr )"

# ---------- logging ----------
LOG_BUFFER = collections.deque(maxlen=2000)


class RingHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
            "level": record.levelname, "logger": record.name, "message": record.getMessage(),
        })


def setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(os.environ.get("MUSICARR_LOG_LEVEL", "INFO"))
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(RingHandler())
    cfg = os.environ.get("MUSICARR_CONFIG", "/config")
    try:
        os.makedirs(os.path.join(cfg, "logs"), exist_ok=True)
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(os.path.join(cfg, "logs", "musicarr.log"), maxBytes=2_000_000, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    logging.getLogger("urllib3").setLevel(logging.WARNING)


log = logging.getLogger("musicarr")

# ---------- HTTP ----------
_session_local = threading.local()


def http():
    s = getattr(_session_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        _session_local.s = s
    return s


# ---------- text ----------
_STOP = {"the", "a", "an", "and", "&", "feat", "ft", "featuring", "vs"}


def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def tokens(s):
    return [t for t in norm(s).split() if t not in _STOP]


def similarity(a, b):
    """Token-set similarity 0..1, tolerant of word order and punctuation."""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 1.0 if norm(a) == norm(b) else 0.0
    inter = len(ta & tb)
    return inter / max(len(ta), len(tb))


def contains_all(haystack, needle, threshold=1.0):
    """Fraction of needle's tokens present in haystack >= threshold."""
    tn = tokens(needle)
    if not tn:
        return True
    th = set(tokens(haystack))
    hit = sum(1 for t in tn if t in th)
    return hit / len(tn) >= threshold


ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_name(s, replace=True):
    s = s or "Unknown"
    if replace:
        s = s.replace(":", " -").replace("/", "+").replace("\\", "+").replace("?", "").replace("*", "-")
        s = s.replace('"', "'").replace("<", "(").replace(">", ")").replace("|", "-")
    s = ILLEGAL.sub("", s)
    s = s.strip().rstrip(".")
    return s[:180] or "Unknown"


AUDIO_EXT = {".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".oga", ".opus", ".wav", ".ape", ".wv", ".aiff", ".aif", ".wma", ".dsf"}


def is_audio(path):
    return os.path.splitext(path)[1].lower() in AUDIO_EXT


def fmt_size(n):
    n = n or 0
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def map_path(path, client_id=None):
    """Apply remote path mappings (client's view -> Musicarr's view)."""
    from . import db
    if not path:
        return path
    rows = db.q("SELECT * FROM remote_path_mappings WHERE client_id IS NULL OR client_id=? ORDER BY length(remote_path) DESC",
                (client_id,))
    p = path.replace("\\", "/")
    for r in rows:
        rp = r["remote_path"].replace("\\", "/").rstrip("/")
        if p == rp or p.startswith(rp + "/"):
            return r["local_path"].rstrip("/") + p[len(rp):]
    return path
