"""Torznab (torrents) and Newznab (Usenet) indexers — works with Prowlarr, Jackett, NZBHydra2 or direct indexers."""
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import db
from .quality import from_title
from .util import http, log

NS_T = "{http://torznab.com/schemas/2015/feed}"
NS_N = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"


def _api(ix):
    url = ix["url"].rstrip("/")
    if not url.endswith("/api") and "/api" not in url.split("?")[0][-6:]:
        url += "/api"
    return url


def protocol_of(ix):
    return "torrent" if ix["kind"] == "torznab" else "usenet"


def _call(ix, params):
    p = {"apikey": ix["api_key"], "cat": ix.get("categories") or "3000", "limit": 100, "extended": 1}
    p.update(params)
    r = http().get(_api(ix), params=p, timeout=45)
    r.raise_for_status()
    return parse(r.text, ix)


def parse(xml_text, ix):
    root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    err = root if root.tag == "error" else None
    if err is not None:
        raise RuntimeError(f"Indexer error {err.get('code')}: {err.get('description')}")
    out = []
    proto = protocol_of(ix)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = item.findtext("link") or ""
        enc = item.find("enclosure")
        if enc is not None and enc.get("url"):
            link = enc.get("url")
        size = int(item.findtext("size") or (enc.get("length") if enc is not None else 0) or 0)
        attrs = {}
        for a in list(item.findall(f"{NS_T}attr")) + list(item.findall(f"{NS_N}attr")):
            attrs[a.get("name")] = a.get("value")
        if attrs.get("size"):
            size = int(attrs["size"])
        if attrs.get("magneturl") and not link:
            link = attrs["magneturl"]
        pub = item.findtext("pubDate")
        age = 0.0
        if pub:
            try:
                age = (datetime.now(timezone.utc) - parsedate_to_datetime(pub)).total_seconds() / 86400
            except Exception:
                pass
        out.append({
            "title": title, "guid": item.findtext("guid") or link, "link": link, "size": size,
            "protocol": proto, "indexer": ix["name"], "indexer_id": ix["id"], "indexer_priority": ix.get("priority", 25),
            "seeders": int(attrs["seeders"]) if attrs.get("seeders", "").isdigit() else None,
            "leechers": int(attrs["peers"]) - int(attrs.get("seeders", 0) or 0) if attrs.get("peers", "").isdigit() else None,
            "grabs": int(attrs["grabs"]) if attrs.get("grabs", "").isdigit() else None,
            "age_days": round(age, 1), "info_url": item.findtext("comments") or "",
            "quality": from_title(title), "min_seeders": ix.get("min_seeders", 1),
        })
    return out


def search(ix, artist, album, year=None):
    """Try the structured music search first, then fall back to a free-text query."""
    results, seen = [], set()

    def add(items):
        for r in items:
            if r["guid"] not in seen:
                seen.add(r["guid"])
                results.append(r)

    try:
        add(_call(ix, {"t": "music", "artist": artist, "album": album}))
    except Exception as e:
        log.debug("%s music search failed (%s), using text search", ix["name"], e)
    if len(results) < 3:
        try:
            add(_call(ix, {"t": "search", "q": f"{artist} {album}"}))
        except Exception as e:
            log.warning("Indexer %s search failed: %s", ix["name"], e)
    return results


def rss(ix):
    try:
        return _call(ix, {"t": "search", "q": ""})
    except Exception as e:
        log.warning("RSS sync from %s failed: %s", ix["name"], e)
        return []


def test(ix):
    r = http().get(_api(ix), params={"t": "caps", "apikey": ix["api_key"]}, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    if root.tag == "error":
        raise RuntimeError(root.get("description"))
    cats = [c.get("id") + " " + c.get("name", "") for c in root.iter("category") if c.get("id", "").startswith("3")]
    return {"ok": True, "message": f"Connected. Audio categories: {', '.join(cats[:8]) or 'none reported'}"}


def enabled(kind_filter=None, for_rss=False):
    rows = db.q("SELECT * FROM indexers WHERE enabled=1 ORDER BY priority")
    if for_rss:
        rows = [r for r in rows if r["rss"]]
    else:
        rows = [r for r in rows if r["search"]]
    return rows
