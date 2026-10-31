"""Artist/album library management: adding artists, refreshing metadata from MusicBrainz, monitoring rules."""
from datetime import date

from . import db, musicbrainz as mb
from .util import log, http

MONITOR_OPTIONS = ["all", "future", "missing", "existing", "latest", "first", "none"]


def metadata_profile(pid):
    p = db.decode(db.q1("SELECT * FROM metadata_profiles WHERE id=?", (pid,)))
    return p or db.decode(db.q1("SELECT * FROM metadata_profiles ORDER BY id LIMIT 1"))


def _status_groups(mbid, statuses):
    """Release-group ids that have at least one release with an allowed status."""
    ids = set()
    for st in statuses:
        offset = 0
        while True:
            d = mb._get("release", {"artist": mbid, "status": st.lower(), "inc": "release-groups", "limit": 100, "offset": offset})
            if not d:
                break
            rels = d.get("releases", [])
            for r in rels:
                rg = r.get("release-group") or {}
                if rg.get("id"):
                    ids.add(rg["id"])
            offset += len(rels)
            if not rels or offset >= d.get("release-count", 0) or offset >= 2000:
                break
    return ids


def _eligible(rg, prof, status_ids):
    if rg.get("primary-type") not in prof["primary_types"]:
        return False
    sec = rg.get("secondary-types") or []
    if any(s not in prof["secondary_types"] for s in sec):
        return False
    if status_ids is not None and rg["id"] not in status_ids:
        return False
    return True


def add_artist(mbid, quality_profile_id=None, metadata_profile_id=None, root_folder=None, monitor="all",
               monitor_new="all", search=False, tags=None, path=None):
    if db.q1("SELECT 1 FROM artists WHERE mbid=?", (mbid,)):
        raise ValueError("Artist already in library")
    if db.q1("SELECT 1 FROM import_exclusions WHERE mbid=?", (mbid,)) and not path:
        log.info("Adding excluded artist %s anyway (manual add)", mbid)
    info = mb.artist_info(mbid)
    if not root_folder:
        rf = db.q1("SELECT path FROM root_folders ORDER BY id")
        root_folder = rf["path"] if rf else "/music"
    import sqlite3
    try:
        aid = db.insert("artists", dict(
        mbid=mbid, name=info["name"], sort_name=info["sort_name"], disambiguation=info["disambiguation"],
        artist_type=info["artist_type"], country=info["country"], overview=info["overview"], genres=info["genres"],
        image=info["image"], links=info["links"], monitored=1, monitor_new=monitor_new,
        quality_profile_id=quality_profile_id or db.q1("SELECT id FROM quality_profiles ORDER BY id")["id"],
        metadata_profile_id=metadata_profile_id or db.q1("SELECT id FROM metadata_profiles ORDER BY id")["id"],
        root_folder=root_folder, path=path, tags=tags or [], added=db.now()))
    except sqlite3.IntegrityError:
        raise ValueError("Artist already in library")
    db.add_history("artistAdded", artist_id=aid, source_title=info["name"])
    log.info("Added artist %s", info["name"])
    return aid


def refresh_artist(artist_id, initial_monitor=None):
    artist = db.decode(db.q1("SELECT * FROM artists WHERE id=?", (artist_id,)))
    if not artist:
        return
    try:
        info = mb.artist_info(artist["mbid"])
        upd = {k: info[k] for k in ("name", "sort_name", "disambiguation", "artist_type", "country", "genres", "links")}
        if info["overview"]:
            upd["overview"] = info["overview"]
        if info["image"]:
            upd["image"] = info["image"]
        db.update("artists", artist_id, upd)
    except Exception as e:
        log.warning("Artist info refresh failed for %s: %s", artist["name"], e)
    prof = metadata_profile(artist["metadata_profile_id"])
    statuses = prof.get("release_statuses") or ["Official"]
    status_ids = _status_groups(artist["mbid"], statuses) if set(statuses) != {"Official", "Promotion", "Bootleg", "Pseudo-Release"} else None
    groups = [g for g in mb.artist_release_groups(artist["mbid"]) if _eligible(g, prof, status_ids)]
    existing = {a["mbid"]: a for a in db.q("SELECT * FROM albums WHERE artist_id=?", (artist_id,))}
    today = date.today().isoformat()
    new_ids = []
    for g in groups:
        rd = g.get("first-release-date") or None
        genres = [t["name"] for t in sorted(g.get("tags", []), key=lambda t: -t.get("count", 0))[:5]]
        if g["id"] in existing:
            db.update("albums", existing[g["id"]]["id"], {"title": g["title"], "release_date": rd,
                                                          "album_type": g.get("primary-type"),
                                                          "secondary_types": g.get("secondary-types") or [], "genres": genres})
            continue
        if initial_monitor is not None:
            mon = initial_monitor in ("all", "missing", "existing") or (initial_monitor == "future" and (not rd or rd > today))
        else:
            mon = artist["monitor_new"] == "all"
        aid = db.insert("albums", dict(mbid=g["id"], artist_id=artist_id, title=g["title"], album_type=g.get("primary-type"),
                                       secondary_types=g.get("secondary-types") or [], release_date=rd, monitored=int(mon),
                                       cover=mb.cover_url(g["id"]), added=db.now(), genres=genres))
        new_ids.append(aid)
    # albums removed from MusicBrainz / filtered out by profile and without files
    keep = {g["id"] for g in groups}
    for mbid_, a in existing.items():
        if mbid_ not in keep and not db.q1("SELECT 1 FROM tracks WHERE album_id=? AND file_path IS NOT NULL", (a["id"],)):
            db.ex("DELETE FROM albums WHERE id=?", (a["id"],))
    # tracklists
    for a in db.q("SELECT * FROM albums WHERE artist_id=?", (artist_id,)):
        have = db.q1("SELECT COUNT(*) c FROM tracks WHERE album_id=?", (a["id"],))["c"]
        if have and a["id"] not in new_ids and artist.get("last_refresh"):
            continue
        refresh_tracks(a)
    if initial_monitor in ("latest", "first"):
        rows = db.q("SELECT id, release_date FROM albums WHERE artist_id=? AND album_type='Album' AND release_date<=? "
                    "ORDER BY release_date " + ("DESC" if initial_monitor == "latest" else "ASC") + " LIMIT 1", (artist_id, today))
        if rows:
            db.update("albums", rows[0]["id"], {"monitored": 1})
    db.update("artists", artist_id, {"last_refresh": db.now()})
    if not artist.get("image"):
        first = db.q1("SELECT mbid FROM albums WHERE artist_id=? AND album_type='Album' ORDER BY release_date", (artist_id,))
        if first:
            db.update("artists", artist_id, {"image": mb.cover_url(first["mbid"], 500)})
    log.info("Refreshed %s: %d albums (%d new)", artist["name"], len(groups), len(new_ids))
    return new_ids


def refresh_tracks(album, release_mbid=None):
    rel = mb.pick_release(album["mbid"], release_mbid or album.get("release_mbid"))
    if not rel:
        return
    tracks = mb.release_tracks(rel)
    old = db.q("SELECT * FROM tracks WHERE album_id=?", (album["id"],))
    by_pos = {(t["disc"], t["position"]): t for t in old}
    by_rec = {t["recording_mbid"]: t for t in old if t["recording_mbid"]}
    kept = set()
    for t in tracks:
        prev = by_rec.get(t["recording_mbid"]) or by_pos.get((t["disc"], t["position"]))
        if prev and prev["id"] not in kept:
            db.update("tracks", prev["id"], t)
            kept.add(prev["id"])
        else:
            db.insert("tracks", dict(album_id=album["id"], **t))
    for t in old:
        if t["id"] not in kept and not t["file_path"]:
            db.ex("DELETE FROM tracks WHERE id=?", (t["id"],))
    db.update("albums", album["id"], {"release_mbid": rel["id"], "track_count": len(tracks)})


def apply_monitor_existing(artist_id, option):
    """After a disk scan, apply 'missing'/'existing' monitoring choices."""
    if option == "existing":
        db.ex("""UPDATE albums SET monitored = EXISTS(SELECT 1 FROM tracks t WHERE t.album_id=albums.id AND t.file_path IS NOT NULL)
                 WHERE artist_id=?""", (artist_id,))
    elif option == "none":
        db.ex("UPDATE albums SET monitored=0 WHERE artist_id=?", (artist_id,))


def delete_artist(artist_id, delete_files=False, exclude=False):
    import shutil
    a = db.q1("SELECT * FROM artists WHERE id=?", (artist_id,))
    if not a:
        return
    if delete_files and a.get("path"):
        shutil.rmtree(a["path"], ignore_errors=True)
    if exclude:
        db.ex("INSERT OR IGNORE INTO import_exclusions(mbid,name) VALUES(?,?)", (a["mbid"], a["name"]))
    db.ex("DELETE FROM tracks WHERE album_id IN (SELECT id FROM albums WHERE artist_id=?)", (artist_id,))
    db.ex("DELETE FROM albums WHERE artist_id=?", (artist_id,))
    db.ex("DELETE FROM artists WHERE id=?", (artist_id,))
    log.info("Deleted artist %s", a["name"])


# ---------------- Import lists ----------------
def import_list_artists(il):
    cfg = il["config"] if isinstance(il["config"], dict) else {}
    k = il["kind"]
    s = http()
    out = []
    if k == "listenbrainz_top":
        r = s.get(f"https://api.listenbrainz.org/1/stats/user/{cfg['username']}/artists",
                  params={"range": cfg.get("range", "all_time"), "count": int(cfg.get("count", 25))}, timeout=30)
        if r.status_code == 204:
            return []
        r.raise_for_status()
        for a in r.json()["payload"]["artists"]:
            for m in a.get("artist_mbids") or ([a["artist_mbid"]] if a.get("artist_mbid") else []):
                out.append((m, a.get("artist_name")))
                break
    elif k == "listenbrainz_similar":
        # artists similar to the user's top artists via LB radio/explore
        r = s.get(f"https://api.listenbrainz.org/1/stats/user/{cfg['username']}/artists",
                  params={"range": "year", "count": 5}, timeout=30)
        seeds = [a["artist_mbids"][0] for a in (r.json()["payload"]["artists"] if r.ok and r.status_code != 204 else []) if a.get("artist_mbids")]
        if seeds:
            r2 = s.post("https://labs.api.listenbrainz.org/similar-artists/json",
                        json=[{"artist_mbids": [m], "algorithm": "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"} for m in seeds], timeout=30)
            if r2.ok:
                for row in r2.json()[: int(cfg.get("count", 25))]:
                    if row.get("artist_mbid"):
                        out.append((row["artist_mbid"], row.get("name")))
    elif k == "lastfm_user":
        key = db.get_setting("lastfm_api_key")
        r = s.get("https://ws.audioscrobbler.com/2.0/", params={"method": "user.gettopartists", "user": cfg["username"],
                                                                 "api_key": key, "format": "json", "period": cfg.get("period", "overall"),
                                                                 "limit": int(cfg.get("count", 25))}, timeout=30)
        r.raise_for_status()
        for a in r.json()["topartists"]["artist"]:
            if a.get("mbid"):
                out.append((a["mbid"], a["name"]))
    elif k == "musicbrainz_collection":
        offset = 0
        while True:
            d = mb._get("artist", {"collection": cfg["collection_id"], "limit": 100, "offset": offset})
            arts = (d or {}).get("artists", [])
            out.extend((a["id"], a["name"]) for a in arts)
            offset += len(arts)
            if not arts or offset >= (d or {}).get("artist-count", 0):
                break
    elif k == "mbid_list":
        for line in (cfg.get("mbids") or "").splitlines():
            line = line.strip()
            if len(line) == 36:
                out.append((line, None))
    return out
