"""Local thumbnail cache for artist and album images (like Sonarr's MediaCover).

The UI asks Musicarr for /api/v1/mediacover/{artist|album}/{id}?size=250. The first request downloads the
image once (or reads the album's own cover.jpg), shrinks it and stores it in /config/MediaCover; after that
it's served straight from disk with long browser caching, so the library page doesn't wait on the
Cover Art Archive."""
import hashlib
import io
import os
import threading
import time

from . import db
from .util import http, log

SIZES = (100, 250, 500)
_fetch_slots = threading.Semaphore(6)
_locks = {}
_locks_guard = threading.Lock()
MISS_TTL = 24 * 3600


def cache_dir(kind):
    d = os.path.join(db.CONFIG_DIR, "MediaCover", kind)
    os.makedirs(d, exist_ok=True)
    return d


def _source(kind, rid):
    """(key, local_path or None, remote_url or None) describing where this image comes from."""
    if kind == "album":
        from .artwork import local_cover
        al = db.q1("SELECT mbid, cover FROM albums WHERE id=?", (rid,))
        if not al:
            return None
        lc = None
        try:
            lc = local_cover(rid)
        except Exception:
            pass
        if lc:
            try:
                return f"file:{lc}:{int(os.path.getmtime(lc))}", lc, None
            except OSError:
                pass
        url = al["cover"] or f"https://coverartarchive.org/release-group/{al['mbid']}/front-500"
        if url.startswith("/"):
            url = f"https://coverartarchive.org/release-group/{al['mbid']}/front-500"
        url = url.replace("/front-1200", "/front-500").replace("/front-250", "/front-500")
        return f"url:{url}", None, url
    a = db.q1("SELECT image FROM artists WHERE id=?", (rid,))
    if not a:
        return None
    url = a["image"]
    if not url:
        first = db.q1("SELECT id FROM albums WHERE artist_id=? AND album_type='Album' ORDER BY release_date", (rid,)) \
            or db.q1("SELECT id FROM albums WHERE artist_id=? ORDER BY release_date", (rid,))
        return _source("album", first["id"]) if first else None
    if "fanart.tv" in url and "/fanart/" in url:
        url = url.replace("/fanart/", "/preview/")  # fanart.tv's small preview instead of the full photo
    return f"url:{url}", None, url


def _lock(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def get(kind, rid, size):
    """Return a path to a cached JPEG, or None if there is no image."""
    size = min(SIZES, key=lambda s: abs(s - size))
    src = _source(kind, rid)
    if not src:
        return None
    key, local, url = src
    h = hashlib.sha1(key.encode()).hexdigest()[:10]
    d = cache_dir(kind)
    path = os.path.join(d, f"{rid}-{size}-{h}.jpg")
    miss = os.path.join(d, f"{rid}-{h}.miss")
    if os.path.exists(path):
        return path
    if os.path.exists(miss) and time.time() - os.path.getmtime(miss) < MISS_TTL:
        return None
    with _lock(path):
        if os.path.exists(path):
            return path
        try:
            if local:
                with open(local, "rb") as fh:
                    data = fh.read()
            else:
                with _fetch_slots:
                    r = http().get(url, timeout=25)
                if r.status_code == 404:
                    open(miss, "w").close()
                    return None
                r.raise_for_status()
                data = r.content
            out = _shrink(data, size)
        except Exception as e:
            log.debug("Image cache %s/%s failed: %s", kind, rid, e)
            open(miss, "w").close()
            return None
        # drop older versions of this image
        for n in os.listdir(d):
            if n.startswith(f"{rid}-") and n != os.path.basename(path) and (n.startswith(f"{rid}-{size}-") or n.endswith(".miss")):
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, path)
        return path


def _shrink(data, size):
    try:
        from PIL import Image
    except Exception:
        return data
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (size, size))  # fast JPEG downscale while decoding
    im = im.convert("RGB")
    im.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
    return buf.getvalue()


def invalidate(kind, rid):
    d = cache_dir(kind)
    for n in os.listdir(d):
        if n.startswith(f"{rid}-"):
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
