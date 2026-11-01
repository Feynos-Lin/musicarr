"""Command queue + scheduled tasks + download monitoring (completed-download handling and failed-download handling)."""
import asyncio
import collections
import itertools
import os
import shutil
import sqlite3
import threading
import time
import traceback

from . import db, importer, library, notify, search
from .downloaders import safe_status, impl
from .util import log, map_path

_ids = itertools.count(1)
COMMANDS = collections.deque(maxlen=200)
_queue: asyncio.Queue = None
_heavy_queue: asyncio.Queue = None
_loop = None

SCHEDULE = {  # name: (setting key, unit seconds, default)
    "RSS Sync": ("rss_interval_min", 60, 15),
    "Check Downloads": ("download_check_interval_sec", 1, 60),
    "Missing Album Search": ("missing_search_interval_hours", 3600, 24),
    "Refresh Artists": ("refresh_interval_hours", 3600, 12),
    "Library Scan": ("library_scan_interval_hours", 3600, 24),
    "Import List Sync": (None, 3600, 12),
    "Artwork Scan": ("artwork_interval_days", 86400, 7),
    "Backup": (None, 86400, 7),
    "Housekeeping": (None, 86400, 1),
}
LAST_RUN = {}
# Jobs that read/write every file in the library. Only one runs at a time, so a slow (e.g. Windows) mount
# isn't hammered by several full scans at once.
HEAVY = {"Library Scan", "Artwork Scan", "Refresh All Artwork", "Refresh Artists", "Missing Album Search", "Cutoff Unmet Search"}


def is_heavy(name):
    base = name.split(" (")[0]
    return base in HEAVY or base.startswith(("Library Import", "Artwork ", "Import ", "Refresh ", "Rescan "))
# On a fresh install, first runs are spread out instead of all firing a minute after startup.
FIRST_DELAY = {"RSS Sync": 120, "Import List Sync": 900, "Missing Album Search": 1800, "Housekeeping": 3600,
               "Refresh Artists": 2 * 3600, "Library Scan": 4 * 3600, "Artwork Scan": 6 * 3600, "Backup": 8 * 3600}


def _load_last_run():
    saved = db.get_setting("task_last_run") or {}
    now = time.time()
    for name in SCHEDULE:
        if name == "Check Downloads":
            continue
        if name in saved:
            LAST_RUN[name] = float(saved[name])
        else:
            # pretend it last ran so the first run lands FIRST_DELAY from now
            LAST_RUN[name] = now - interval(name) + FIRST_DELAY.get(name, 3600)


def _save_last_run():
    db.set_setting("task_last_run", {k: v for k, v in LAST_RUN.items() if k in SCHEDULE and k != "Check Downloads"})


def interval(name):
    key, unit, default = SCHEDULE[name]
    v = db.get_setting(key) if key else default
    try:
        v = float(v)
    except (TypeError, ValueError):
        v = default
    return v * unit


def enqueue(name, fn, *args, **kw):
    cmd = {"id": next(_ids), "name": name, "status": "queued", "queued": db.now(), "started": None, "ended": None, "message": ""}
    COMMANDS.appendleft(cmd)
    if _loop is None:
        return cmd
    q = _heavy_queue if is_heavy(name) else _queue
    _loop.call_soon_threadsafe(q.put_nowait, (cmd, fn, args, kw))
    return cmd


async def _worker(queue):
    while True:
        cmd, fn, args, kw = await queue.get()
        cmd["status"], cmd["started"] = "running", db.now()
        try:
            res = await asyncio.to_thread(fn, *args, **kw)
            cmd["status"] = "completed"
            if isinstance(res, str):
                cmd["message"] = res
        except Exception as e:
            cmd["status"], cmd["message"] = "failed", str(e)
            log.error("Command %s failed: %s\n%s", cmd["name"], e, traceback.format_exc())
        cmd["ended"] = db.now()


async def _scheduler():
    await asyncio.sleep(10)
    _load_last_run()
    while True:
        for name in SCHEDULE:
            if name == "Check Downloads":
                continue
            iv = interval(name)
            if iv <= 0:
                continue
            if time.time() - LAST_RUN.get(name, time.time()) >= iv:
                LAST_RUN[name] = time.time()
                _save_last_run()
                if not any(c["name"] == name and c["status"] in ("queued", "running") for c in COMMANDS):
                    enqueue(name, TASKS[name])
        await asyncio.sleep(20)


async def _download_monitor():
    await asyncio.sleep(5)
    while True:
        try:
            LAST_RUN["Check Downloads"] = time.time()
            await asyncio.to_thread(check_downloads)
        except Exception as e:
            log.error("Download monitor error: %s", e)
        await asyncio.sleep(max(15, interval("Check Downloads")))


def start(loop):
    global _queue, _heavy_queue, _loop
    _loop = loop
    _queue = asyncio.Queue()
    _heavy_queue = asyncio.Queue()
    for _ in range(2):
        loop.create_task(_worker(_queue))
    loop.create_task(_worker(_heavy_queue))  # disk-heavy jobs run one at a time
    loop.create_task(_scheduler())
    loop.create_task(_download_monitor())


# ---------------- task bodies ----------------
def add_artist_flow(artist_id, monitor="all", search_now=True):
    library.refresh_artist(artist_id, initial_monitor=monitor)
    importer.scan_artist(artist_id)
    library.apply_monitor_existing(artist_id, monitor)
    if monitor == "missing":
        db.ex("""UPDATE albums SET monitored = NOT EXISTS(SELECT 1 FROM tracks t WHERE t.album_id=albums.id AND t.file_path IS NOT NULL)
                 WHERE artist_id=?""", (artist_id,))
    if search_now:
        artist_search(artist_id)


def artist_search(artist_id):
    n = 0
    for al in search.wanted_missing():
        if al["artist_id"] == artist_id:
            if search.auto(al["id"]):
                n += 1
    for al in search.wanted_cutoff():
        if al["artist_id"] == artist_id and search.auto(al["id"]):
            n += 1
    return f"Grabbed {n} release(s)"


def album_search(album_ids):
    n = sum(1 for aid in album_ids if search.auto(aid))
    return f"Grabbed {n} of {len(album_ids)}"


def missing_search(limit=50):
    ids = [a["id"] for a in search.wanted_missing(limit)]
    return album_search(ids)


def cutoff_search():
    return album_search([a["id"] for a in search.wanted_cutoff()])


def rss_sync():
    return f"Grabbed {search.rss_sync()} release(s) from RSS"


def refresh_all():
    for a in db.q("SELECT id FROM artists ORDER BY last_refresh"):
        new = library.refresh_artist(a["id"]) or []
        for aid in new:
            al = db.q1("SELECT * FROM albums WHERE id=?", (aid,))
            if al and al["monitored"] and (not al["release_date"] or al["release_date"] <= db.now()[:10]):
                enqueue(f"Album Search", album_search, [aid])
    return "Refreshed all artists"


def library_scan():
    total = sum(importer.scan_artist(a["id"]) for a in db.q("SELECT id FROM artists"))
    return f"Linked {total} file(s)"


def library_import(items):
    """Add each matched folder as an artist; one bad folder never stops the rest."""
    added, skipped, failed = 0, 0, []
    for item in items:
        name = os.path.basename(item.get("path", "")) or item["mbid"]
        try:
            existing = db.q1("SELECT id, path FROM artists WHERE mbid=?", (item["mbid"],))
            if existing:
                if not existing["path"] and item.get("path"):
                    db.update("artists", existing["id"], {"path": item["path"]})
                    enqueue("Rescan", importer.scan_artist, existing["id"])
                skipped += 1
                continue
            monitor = item.get("monitor", "existing")
            aid = library.add_artist(item["mbid"], item.get("quality_profile_id"), item.get("metadata_profile_id"),
                                     item.get("root_folder"), monitor, path=item.get("path"))
            enqueue(f"Import {name}", add_artist_flow, aid, monitor, bool(item.get("search")))
            added += 1
        except Exception as e:
            log.warning("Library import of %s failed: %s", name, e)
            failed.append(f"{name}: {e}")
    msg = f"Added {added}, already in library {skipped}, failed {len(failed)}"
    if failed:
        msg += " — " + "; ".join(failed[:5]) + (" …" if len(failed) > 5 else "")
    return msg


def import_list_sync():
    added = 0
    for il in db.q("SELECT * FROM import_lists WHERE enabled=1"):
        il = db.decode(il)
        try:
            for mbid, name in library.import_list_artists(il):
                if db.q1("SELECT 1 FROM artists WHERE mbid=?", (mbid,)) or db.q1("SELECT 1 FROM import_exclusions WHERE mbid=?", (mbid,)):
                    continue
                aid = library.add_artist(mbid, il["quality_profile_id"], il["metadata_profile_id"], il["root_folder"],
                                         monitor=il["monitor"])
                add_artist_flow(aid, il["monitor"], bool(il["search_on_add"]))
                added += 1
            db.update("import_lists", il["id"], {"last_sync": db.now()})
        except Exception as e:
            log.warning("Import list %s failed: %s", il["name"], e)
    return f"Added {added} artist(s)"


def backup():
    d = os.path.join(db.CONFIG_DIR, "backups")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, f"musicarr_{time.strftime('%Y%m%d_%H%M%S')}.db")
    src = db.conn()
    out = sqlite3.connect(dst)
    with db._lock:
        src.backup(out)
    out.close()
    files = sorted(f for f in os.listdir(d) if f.endswith(".db"))
    for f in files[:-14]:
        os.remove(os.path.join(d, f))
    return f"Backup written to {dst}"


def housekeeping():
    db.ex("DELETE FROM history WHERE date < date('now','-365 day')")
    db.ex("DELETE FROM queue WHERE status='imported' AND updated < date('now','-1 day')")
    db.ex("DELETE FROM tracks WHERE album_id NOT IN (SELECT id FROM albums)")
    db.ex("PRAGMA optimize")
    return "Done"


def artwork_scan():
    from . import artwork
    return artwork.update_library(replace=False, missing_only=True)


def artwork_refresh_all():
    from . import artwork
    return artwork.update_library(replace=True, missing_only=False)


TASKS = {"Artwork Scan": artwork_scan, "Refresh All Artwork": artwork_refresh_all, "RSS Sync": rss_sync, "Missing Album Search": missing_search, "Refresh Artists": refresh_all,
         "Library Scan": library_scan, "Import List Sync": import_list_sync, "Backup": backup,
         "Housekeeping": housekeeping, "Check Downloads": lambda: check_downloads(), "Cutoff Unmet Search": cutoff_search}


# ---------------- download monitoring ----------------
def check_downloads():
    rows = db.q("SELECT * FROM queue WHERE status NOT IN ('imported','failed','importBlocked')")
    for item in rows:
        c = db.decode(db.q1("SELECT * FROM download_clients WHERE id=?", (item["client_id"],)))
        if not c or not item["download_id"]:
            continue
        st = safe_status(c, item["download_id"])
        upd = {"updated": db.now()}
        for k in ("progress", "eta", "size"):
            if st.get(k) is not None:
                upd[k] = st[k]
        if st.get("download_id"):
            upd["download_id"] = st["download_id"]
        state = st.get("state")
        if state in ("queued", "downloading", "stalled"):
            upd["status"] = state
            upd["message"] = st.get("message") or ""
            db.update("queue", item["id"], upd)
            # stalled torrents / Soulseek transfers that never start get failed after 6h
            if state in ("stalled", "queued") and item["added"] and _age_h(item["added"]) > 6 and (item["progress"] or 0) == 0 \
                    and db.get_setting("failed_download_handling"):
                fail(item, c, f"No progress after 6 hours ({state})")
        elif state == "completed":
            upd["output_path"] = map_path(st.get("output_path"), c["id"])
            upd["status"] = "importing"
            db.update("queue", item["id"], upd)
            try:
                res = importer.import_album(item["album_id"], upd["output_path"], source_title=item["title"],
                                            mode="copy" if c["kind"] in ("qbittorrent", "transmission", "deluge") and
                                            db.get_setting("import_mode") == "move" else None)
                db.update("queue", item["id"], {"status": "imported", "progress": 100,
                                                "message": f"Imported {res['imported']}/{res['total']} tracks"})
                if c.get("remove_completed") and c["kind"] in ("sabnzbd", "nzbget", "slskd"):
                    try:
                        impl(c).remove(c, item["download_id"], delete_files=False)
                    except Exception:
                        pass
            except Exception as e:
                log.warning("Import of %s blocked: %s", item["title"], e)
                db.update("queue", item["id"], {"status": "importBlocked", "message": str(e)})
                notify.send("failure", "Import needs attention", f"{item['title']}\n{e}")
        elif state == "failed":
            if db.get_setting("failed_download_handling"):
                fail(item, c, st.get("message") or "Download failed")
            else:
                db.update("queue", item["id"], {"status": "failed", "message": st.get("message")})


def _age_h(iso):
    from datetime import datetime
    try:
        return (datetime.now(datetime.fromisoformat(iso).tzinfo) - datetime.fromisoformat(iso)).total_seconds() / 3600
    except Exception:
        return 0


def fail(item, c, reason, blocklist=True, research=None):
    db.update("queue", item["id"], {"status": "failed", "message": reason, "updated": db.now()})
    if blocklist:
        db.insert("blocklist", dict(album_id=item["album_id"], source_title=item["title"], protocol=item["protocol"],
                                    indexer=item["indexer"], reason=reason, date=db.now()))
    db.add_history("downloadFailed", item["album_id"], source_title=item["title"], quality=item["quality"],
                   protocol=item["protocol"], data={"reason": reason})
    log.warning("Download failed: %s (%s)", item["title"], reason)
    notify.send("failure", "Download failed", f"{item['title']}\n{reason}")
    try:
        if c:
            impl(c).remove(c, item["download_id"], delete_files=True)
    except Exception:
        pass
    if (research if research is not None else db.get_setting("redownload_failed")):
        enqueue("Album Search", album_search, [item["album_id"]])
