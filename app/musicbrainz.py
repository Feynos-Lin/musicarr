"""MusicBrainz / Cover Art Archive / optional Last.fm + fanart.tv metadata, rate limited to MB's 1 req/sec."""
import threading
import time

from . import db
from .util import http, log

MB = "https://musicbrainz.org/ws/2"
_mb_lock = threading.Lock()
_last = [0.0]
_cache = {}
CACHE_TTL = 3600


def _get(path, params=None, cache=True):
    params = dict(params or {})
    params["fmt"] = "json"
    key = (path, tuple(sorted(params.items())))
    if cache and key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    for attempt in range(4):
        with _mb_lock:
            wait = 1.05 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
            try:
                r = http().get(f"{MB}/{path}", params=params, timeout=30)
            except Exception as e:
                log.warning("MusicBrainz request failed: %s", e)
                r = None
        if r is not None and r.status_code == 200:
            data = r.json()
            _cache[key] = (time.time(), data)
            return data
        if r is not None and r.status_code == 404:
            return None
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"MusicBrainz unavailable for {path}")


def cover_url(release_group_mbid, size=500):
    return f"https://coverartarchive.org/release-group/{release_group_mbid}/front-{size}"


def search_artists(term, limit=20):
    if len(term) == 36 and term.count("-") == 4:
        a = _get(f"artist/{term}", {"inc": "tags"})
        items = [a] if a else []
    else:
        d = _get("artist", {"query": term, "limit": limit})
        items = (d or {}).get("artists", [])
    out = []
    for a in items:
        out.append({
            "mbid": a["id"], "name": a.get("name"), "sort_name": a.get("sort-name"),
            "disambiguation": a.get("disambiguation", ""), "type": a.get("type", ""),
            "country": a.get("country", ""), "score": a.get("score", 100),
            "begin": (a.get("life-span") or {}).get("begin", ""),
            "tags": [t["name"] for t in sorted(a.get("tags", []), key=lambda t: -t.get("count", 0))[:5]],
            "existing": bool(db.q1("SELECT 1 FROM artists WHERE mbid=?", (a["id"],))),
        })
    return out


def search_albums(term, limit=20):
    d = _get("release-group", {"query": term, "limit": limit})
    out = []
    for rg in (d or {}).get("release-groups", []):
        credit = rg.get("artist-credit") or [{}]
        art = (credit[0].get("artist") or {})
        out.append({
            "mbid": rg["id"], "title": rg.get("title"), "type": rg.get("primary-type", ""),
            "secondary_types": rg.get("secondary-types", []), "date": rg.get("first-release-date", ""),
            "artist_mbid": art.get("id"), "artist_name": art.get("name"), "cover": cover_url(rg["id"], 250),
        })
    return out


def artist_info(mbid):
    a = _get(f"artist/{mbid}", {"inc": "tags+genres+url-rels"})
    if not a:
        raise RuntimeError("Artist not found on MusicBrainz")
    links = []
    image = None
    for rel in a.get("relations", []):
        url = (rel.get("url") or {}).get("resource")
        if url:
            links.append({"type": rel.get("type"), "url": url})
    genres = [g["name"] for g in sorted(a.get("genres", []) or a.get("tags", []), key=lambda g: -g.get("count", 0))[:8]]
    overview = ""
    lfm = db.get_setting("lastfm_api_key")
    if lfm:
        try:
            r = http().get("https://ws.audioscrobbler.com/2.0/", params={
                "method": "artist.getinfo", "mbid": mbid, "api_key": lfm, "format": "json"}, timeout=15).json()
            overview = ((r.get("artist") or {}).get("bio") or {}).get("summary", "")
            overview = overview.split("<a href")[0].strip()
        except Exception as e:
            log.debug("Last.fm bio failed: %s", e)
    fan = db.get_setting("fanart_api_key")
    if fan:
        try:
            r = http().get(f"https://webservice.fanart.tv/v3/music/{mbid}", params={"api_key": fan}, timeout=15)
            if r.ok:
                j = r.json()
                thumbs = j.get("artistthumb") or j.get("artistbackground") or []
                if thumbs:
                    image = thumbs[0]["url"]
        except Exception as e:
            log.debug("fanart.tv failed: %s", e)
    return {
        "mbid": a["id"], "name": a.get("name"), "sort_name": a.get("sort-name"),
        "disambiguation": a.get("disambiguation", ""), "artist_type": a.get("type", ""),
        "country": a.get("country", ""), "genres": genres, "links": links, "overview": overview, "image": image,
    }


def artist_release_groups(mbid):
    out, offset = [], 0
    while True:
        d = _get("release-group", {"artist": mbid, "limit": 100, "offset": offset, "inc": "tags"})
        if not d:
            break
        groups = d.get("release-groups", [])
        out.extend(groups)
        offset += len(groups)
        if not groups or offset >= d.get("release-group-count", 0):
            break
    return out


def release_group_statuses(rg_mbid):
    d = _get("release", {"release-group": rg_mbid, "limit": 100})
    return {r.get("status") for r in (d or {}).get("releases", []) if r.get("status")}


def pick_release(rg_mbid, preferred_release=None):
    """Choose the most canonical release in a group and return it with its tracklist."""
    d = _get("release", {"release-group": rg_mbid, "inc": "recordings+media+artist-credits", "limit": 100})
    rels = (d or {}).get("releases", [])
    if not rels:
        return None
    if preferred_release:
        for r in rels:
            if r["id"] == preferred_release:
                return r

    counts = sorted(sum(m.get("track-count", 0) for m in r.get("media", [])) for r in rels)
    median = counts[len(counts) // 2]

    def score(r):
        s = 0
        if r.get("status") == "Official":
            s += 100
        fmts = [m.get("format") or "" for m in r.get("media", [])]
        if any(f in ("Digital Media",) for f in fmts):
            s += 30
        elif any("CD" in f for f in fmts):
            s += 25
        elif any("Vinyl" in f for f in fmts):
            s -= 10
        tc = sum(m.get("track-count", 0) for m in r.get("media", []))
        s -= abs(tc - median) * 5
        if r.get("country") in ("XW", "US", "GB", "XE"):
            s += 5
        if (r.get("disambiguation") or "").strip():
            s -= 8
        date = r.get("date") or "9999"
        s -= int(date[:4]) / 1000 if date[:4].isdigit() else 3
        return s

    return max(rels, key=score)


def release_tracks(release):
    tracks = []
    for m in release.get("media", []):
        disc = m.get("position", 1)
        for t in m.get("tracks", []):
            rec = t.get("recording") or {}
            credit = "".join(c.get("name", "") + c.get("joinphrase", "") for c in (t.get("artist-credit") or rec.get("artist-credit") or []))
            tracks.append({
                "mbid": t.get("id"), "recording_mbid": rec.get("id"), "title": t.get("title") or rec.get("title"),
                "disc": disc, "position": t.get("position") or int(t.get("number", "0") or 0),
                "length_ms": t.get("length") or rec.get("length") or 0, "artist_credit": credit,
            })
    return tracks


def album_releases(rg_mbid):
    d = _get("release", {"release-group": rg_mbid, "inc": "media", "limit": 100})
    out = []
    for r in (d or {}).get("releases", []):
        out.append({
            "mbid": r["id"], "title": r.get("title"), "date": r.get("date", ""), "country": r.get("country", ""),
            "status": r.get("status", ""), "disambiguation": r.get("disambiguation", ""),
            "format": " + ".join(f"{m.get('format') or '?'}" for m in r.get("media", [])),
            "tracks": sum(m.get("track-count", 0) for m in r.get("media", [])),
        })
    return out
