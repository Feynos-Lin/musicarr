"""REST API (v1). Everything the web UI does goes through here, so it's scriptable too (X-Api-Key header)."""
import asyncio
import os
import shutil
import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import artwork, db, downloaders, importer, indexers, library, musicbrainz as mb, notify, search, slskd_admin, soulseek, tasks
from .db import QUALITIES
from .util import LOG_BUFFER, VERSION, fmt_size, log

STARTED = time.time()


def J(data, status=200):
    return JSONResponse(data, status_code=status)


async def body(req):
    try:
        return await req.json()
    except Exception:
        return {}


def off(fn, *a, **k):
    return asyncio.to_thread(fn, *a, **k)


def err(fn):
    async def wrap(req):
        try:
            return await fn(req)
        except (ValueError, RuntimeError, KeyError) as e:
            return J({"error": str(e)}, 400)
    wrap.__name__ = fn.__name__
    return wrap


# ---------------- stats helpers ----------------
def album_stats(album_ids=None):
    rows = db.q("""SELECT album_id, COUNT(*) total, SUM(file_path IS NOT NULL) have, SUM(COALESCE(file_size,0)) size,
                   GROUP_CONCAT(DISTINCT quality) quals FROM tracks GROUP BY album_id""")
    return {r["album_id"]: r for r in rows}


def artist_rows():
    arts = [db.decode(a) for a in db.q("SELECT * FROM artists ORDER BY sort_name COLLATE NOCASE")]
    st = db.q("""SELECT al.artist_id, COUNT(DISTINCT al.id) albums, SUM(al.monitored) monitored_albums,
                 (SELECT COUNT(*) FROM tracks t JOIN albums a2 ON a2.id=t.album_id WHERE a2.artist_id=al.artist_id AND a2.monitored=1) tracks,
                 (SELECT COUNT(*) FROM tracks t JOIN albums a2 ON a2.id=t.album_id WHERE a2.artist_id=al.artist_id AND t.file_path IS NOT NULL) have,
                 (SELECT COALESCE(SUM(t.file_size),0) FROM tracks t JOIN albums a2 ON a2.id=t.album_id WHERE a2.artist_id=al.artist_id) size
                 FROM albums al GROUP BY al.artist_id""")
    sm = {r["artist_id"]: r for r in st}
    nxt = {r["artist_id"]: r for r in db.q("""SELECT artist_id, title, MIN(release_date) d FROM albums
                                             WHERE release_date > date('now') GROUP BY artist_id""")}
    last = {r["artist_id"]: r for r in db.q("""SELECT artist_id, title, MAX(release_date) d FROM albums
                                              WHERE release_date <= date('now') GROUP BY artist_id""")}
    for a in arts:
        s = sm.get(a["id"], {})
        a.update(albums=s.get("albums", 0), monitored_albums=s.get("monitored_albums", 0) or 0,
                 track_total=s.get("tracks", 0) or 0, track_have=s.get("have", 0) or 0, size=s.get("size", 0) or 0,
                 next_album=nxt.get(a["id"]), last_album=last.get(a["id"]))
    return arts


# ---------------- system ----------------
async def status(req):
    return J({"version": VERSION, "uptime": int(time.time() - STARTED), "database": db.DB_PATH,
              "config": db.CONFIG_DIR, "mutagen": importer.HAVE_MUTAGEN,
              "artists": db.q1("SELECT COUNT(*) c FROM artists")["c"], "albums": db.q1("SELECT COUNT(*) c FROM albums")["c"],
              "tracks_on_disk": db.q1("SELECT COUNT(*) c FROM tracks WHERE file_path IS NOT NULL")["c"],
              "disk_size": fmt_size(db.q1("SELECT COALESCE(SUM(file_size),0) s FROM tracks")["s"])})


async def health(req):
    issues = []
    if not db.q("SELECT 1 FROM root_folders"):
        issues.append({"level": "error", "message": "No root folder configured (Settings → Media Management)"})
    for rf in db.q("SELECT path FROM root_folders"):
        if not os.path.isdir(rf["path"]):
            issues.append({"level": "error", "message": f"Root folder {rf['path']} is missing or not mounted"})
    if not db.q("SELECT 1 FROM indexers WHERE enabled=1") and not db.q("SELECT 1 FROM download_clients WHERE kind='slskd' AND enabled=1"):
        issues.append({"level": "warning", "message": "No indexers or Soulseek client enabled — nothing can be searched"})
    protos = {downloaders.KINDS[c["kind"]] for c in db.q("SELECT kind FROM download_clients WHERE enabled=1")}
    ixp = {indexers.protocol_of(i) for i in db.q("SELECT kind FROM indexers WHERE enabled=1")}
    for p in ixp - protos:
        issues.append({"level": "warning", "message": f"You have {p} indexers but no {p} download client"})
    if not importer.HAVE_MUTAGEN:
        issues.append({"level": "warning", "message": "mutagen not installed: tagging and quality probing disabled"})
    blocked = db.q1("SELECT COUNT(*) c FROM queue WHERE status='importBlocked'")["c"]
    if blocked:
        issues.append({"level": "warning", "message": f"{blocked} download(s) need manual import (Activity → Queue)"})
    return J(issues)


async def logs(req):
    lvl = req.query_params.get("level")
    items = list(LOG_BUFFER)[::-1]
    if lvl:
        items = [i for i in items if i["level"] == lvl]
    return J(items[:1000])


async def meta(req):
    return J({"qualities": QUALITIES, "download_client_kinds": downloaders.KINDS, "notification_kinds": notify.KINDS,
              "monitor_options": library.MONITOR_OPTIONS,
              "primary_types": ["Album", "EP", "Single", "Broadcast", "Other"],
              "secondary_types": ["Compilation", "Soundtrack", "Spokenword", "Interview", "Audiobook", "Audio drama",
                                  "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo", "Field recording"],
              "release_statuses": ["Official", "Promotion", "Bootleg", "Pseudo-Release"],
              "import_list_kinds": ["listenbrainz_top", "listenbrainz_similar", "lastfm_user", "musicbrainz_collection", "mbid_list"],
              "naming_tokens": ["{Album Artist}", "{Artist Name}", "{Artist CleanName}", "{Artist Disambiguation}", "{Album Title}",
                                "{Album Type}", "{Release Year}", "{track:00}", "{medium:0}", "{Track Title}", "{Track Artist}", "{Quality}"]})


async def settings_get(req):
    s = db.all_settings()
    return J(s)


async def settings_put(req):
    data = await body(req)
    for k, v in data.items():
        if k in db.DEFAULT_SETTINGS or k == "api_key":
            db.set_setting(k, v)
    return J(db.all_settings())


async def naming_preview(req):
    a = {"name": "The Beatles", "disambiguation": "", "path": None, "root_folder": "/music"}
    al = {"title": "Abbey Road", "release_date": "1969-09-26", "album_type": "Album"}
    t = {"title": "Come Together", "position": 1, "disc": 1, "artist_credit": "The Beatles"}
    d = importer.album_path(a, al)
    return J({"album_path": d, "track": importer.track_filename(a, al, t, ".flac", "FLAC", False),
              "multi_disc_track": importer.track_filename(a, al, dict(t, disc=2), ".flac", "FLAC", True)})


# ---------------- artists ----------------
async def artists_list(req):
    return J(await off(artist_rows))


@err
async def artist_get(req):
    aid = int(req.path_params["id"])
    a = db.decode(db.q1("SELECT * FROM artists WHERE id=?", (aid,)))
    if not a:
        return J({"error": "not found"}, 404)
    albums = [db.decode(x) for x in db.q("SELECT * FROM albums WHERE artist_id=? ORDER BY release_date DESC", (aid,))]
    st = album_stats()
    queue = {q["album_id"]: q for q in db.q("SELECT * FROM queue WHERE status NOT IN ('imported','failed')")}
    for al in albums:
        s = st.get(al["id"], {})
        al.update(track_total=s.get("total", 0), track_have=s.get("have", 0) or 0, size=s.get("size", 0) or 0,
                  quality=search.current_quality(al["id"]), queued=queue.get(al["id"]))
    a["albums"] = albums
    a["path"] = a.get("path") or importer.artist_path(a)
    return J(a)


@err
async def artist_add(req):
    d = await body(req)
    aid = await off(library.add_artist, d["mbid"], d.get("quality_profile_id"), d.get("metadata_profile_id"),
                    d.get("root_folder"), d.get("monitor", "all"), d.get("monitor_new", "all"), tags=d.get("tags"),
                    path=d.get("path"))
    tasks.enqueue(f"Add Artist", tasks.add_artist_flow, aid, d.get("monitor", "all"),
                  d.get("search", db.get_setting("search_on_add")))
    return J({"id": aid})


@err
async def artist_put(req):
    aid = int(req.path_params["id"])
    d = await body(req)
    allowed = {"monitored", "monitor_new", "quality_profile_id", "metadata_profile_id", "root_folder", "path", "tags"}
    upd = {k: v for k, v in d.items() if k in allowed}
    old = db.q1("SELECT metadata_profile_id FROM artists WHERE id=?", (aid,))
    db.update("artists", aid, upd)
    if "metadata_profile_id" in upd and old and old["metadata_profile_id"] != upd["metadata_profile_id"]:
        tasks.enqueue("Refresh Artist", library.refresh_artist, aid)
    return J({"ok": True})


async def artists_editor(req):
    d = await body(req)
    allowed = {"monitored", "monitor_new", "quality_profile_id", "metadata_profile_id", "root_folder", "tags"}
    for aid in d.get("ids", []):
        db.update("artists", int(aid), {k: v for k, v in d.items() if k in allowed})
    return J({"ok": True})


async def artist_delete(req):
    aid = int(req.path_params["id"])
    qp = req.query_params
    await off(library.delete_artist, aid, qp.get("delete_files") == "true", qp.get("exclude") == "true")
    return J({"ok": True})


async def artist_command(req):
    aid = int(req.path_params["id"])
    action = req.path_params["action"]
    a = db.q1("SELECT name FROM artists WHERE id=?", (aid,))
    if action == "refresh":
        def run():
            library.refresh_artist(aid)
            importer.scan_artist(aid)
        c = tasks.enqueue(f"Refresh {a['name']}", run)
    elif action == "search":
        c = tasks.enqueue(f"Search {a['name']}", tasks.artist_search, aid)
    elif action == "scan":
        c = tasks.enqueue(f"Rescan {a['name']}", importer.scan_artist, aid)
    else:
        return J({"error": "unknown action"}, 400)
    return J(c)


@err
async def lookup_artists(req):
    return J(await off(mb.search_artists, req.query_params.get("term", "")))


@err
async def lookup_albums(req):
    res = await off(mb.search_albums, req.query_params.get("term", ""))
    for r in res:
        a = db.q1("SELECT id, artist_id FROM albums WHERE mbid=?", (r["mbid"],))
        r["existing"] = a
        r["artist_existing"] = bool(db.q1("SELECT 1 FROM artists WHERE mbid=?", (r["artist_mbid"],)))
    return J(res)


# ---------------- albums ----------------
@err
async def album_get(req):
    aid = int(req.path_params["id"])
    al = db.decode(db.q1("SELECT * FROM albums WHERE id=?", (aid,)))
    if not al:
        return J({"error": "not found"}, 404)
    al["tracks"] = db.q("SELECT * FROM tracks WHERE album_id=? ORDER BY disc, position", (aid,))
    al["artist"] = db.decode(db.q1("SELECT id,name,mbid,quality_profile_id FROM artists WHERE id=?", (al["artist_id"],)))
    al["quality"] = search.current_quality(aid)
    al["history"] = [db.decode(h) for h in db.q("SELECT * FROM history WHERE album_id=? ORDER BY id DESC LIMIT 50", (aid,))]
    return J(al)


@err
async def album_put(req):
    aid = int(req.path_params["id"])
    d = await body(req)
    upd = {k: v for k, v in d.items() if k in ("monitored", "any_release_ok")}
    db.update("albums", aid, upd)
    if d.get("release_mbid"):
        al = db.q1("SELECT * FROM albums WHERE id=?", (aid,))
        await off(library.refresh_tracks, al, d["release_mbid"])
    return J({"ok": True})


async def albums_monitor(req):
    d = await body(req)
    for aid in d.get("ids", []):
        db.update("albums", int(aid), {"monitored": int(bool(d.get("monitored")))})
    return J({"ok": True})


@err
async def album_releases(req):
    al = db.q1("SELECT mbid FROM albums WHERE id=?", (int(req.path_params["id"]),))
    return J(await off(mb.album_releases, al["mbid"]))


async def album_search_cmd(req):
    aid = int(req.path_params["id"])
    al = db.q1("SELECT title FROM albums WHERE id=?", (aid,))
    return J(tasks.enqueue(f"Search {al['title']}", tasks.album_search, [aid]))


async def albums_search_cmd(req):
    d = await body(req)
    ids = [int(i) for i in d.get("ids", [])]
    return J(tasks.enqueue(f"Search {len(ids)} album(s)", tasks.album_search, ids))


@err
async def album_interactive(req):
    res = await off(search.interactive, int(req.path_params["id"]))
    return J(res)


@err
async def album_grab(req):
    rel = await body(req)
    qid = await off(search.grab, int(req.path_params["id"]), rel)
    return J({"queue_id": qid})


@err
async def album_preview(req):
    return J(await off(importer.preview, int(req.path_params["id"]), req.query_params["folder"]))


@err
async def album_import(req):
    d = await body(req)
    ov = {int(k): v for k, v in (d.get("overrides") or {}).items() if v} or None
    res = await off(importer.import_album, int(req.path_params["id"]), d["folder"], d.get("mode"), bool(d.get("force")),
                    d.get("folder"), None, ov)
    if d.get("queue_id"):
        db.update("queue", int(d["queue_id"]), {"status": "imported", "message": "Manually imported"})
    return J(res)


async def album_delete_files(req):
    aid = int(req.path_params["id"])
    for t in db.q("SELECT id, file_path FROM tracks WHERE album_id=? AND file_path IS NOT NULL", (aid,)):
        try:
            importer._retire(t["file_path"])
        except OSError:
            pass
        db.update("tracks", t["id"], {"file_path": None, "quality": None, "file_size": None})
    db.add_history("fileDeleted", aid)
    return J({"ok": True})


# ---------------- calendar / wanted ----------------
async def calendar(req):
    s = req.query_params.get("start") or time.strftime("%Y-%m-01")
    e = req.query_params.get("end") or "9999-12-31"
    rows = db.q("""SELECT al.*, ar.name artist_name FROM albums al JOIN artists ar ON ar.id=al.artist_id
                   WHERE al.release_date BETWEEN ? AND ? ORDER BY al.release_date""", (s, e))
    st = album_stats()
    for r in rows:
        x = st.get(r["id"], {})
        r["track_have"], r["track_total"] = x.get("have", 0) or 0, x.get("total", 0)
    return J(rows)


async def calendar_ics(req):
    rows = db.q("""SELECT al.id, al.title, al.release_date, ar.name FROM albums al JOIN artists ar ON ar.id=al.artist_id
                   WHERE al.release_date >= date('now','-60 day') AND al.monitored=1 AND length(al.release_date)=10""")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Musicarr//EN", "X-WR-CALNAME:Musicarr"]
    for r in rows:
        d = r["release_date"].replace("-", "")
        lines += ["BEGIN:VEVENT", f"UID:musicarr-{r['id']}", f"DTSTAMP:{d}T000000Z", f"DTSTART;VALUE=DATE:{d}",
                  f"SUMMARY:{r['name']} - {r['title']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    from starlette.responses import Response
    return Response("\r\n".join(lines), media_type="text/calendar")


def _with_artist(rows):
    names = {a["id"]: a["name"] for a in db.q("SELECT id, name FROM artists")}
    for r in rows:
        r["artist_name"] = names.get(r["artist_id"])
    return rows


async def wanted_missing(req):
    return J(_with_artist(await off(search.wanted_missing)))


async def wanted_cutoff(req):
    return J(_with_artist(await off(search.wanted_cutoff)))


# ---------------- activity ----------------
async def queue_list(req):
    rows = db.q("""SELECT q.*, al.title album_title, ar.name artist_name, ar.id artist_id, c.name client_name
                   FROM queue q LEFT JOIN albums al ON al.id=q.album_id LEFT JOIN artists ar ON ar.id=al.artist_id
                   LEFT JOIN download_clients c ON c.id=q.client_id
                   WHERE q.status NOT IN ('imported') OR q.updated > datetime('now','-1 hour') ORDER BY q.id DESC""")
    return J([db.decode(r) for r in rows])


async def queue_delete(req):
    qid = int(req.path_params["id"])
    item = db.q1("SELECT * FROM queue WHERE id=?", (qid,))
    if not item:
        return J({"ok": True})
    qp = req.query_params
    c = db.decode(db.q1("SELECT * FROM download_clients WHERE id=?", (item["client_id"],)))
    if qp.get("blocklist") == "true":
        await off(tasks.fail, item, c, "Removed by user", True, qp.get("search") == "true")
    elif qp.get("remove") == "true" and c:
        try:
            await off(downloaders.impl(c).remove, c, item["download_id"], True)
        except Exception as e:
            log.warning("Remove from client failed: %s", e)
    db.ex("DELETE FROM queue WHERE id=?", (qid,))
    return J({"ok": True})


async def queue_retry(req):
    qid = int(req.path_params["id"])
    db.update("queue", qid, {"status": "downloading", "message": ""})
    tasks.enqueue("Check Downloads", tasks.check_downloads)
    return J({"ok": True})


async def history_list(req):
    page = int(req.query_params.get("page", 1))
    size = int(req.query_params.get("size", 50))
    ev = req.query_params.get("event")
    where, args = ("WHERE h.event=?", [ev]) if ev else ("", [])
    rows = db.q(f"""SELECT h.*, al.title album_title, ar.name artist_name FROM history h
                    LEFT JOIN albums al ON al.id=h.album_id LEFT JOIN artists ar ON ar.id=h.artist_id
                    {where} ORDER BY h.id DESC LIMIT ? OFFSET ?""", args + [size, (page - 1) * size])
    total = db.q1(f"SELECT COUNT(*) c FROM history h {where}", args)["c"]
    return J({"items": [db.decode(r) for r in rows], "total": total, "page": page, "size": size})


async def history_failed(req):
    h = db.q1("SELECT * FROM history WHERE id=?", (int(req.path_params["id"]),))
    db.insert("blocklist", dict(album_id=h["album_id"], source_title=h["source_title"], protocol=h["protocol"],
                                indexer="", reason="Marked as failed", date=db.now()))
    tasks.enqueue("Album Search", tasks.album_search, [h["album_id"]])
    return J({"ok": True})


async def blocklist_list(req):
    return J(db.q("""SELECT b.*, al.title album_title, ar.name artist_name FROM blocklist b
                     LEFT JOIN albums al ON al.id=b.album_id LEFT JOIN artists ar ON ar.id=al.artist_id ORDER BY b.id DESC"""))


async def blocklist_delete(req):
    rid = req.path_params["id"]
    if rid == "all":
        db.ex("DELETE FROM blocklist")
    else:
        db.ex("DELETE FROM blocklist WHERE id=?", (int(rid),))
    return J({"ok": True})


# ---------------- generic CRUD for settings resources ----------------
RESOURCES = {
    "quality_profiles": ["name", "allowed", "cutoff", "upgrade_allowed", "preferred_words", "required_words", "ignored_words",
                         "min_seeders", "protocol_order"],
    "metadata_profiles": ["name", "primary_types", "secondary_types", "release_statuses"],
    "indexers": ["name", "kind", "url", "api_key", "categories", "enabled", "rss", "search", "priority", "min_seeders"],
    "download_clients": ["name", "kind", "url", "username", "password", "api_key", "category", "enabled", "priority",
                         "download_path", "remove_completed", "extra"],
    "notifications": ["name", "kind", "config", "on_grab", "on_import", "on_upgrade", "on_failure", "on_health", "enabled"],
    "import_lists": ["name", "kind", "config", "enabled", "quality_profile_id", "metadata_profile_id", "root_folder",
                     "monitor", "search_on_add"],
    "root_folders": ["path"],
    "remote_path_mappings": ["client_id", "remote_path", "local_path"],
    "import_exclusions": ["mbid", "name"],
}


def _res(req):
    r = req.path_params["res"]
    if r not in RESOURCES:
        raise KeyError("Unknown resource")
    return r


@err
async def res_list(req):
    r = _res(req)
    rows = [db.decode(x) for x in db.q(f"SELECT * FROM {r} ORDER BY id")]
    if r == "root_folders":
        for x in rows:
            try:
                du = shutil.disk_usage(x["path"])
                x["free"], x["total"] = du.free, du.total
                x["unmapped"] = sum(1 for u in importer.unmapped_folders() if u["root_folder"] == x["path"])
            except OSError:
                x["free"] = x["total"] = None
                x["missing"] = True
    if r == "download_clients":
        for x in rows:
            x["protocol"] = downloaders.KINDS.get(x["kind"])
    return J(rows)


def _clean(r, d):
    data = {k: d[k] for k in RESOURCES[r] if k in d}
    if r == "download_clients" and "kind" in data:
        data["protocol"] = downloaders.KINDS.get(data["kind"])
    return data


@err
async def res_create(req):
    r = _res(req)
    d = await body(req)
    data = _clean(r, d)
    if r == "root_folders":
        os.makedirs(data["path"], exist_ok=True)
    rid = db.insert(r, data)
    return J({"id": rid})


@err
async def res_update(req):
    r = _res(req)
    d = await body(req)
    db.update(r, int(req.path_params["id"]), _clean(r, d))
    return J({"ok": True})


@err
async def res_delete(req):
    r = _res(req)
    db.ex(f"DELETE FROM {r} WHERE id=?", (int(req.path_params["id"]),))
    return J({"ok": True})


async def res_test(req):
    r = req.path_params["res"]
    d = db.decode(await body(req))
    d.setdefault("id", 0)
    try:
        if r == "indexers":
            res = await off(indexers.test, d)
        elif r == "download_clients":
            res = await off(downloaders.IMPL[d["kind"]].test, d)
        elif r == "notifications":
            res = await off(notify.test, d)
        else:
            return J({"ok": False, "message": "Nothing to test"})
        return J(res)
    except Exception as e:
        return J({"ok": False, "message": str(e)})


# ---------------- commands / tasks ----------------
async def commands_list(req):
    return J(list(tasks.COMMANDS)[:100])


async def command_run(req):
    d = await body(req)
    name = d.get("name")
    if name not in tasks.TASKS:
        return J({"error": "unknown task"}, 400)
    tasks.LAST_RUN[name] = time.time()
    return J(tasks.enqueue(name, tasks.TASKS[name]))


async def task_list(req):
    out = []
    for name in list(tasks.SCHEDULE) + ["Cutoff Unmet Search"]:
        iv = tasks.interval(name) if name in tasks.SCHEDULE else 0
        last = tasks.LAST_RUN.get(name)
        out.append({"name": name, "interval": iv, "last": last, "next": (last + iv) if last and iv else None,
                    "running": any(c["name"] == name and c["status"] == "running" for c in tasks.COMMANDS)})
    return J(out)


# ---------------- library import / filesystem ----------------
async def unmapped(req):
    return J(await off(importer.unmapped_folders))


@err
async def library_import(req):
    d = await body(req)
    items, seen = [], set()
    for item in d.get("items", []):
        if not item.get("mbid") or item["mbid"] in seen:
            continue
        seen.add(item["mbid"])
        items.append(item)
    if not items:
        return J({"error": "Nothing to import"}, 400)
    if any(c["name"].startswith("Library Import") and c["status"] in ("queued", "running") for c in tasks.COMMANDS):
        return J({"error": "A library import is already running — check System → Tasks"}, 409)
    cmd = tasks.enqueue(f"Library Import ({len(items)} artists)", tasks.library_import, items)
    return J({"queued": len(items), "command": cmd})


async def filesystem(req):
    p = req.query_params.get("path") or "/"
    try:
        dirs = sorted([n for n in os.listdir(p) if os.path.isdir(os.path.join(p, n)) and not n.startswith(".")], key=str.lower)
        files = sorted([n for n in os.listdir(p) if os.path.isfile(os.path.join(p, n))], key=str.lower)[:300]
    except OSError as e:
        return J({"path": p, "error": str(e), "dirs": [], "files": []})
    return J({"path": p, "parent": os.path.dirname(p.rstrip("/")) or "/", "dirs": dirs, "files": files})


async def stats(req):
    q = db.q1
    return J({
        "artists": q("SELECT COUNT(*) c FROM artists")["c"],
        "albums": q("SELECT COUNT(*) c FROM albums")["c"],
        "monitored_albums": q("SELECT COUNT(*) c FROM albums WHERE monitored=1")["c"],
        "tracks_have": q("SELECT COUNT(*) c FROM tracks WHERE file_path IS NOT NULL")["c"],
        "tracks_total": q("SELECT COUNT(*) c FROM tracks")["c"],
        "size": q("SELECT COALESCE(SUM(file_size),0) s FROM tracks")["s"],
        "missing": len(search.wanted_missing()),
        "queue": q("SELECT COUNT(*) c FROM queue WHERE status NOT IN ('imported','failed')")["c"],
        "qualities": db.q("SELECT quality, COUNT(*) c FROM tracks WHERE quality IS NOT NULL GROUP BY quality ORDER BY c DESC"),
        "genres": [],
    })


# ---------------- artwork ----------------
@err
async def album_artwork_get(req):
    aid = int(req.path_params["id"])
    al = db.decode(db.q1("SELECT * FROM albums WHERE id=?", (aid,)))
    files = [t["file_path"] for t in db.q("SELECT file_path FROM tracks WHERE album_id=? AND file_path IS NOT NULL", (aid,))]
    embedded = await off(lambda: sum(1 for f in files if os.path.exists(f) and artwork.has_art(f)))
    options = await off(artwork.caa_options, al)
    lc = artwork.local_cover(aid)
    return J({"current": al.get("cover"), "local_cover": f"/api/v1/albums/{aid}/cover?v={int(os.path.getmtime(lc))}" if lc else None,
              "files": len(files), "embedded": embedded, "options": options})


@err
async def album_artwork_set(req):
    import base64
    aid = int(req.path_params["id"])
    d = await body(req)
    data = None
    if d.get("data"):
        raw = d["data"].split(",", 1)[-1]
        data = base64.b64decode(raw)
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("Image is larger than 25 MB")
    res = await off(artwork.update_album, aid, data, d.get("url"), bool(d.get("replace", True)))
    await off(artwork._rescan)
    return J(res)


async def album_cover(req):
    from starlette.responses import FileResponse
    p = artwork.local_cover(int(req.path_params["id"]))
    if not p:
        return J({"error": "no cover file"}, 404)
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


async def artist_artwork(req):
    aid = int(req.path_params["id"])
    d = await body(req)
    a = db.q1("SELECT name FROM artists WHERE id=?", (aid,))
    replace = bool(d.get("replace", True))
    return J(tasks.enqueue(f"Artwork {a['name']}", artwork.update_artist, aid, replace, bool(d.get("missing_only"))))


async def library_artwork(req):
    d = await body(req)
    name = "Refresh All Artwork" if d.get("replace") else "Artwork Scan"
    tasks.LAST_RUN[name] = __import__("time").time()
    return J(tasks.enqueue(name, tasks.TASKS[name]))


# ---------------- Soulseek (slskd management) ----------------
async def slsk_status(req):
    return J(await off(slskd_admin.status))


@err
async def slsk_save(req):
    d = await body(req)
    search_keys = ("soulseek_search_timeout_sec", "soulseek_min_upload_speed_kbps", "soulseek_max_queue", "allow_partial_soulseek")
    for k in search_keys:
        if k in d:
            db.set_setting(k, d.pop(k))
    res = {"ok": True}
    if any(k in d for k in list(slskd_admin.FIELDS) + ["share_dirs"]):
        res = await off(slskd_admin.save, d)
    return J(res)


@err
async def slsk_connection(req):
    return J(await off(slskd_admin.save_connection, await body(req)))


@err
async def slsk_action(req):
    fn = {"connect": slskd_admin.connect, "disconnect": slskd_admin.disconnect, "reconnect": slskd_admin.reconnect}[req.path_params["action"]]
    return J(await off(fn))


P = "/api/v1"
routes = [
    Route(P + "/system/status", status), Route(P + "/health", health), Route(P + "/logs", logs), Route(P + "/meta", meta),
    Route(P + "/stats", stats),
    Route(P + "/settings", settings_get), Route(P + "/settings", settings_put, methods=["PUT"]),
    Route(P + "/naming/preview", naming_preview),
    Route(P + "/artists", artists_list), Route(P + "/artists", artist_add, methods=["POST"]),
    Route(P + "/artists/editor", artists_editor, methods=["PUT"]),
    Route(P + "/artists/{id:int}", artist_get), Route(P + "/artists/{id:int}", artist_put, methods=["PUT"]),
    Route(P + "/artists/{id:int}", artist_delete, methods=["DELETE"]),
    Route(P + "/artists/{id:int}/artwork", artist_artwork, methods=["POST"]),
    Route(P + "/artists/{id:int}/{action}", artist_command, methods=["POST"]),
    Route(P + "/lookup/artists", lookup_artists), Route(P + "/lookup/albums", lookup_albums),
    Route(P + "/albums/monitor", albums_monitor, methods=["PUT"]),
    Route(P + "/albums/search", albums_search_cmd, methods=["POST"]),
    Route(P + "/albums/{id:int}", album_get), Route(P + "/albums/{id:int}", album_put, methods=["PUT"]),
    Route(P + "/albums/{id:int}/releases", album_releases),
    Route(P + "/albums/{id:int}/search", album_search_cmd, methods=["POST"]),
    Route(P + "/albums/{id:int}/interactive", album_interactive),
    Route(P + "/albums/{id:int}/grab", album_grab, methods=["POST"]),
    Route(P + "/albums/{id:int}/preview", album_preview),
    Route(P + "/albums/{id:int}/import", album_import, methods=["POST"]),
    Route(P + "/albums/{id:int}/files", album_delete_files, methods=["DELETE"]),
    Route(P + "/albums/{id:int}/artwork", album_artwork_get), Route(P + "/albums/{id:int}/artwork", album_artwork_set, methods=["POST"]),
    Route(P + "/albums/{id:int}/cover", album_cover),
    Route(P + "/artwork/library", library_artwork, methods=["POST"]),
    Route(P + "/calendar", calendar), Route("/feed/calendar.ics", calendar_ics),
    Route(P + "/wanted/missing", wanted_missing), Route(P + "/wanted/cutoff", wanted_cutoff),
    Route(P + "/queue", queue_list), Route(P + "/queue/{id:int}", queue_delete, methods=["DELETE"]),
    Route(P + "/queue/{id:int}/retry", queue_retry, methods=["POST"]),
    Route(P + "/history", history_list), Route(P + "/history/{id:int}/failed", history_failed, methods=["POST"]),
    Route(P + "/blocklist", blocklist_list), Route(P + "/blocklist/{id}", blocklist_delete, methods=["DELETE"]),
    Route(P + "/commands", commands_list), Route(P + "/commands", command_run, methods=["POST"]),
    Route(P + "/tasks", task_list),
    Route(P + "/library/unmapped", unmapped), Route(P + "/library/import", library_import, methods=["POST"]),
    Route(P + "/filesystem", filesystem),
    Route(P + "/soulseek", slsk_status), Route(P + "/soulseek", slsk_save, methods=["PUT"]),
    Route(P + "/soulseek/connection", slsk_connection, methods=["PUT"]),
    Route(P + "/soulseek/{action}", slsk_action, methods=["POST"]),
    Route(P + "/res/{res}/test", res_test, methods=["POST"]),
    Route(P + "/res/{res}", res_list), Route(P + "/res/{res}", res_create, methods=["POST"]),
    Route(P + "/res/{res}/{id:int}", res_update, methods=["PUT"]),
    Route(P + "/res/{res}/{id:int}", res_delete, methods=["DELETE"]),
]
