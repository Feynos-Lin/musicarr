"""Decision engine (accept / reject / rank releases), automatic + interactive search, and grabbing."""
import re
import threading

from . import db, indexers, soulseek, notify
from .downloaders import KINDS, impl
from .quality import rank, meets_cutoff
from .util import contains_all, log, norm

_grab_lock = threading.Lock()


def profile_for(artist):
    p = db.decode(db.q1("SELECT * FROM quality_profiles WHERE id=?", (artist.get("quality_profile_id"),)))
    return p or db.decode(db.q1("SELECT * FROM quality_profiles ORDER BY id LIMIT 1"))


def current_quality(album_id):
    rows = db.q("SELECT quality FROM tracks WHERE album_id=? AND file_path IS NOT NULL", (album_id,))
    if not rows:
        return None
    return max((r["quality"] or "Unknown" for r in rows), key=rank)


def _words(s):
    return [w.strip().lower() for w in (s or "").split(",") if w.strip()]


def evaluate(rel, album, artist, profile, tracks):
    rej = []
    title = rel["title"]
    nt = norm(title)
    if rel["protocol"] != "soulseek":
        va = artist["name"].lower() in ("various artists", "va")
        if not va and not contains_all(title, artist["name"], 0.75):
            rej.append("Artist name not found in release title")
        if not contains_all(title, album["title"], 0.75):
            rej.append("Album title not found in release title")
        if re.search(r"\bdiscography\b|\bcollection\b|\bcomplete works\b|\banthology\b", nt) and \
                not re.search(r"discography|collection|anthology", norm(album["title"])):
            rej.append("Looks like a discography/collection")
        if rel["protocol"] == "torrent" and rel.get("seeders") is not None:
            need = max(profile.get("min_seeders") or 0, rel.get("min_seeders") or 0)
            if rel["seeders"] < need:
                rej.append(f"Not enough seeders ({rel['seeders']} < {need})")
        n = max(len(tracks), 1)
        if rel["size"] and rel["size"] < n * 700_000 and rel["quality"] != "Unknown":
            rej.append("Too small for the track count")
    else:
        if rel.get("track_match") is not None and rel["track_match"] < int(db.get_setting("min_track_match_pct") or 80) - 30:
            rej.append(f"Only {rel['track_match']}% of tracks found in folder")
    if rel["quality"] not in profile["allowed"]:
        rej.append(f"Quality {rel['quality']} not wanted by profile '{profile['name']}'")
    for w in _words(profile.get("ignored_words")):
        if w in title.lower():
            rej.append(f"Contains ignored term '{w}'")
    req = _words(profile.get("required_words"))
    if req and not any(w in title.lower() for w in req):
        rej.append("Missing a required term")
    if db.q1("SELECT 1 FROM blocklist WHERE album_id=? AND source_title=?", (album["id"], rel["title"])):
        rej.append("Release is blocklisted")
    cur = current_quality(album["id"])
    if cur:
        if not profile.get("upgrade_allowed"):
            rej.append("Album already has files and upgrades are disabled")
        elif meets_cutoff(cur, profile):
            rej.append(f"Existing {cur} already meets cutoff ({profile['cutoff']})")
        elif rank(rel["quality"]) >= rank(cur):
            rej.append(f"Not an upgrade over existing {cur}")
    if db.q1("SELECT 1 FROM queue WHERE album_id=? AND status NOT IN ('failed','imported')", (album["id"],)):
        rej.append("Album is already in the download queue")
    rel["rejections"] = rej
    rel["approved"] = not rej
    pref = sum(1 for w in _words(profile.get("preferred_words")) if w in title.lower())
    order = profile.get("protocol_order") or ["usenet", "torrent", "soulseek"]
    proto_rank = order.index(rel["protocol"]) if rel["protocol"] in order else 9
    rel["preferred_score"] = pref
    rel["_sort"] = (rank(rel["quality"]), -pref, proto_rank, rel.get("indexer_priority", 25),
                    -(rel.get("seeders") or 0), -(rel.get("track_match") or 0), rel.get("age_days") or 0)
    return rel


def gather(album, artist, tracks, include_soulseek=True):
    results = []
    for ix in indexers.enabled():
        try:
            results.extend(indexers.search(ix, artist["name"], album["title"], (album.get("release_date") or "")[:4]))
        except Exception as e:
            log.warning("Search on %s failed: %s", ix["name"], e)
    if include_soulseek and soulseek.client():
        try:
            results.extend(soulseek.search(album, artist, tracks))
        except Exception as e:
            log.warning("Soulseek search failed: %s", e)
    return results


def load(album_id):
    album = db.decode(db.q1("SELECT * FROM albums WHERE id=?", (album_id,)))
    artist = db.decode(db.q1("SELECT * FROM artists WHERE id=?", (album["artist_id"],)))
    tracks = db.q("SELECT * FROM tracks WHERE album_id=? ORDER BY disc, position", (album_id,))
    return album, artist, tracks


def interactive(album_id):
    album, artist, tracks = load(album_id)
    profile = profile_for(artist)
    out = [evaluate(r, album, artist, profile, tracks) for r in gather(album, artist, tracks)]
    out.sort(key=lambda r: (not r["approved"], r["_sort"]))
    for r in out:
        r.pop("_sort", None)
    return out


def auto(album_id):
    """Search all sources and grab the best approved release. Returns the grabbed title or None."""
    album, artist, tracks = load(album_id)
    db.update("albums", album_id, {"last_search": db.now()})
    if not album["monitored"] or not artist["monitored"]:
        return None
    profile = profile_for(artist)
    # Torrent/Usenet first (fast); Soulseek searches take ~30s so only run them if needed
    rels = [evaluate(r, album, artist, profile, tracks) for r in gather(album, artist, tracks, include_soulseek=False)]
    good = sorted([r for r in rels if r["approved"]], key=lambda r: r["_sort"])
    if (not good or not meets_cutoff(good[0]["quality"], profile)) and soulseek.client():
        try:
            for r in soulseek.search(album, artist, tracks):
                evaluate(r, album, artist, profile, tracks)
                if r["approved"]:
                    good.append(r)
            good.sort(key=lambda r: r["_sort"])
        except Exception as e:
            log.warning("Soulseek search failed: %s", e)
    for rel in good[:3]:
        try:
            grab(album_id, rel)
            return rel["title"]
        except Exception as e:
            log.warning("Grab of %s failed, trying next: %s", rel["title"], e)
    log.info("No suitable release found for %s - %s", artist["name"], album["title"])
    return None


def pick_client(protocol):
    rows = db.q("SELECT * FROM download_clients WHERE enabled=1 ORDER BY priority, id")
    for c in rows:
        if KINDS.get(c["kind"]) == protocol:
            return db.decode(c)
    return None


def grab(album_id, rel):
    with _grab_lock:
        c = pick_client(rel["protocol"])
        if not c:
            raise RuntimeError(f"No enabled {rel['protocol']} download client")
        album, artist, _ = load(album_id)
        qid = db.insert("queue", dict(album_id=album_id, client_id=c["id"], title=rel["title"], protocol=rel["protocol"],
                                      indexer=rel.get("indexer"), quality=rel["quality"], size=rel.get("size") or 0,
                                      status="sending", added=db.now(), updated=db.now(),
                                      payload={k: rel.get(k) for k in ("guid", "link", "info_url", "username", "folder", "seeders")}))
        try:
            did = impl(c).grab(c, rel, f"musicarr-{qid}")
        except Exception as e:
            db.ex("DELETE FROM queue WHERE id=?", (qid,))
            raise
        db.update("queue", qid, {"download_id": did, "status": "queued"})
        db.add_history("grabbed", album_id, artist["id"], rel["title"], rel["quality"], rel["protocol"],
                       {"indexer": rel.get("indexer"), "size": rel.get("size"), "client": c["name"], "guid": rel.get("guid")})
        log.info("Grabbed '%s' [%s] via %s for %s - %s", rel["title"], rel["quality"], c["name"], artist["name"], album["title"])
        notify.send("grab", f"Grabbed: {artist['name']} – {album['title']}",
                    f"{rel['title']}\n{rel['quality']} · {rel['protocol']} · {rel.get('indexer') or ''}", album=album, artist=artist)
        return qid


def wanted_missing(limit=None):
    sql = """SELECT al.* FROM albums al JOIN artists ar ON ar.id=al.artist_id
             WHERE al.monitored=1 AND ar.monitored=1 AND (al.release_date IS NULL OR al.release_date<=date('now'))
             AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.album_id=al.id AND t.file_path IS NOT NULL)
             ORDER BY al.last_search IS NOT NULL, al.last_search, al.release_date DESC"""
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.q(sql)


def wanted_cutoff():
    out = []
    for al in db.q("""SELECT al.*, ar.quality_profile_id FROM albums al JOIN artists ar ON ar.id=al.artist_id
                      WHERE al.monitored=1 AND ar.monitored=1
                      AND EXISTS (SELECT 1 FROM tracks t WHERE t.album_id=al.id AND t.file_path IS NOT NULL)"""):
        p = profile_for(al)
        cur = current_quality(al["id"])
        if p and p.get("upgrade_allowed") and cur and not meets_cutoff(cur, p):
            al["current_quality"] = cur
            al["cutoff"] = p["cutoff"]
            out.append(al)
    return out


def rss_sync():
    """Pull latest releases from every RSS-enabled indexer and grab anything matching a wanted album."""
    wanted = {}
    for al in wanted_missing() + wanted_cutoff():
        ar = db.q1("SELECT name FROM artists WHERE id=?", (al["artist_id"],))
        wanted[al["id"]] = (al, ar["name"])
    if not wanted:
        return 0
    grabbed = 0
    for ix in indexers.enabled(for_rss=True):
        for rel in indexers.rss(ix):
            for aid, (al, artist_name) in list(wanted.items()):
                if contains_all(rel["title"], al["title"], 0.9) and contains_all(rel["title"], artist_name, 0.75):
                    album, artist, tracks = load(aid)
                    evaluate(rel, album, artist, profile_for(artist), tracks)
                    if rel["approved"]:
                        try:
                            grab(aid, rel)
                            grabbed += 1
                            wanted.pop(aid, None)
                        except Exception as e:
                            log.warning("RSS grab failed: %s", e)
    return grabbed
