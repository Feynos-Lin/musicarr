"""Soulseek through slskd's REST API: search, folder matching against MusicBrainz tracklists, download, status."""
import os
import re
import time
import uuid

from . import db
from .quality import from_file, rank, worst
from .util import http, is_audio, log, similarity, tokens


def _h(c):
    return {"X-API-Key": c["api_key"]} if c.get("api_key") else {}


def _url(c, path):
    return c["url"].rstrip("/") + "/api/v0" + path


def _auth(c):
    """slskd supports API keys; fall back to username/password session token."""
    if c.get("api_key"):
        return _h(c)
    if c.get("username"):
        r = http().post(_url(c, "/session"), json={"username": c["username"], "password": c["password"]}, timeout=15)
        r.raise_for_status()
        return {"Authorization": "Bearer " + r.json()["token"]}
    return {}


def test(c):
    r = http().get(_url(c, "/application"), headers=_auth(c), timeout=15)
    r.raise_for_status()
    j = r.json()
    server = (j.get("server") or {})
    state = server.get("state", "unknown")
    return {"ok": "Connected" in state or "LoggedIn" in state,
            "message": f"slskd {j.get('version', {}).get('full', '')} — Soulseek server: {state}"}


def client():
    return db.q1("SELECT * FROM download_clients WHERE kind='slskd' AND enabled=1 ORDER BY priority")


def _clean_query(s):
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)          # drop (Deluxe Edition) etc for a wider net
    s = re.sub(r"[^\w\s']", " ", s)
    return " ".join(s.split())


def search(album, artist, tracks, timeout=None):
    c = client()
    if not c:
        return []
    timeout = timeout or int(db.get_setting("soulseek_search_timeout_sec") or 30)
    headers = _auth(c)
    queries = [f"{_clean_query(artist['name'])} {_clean_query(album['title'])}"]
    if len(tokens(album["title"])) >= 2:
        queries.append(_clean_query(album["title"]))
    responses = []
    for qtext in queries:
        sid = str(uuid.uuid4())
        try:
            r = http().post(_url(c, "/searches"), headers=headers, json={
                "id": sid, "searchText": qtext, "searchTimeout": timeout * 1000,
                "filterResponses": True, "responseLimit": 250, "fileLimit": 20000}, timeout=20)
            r.raise_for_status()
            deadline = time.time() + timeout + 15
            while time.time() < deadline:
                time.sleep(2)
                s = http().get(_url(c, f"/searches/{sid}"), headers=headers, timeout=15).json()
                if s.get("isComplete"):
                    break
            rr = http().get(_url(c, f"/searches/{sid}/responses"), headers=headers, timeout=30)
            responses.extend(rr.json() if rr.ok else [])
            http().delete(_url(c, f"/searches/{sid}"), headers=headers, timeout=15)
        except Exception as e:
            log.warning("Soulseek search '%s' failed: %s", qtext, e)
        if len(responses) > 40:
            break
    return folders_to_releases(responses, album, artist, tracks, c)


def folders_to_releases(responses, album, artist, tracks, c):
    expected = len(tracks) or album.get("track_count") or 0
    min_kbps = int(db.get_setting("soulseek_min_upload_speed_kbps") or 0)
    max_queue = int(db.get_setting("soulseek_max_queue") or 50)
    allow_partial = db.get_setting("allow_partial_soulseek")
    out = []
    for resp in responses:
        user = resp.get("username")
        speed_kbps = (resp.get("uploadSpeed") or 0) // 1024
        if min_kbps and speed_kbps < min_kbps:
            continue
        if (resp.get("queueLength") or 0) > max_queue:
            continue
        folders = {}
        for f in resp.get("files", []):
            fn = f.get("filename", "")
            if not is_audio(fn):
                continue
            d = fn.replace("/", "\\").rsplit("\\", 1)[0]
            folders.setdefault(d, []).append(f)
        for folder, files in folders.items():
            names = [os.path.splitext(f["filename"].replace("/", "\\").rsplit("\\", 1)[-1])[0] for f in files]
            matched = 0
            for t in tracks:
                if any(similarity(t["title"], n) >= 0.6 or (tokens(t["title"]) and all(x in tokens(n) for x in tokens(t["title"]))) for n in names):
                    matched += 1
            folder_sim = max(similarity(album["title"], folder.rsplit("\\", 1)[-1]), similarity(album["title"], folder))
            if expected:
                pct = matched / expected
                if pct < 0.5 and folder_sim < 0.6:
                    continue
                if not allow_partial and len(files) < expected:
                    continue
                if len(files) > expected * 2 + 4:
                    continue  # whole discography dumped in one folder
            elif folder_sim < 0.6:
                continue
            quals = [from_file(os.path.splitext(f["filename"])[1], f.get("bitRate"), f.get("bitDepth")) for f in files]
            q = worst(quals)
            size = sum(f.get("size", 0) for f in files)
            out.append({
                "title": f"{folder.rsplit(chr(92), 1)[-1]}  [{user}]", "guid": f"slsk://{user}/{folder}",
                "link": "", "size": size, "protocol": "soulseek", "indexer": "Soulseek", "indexer_id": None,
                "indexer_priority": 25, "seeders": None, "leechers": None, "age_days": 0, "quality": q,
                "username": user, "folder": folder, "files": [{"filename": f["filename"], "size": f.get("size", 0)} for f in files],
                "track_match": round(matched / expected * 100) if expected else None, "file_count": len(files),
                "free_slot": bool(resp.get("hasFreeUploadSlot")), "upload_speed": resp.get("uploadSpeed") or 0,
                "queue_length": resp.get("queueLength") or 0, "info_url": "",
            })
    out.sort(key=lambda r: (rank(r["quality"]), -(r["track_match"] or 0), not r["free_slot"], -r["upload_speed"]))
    return out


def grab(c, release):
    headers = _auth(c)
    r = http().post(_url(c, f"/transfers/downloads/{release['username']}"), headers=headers,
                    json=release["files"], timeout=30)
    r.raise_for_status()
    return f"{release['username']}|{release['folder']}"


def status(c, download_id):
    user, folder = download_id.split("|", 1)
    headers = _auth(c)
    r = http().get(_url(c, f"/transfers/downloads/{user}"), headers=headers, timeout=20)
    if r.status_code == 404:
        return {"state": "failed", "message": "Transfer no longer exists in slskd"}
    r.raise_for_status()
    files = []
    for d in r.json().get("directories", []):
        if d.get("directory", "").replace("/", "\\") == folder.replace("/", "\\"):
            files.extend(d.get("files", []))
    if not files:
        return {"state": "failed", "message": "Transfer no longer exists in slskd"}
    total = sum(f.get("size", 0) for f in files) or 1
    done = sum(f.get("bytesTransferred", 0) for f in files)
    states = [f.get("state", "") for f in files]
    failed = [s for s in states if any(x in s for x in ("Errored", "Rejected", "TimedOut", "Cancelled"))]
    complete = all("Succeeded" in s for s in states)
    out_dir = os.path.join(c.get("download_path") or "/downloads/soulseek", folder.replace("/", "\\").rsplit("\\", 1)[-1])
    st = {"progress": round(done / total * 100, 1), "size": total, "output_path": out_dir, "files": len(files)}
    if complete:
        st["state"] = "completed"
    elif failed and all(("Completed" in s) for s in states):
        st["state"] = "failed"
        st["message"] = f"{len(failed)} of {len(files)} files failed ({failed[0]})"
    elif any("Queued" in s for s in states) and done == 0:
        st["state"] = "queued"
    else:
        st["state"] = "downloading"
    return st


def remove(c, download_id, delete_files=False):
    user, folder = download_id.split("|", 1)
    headers = _auth(c)
    try:
        r = http().get(_url(c, f"/transfers/downloads/{user}"), headers=headers, timeout=20)
        for d in r.json().get("directories", []):
            if d.get("directory", "").replace("/", "\\") == folder.replace("/", "\\"):
                for f in d.get("files", []):
                    http().delete(_url(c, f"/transfers/downloads/{user}/{f['id']}"), params={"remove": "true"},
                                  headers=headers, timeout=15)
    except Exception as e:
        log.debug("slskd remove failed: %s", e)
