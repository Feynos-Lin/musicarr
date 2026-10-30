"""Download clients: qBittorrent, Transmission, Deluge, SABnzbd, NZBGet, Blackhole, plus slskd (Soulseek)."""
import base64
import hashlib
import os
import re
import time

import requests

from . import soulseek
from .util import http, log

KINDS = {
    "qbittorrent": "torrent", "transmission": "torrent", "deluge": "torrent", "torrent_blackhole": "torrent",
    "sabnzbd": "usenet", "nzbget": "usenet", "usenet_blackhole": "usenet", "slskd": "soulseek",
}


def _base(c):
    return c["url"].rstrip("/")


def _fetch_file(link):
    r = http().get(link, timeout=60, allow_redirects=False)
    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location", "").startswith("magnet:"):
        return None, r.headers["Location"]
    if r.is_redirect:
        r = http().get(r.headers["Location"], timeout=60)
    r.raise_for_status()
    return r.content, None


def _infohash_from_magnet(m):
    x = re.search(r"btih:([a-zA-Z0-9]+)", m or "")
    if not x:
        return None
    h = x.group(1)
    if len(h) == 32:
        h = base64.b32decode(h.upper()).hex()
    return h.lower()


def _infohash_from_torrent(data):
    try:
        i = data.index(b"4:info") + 6
        # walk the bencoded dict to find its end
        def walk(p):
            c = data[p:p + 1]
            if c == b"d" or c == b"l":
                p += 1
                while data[p:p + 1] != b"e":
                    p = walk(p)
                return p + 1
            if c == b"i":
                return data.index(b"e", p) + 1
            colon = data.index(b":", p)
            return colon + 1 + int(data[p:colon])
        end = walk(i)
        return hashlib.sha1(data[i:end]).hexdigest()
    except Exception:
        return None


# ---------------- qBittorrent ----------------
class QBit:
    _sessions = {}

    @classmethod
    def s(cls, c):
        key = c["id"]
        sess = cls._sessions.get(key)
        if sess is None or time.time() - sess[1] > 1800:
            se = requests.Session()
            se.headers["Referer"] = _base(c)
            r = se.post(_base(c) + "/api/v2/auth/login", data={"username": c["username"], "password": c["password"]}, timeout=15)
            if r.text.strip() not in ("Ok.", ""):
                raise RuntimeError(f"qBittorrent login failed: {r.text}")
            sess = (se, time.time())
            cls._sessions[key] = sess
        return sess[0]

    @classmethod
    def test(cls, c):
        v = cls.s(c).get(_base(c) + "/api/v2/app/version", timeout=10).text
        return {"ok": True, "message": f"qBittorrent {v}"}

    @classmethod
    def grab(cls, c, rel, tag):
        se = cls.s(c)
        data = {"category": c.get("category") or "music", "tags": f"musicarr,{tag}"}
        content, magnet = (None, rel["link"]) if rel["link"].startswith("magnet:") else _fetch_file(rel["link"])
        if content:
            r = se.post(_base(c) + "/api/v2/torrents/add", data=data, files={"torrents": ("r.torrent", content)}, timeout=30)
            h = _infohash_from_torrent(content)
        else:
            data["urls"] = magnet
            r = se.post(_base(c) + "/api/v2/torrents/add", data=data, timeout=30)
            h = _infohash_from_magnet(magnet)
        if "Fails" in r.text:
            raise RuntimeError("qBittorrent rejected the torrent")
        return h or f"tag:{tag}"

    @classmethod
    def _find(cls, c, did):
        se = cls.s(c)
        if did.startswith("tag:"):
            items = se.get(_base(c) + "/api/v2/torrents/info", params={"tag": did[4:]}, timeout=15).json()
        else:
            items = se.get(_base(c) + "/api/v2/torrents/info", params={"hashes": did}, timeout=15).json()
        return items[0] if items else None

    @classmethod
    def status(cls, c, did):
        t = cls._find(c, did)
        if not t:
            return {"state": "failed", "message": "Torrent removed from qBittorrent"}
        st = t["state"]
        out = {"progress": round(t["progress"] * 100, 1), "size": t["size"], "eta": t.get("eta"),
               "output_path": t.get("content_path") or os.path.join(t["save_path"], t["name"]), "download_id": t["hash"]}
        if st in ("error", "missingFiles"):
            out["state"] = "failed"
            out["message"] = st
        elif t["progress"] >= 1 or st in ("uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP", "checkingUP"):
            out["state"] = "completed"
        elif st in ("metaDL", "queuedDL", "pausedDL", "stoppedDL"):
            out["state"] = "queued"
        elif st == "stalledDL":
            out["state"] = "stalled"
        else:
            out["state"] = "downloading"
        return out

    @classmethod
    def remove(cls, c, did, delete_files=False):
        t = cls._find(c, did)
        if t:
            cls.s(c).post(_base(c) + "/api/v2/torrents/delete",
                          data={"hashes": t["hash"], "deleteFiles": str(delete_files).lower()}, timeout=15)


# ---------------- Transmission ----------------
class Transmission:
    _sid = {}

    @classmethod
    def rpc(cls, c, method, args=None):
        url = _base(c)
        if not url.endswith("/rpc"):
            url += "/transmission/rpc"
        auth = (c["username"], c["password"]) if c.get("username") else None
        for _ in range(2):
            r = http().post(url, json={"method": method, "arguments": args or {}}, auth=auth,
                            headers={"X-Transmission-Session-Id": cls._sid.get(c["id"], "")}, timeout=20)
            if r.status_code == 409:
                cls._sid[c["id"]] = r.headers.get("X-Transmission-Session-Id", "")
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("result") != "success":
                raise RuntimeError(j.get("result"))
            return j["arguments"]
        raise RuntimeError("Transmission session negotiation failed")

    @classmethod
    def test(cls, c):
        a = cls.rpc(c, "session-get")
        return {"ok": True, "message": f"Transmission {a.get('version')}"}

    @classmethod
    def grab(cls, c, rel, tag):
        args = {"labels": ["musicarr", c.get("category") or "music"]}
        if c.get("download_path"):
            args["download-dir"] = c["download_path"]
        if rel["link"].startswith("magnet:"):
            args["filename"] = rel["link"]
        else:
            content, magnet = _fetch_file(rel["link"])
            if content:
                args["metainfo"] = base64.b64encode(content).decode()
            else:
                args["filename"] = magnet
        a = cls.rpc(c, "torrent-add", args)
        t = a.get("torrent-added") or a.get("torrent-duplicate")
        return t["hashString"]

    @classmethod
    def status(cls, c, did):
        a = cls.rpc(c, "torrent-get", {"ids": [did], "fields": ["hashString", "name", "percentDone", "status", "error",
                                                                "errorString", "totalSize", "eta", "downloadDir"]})
        if not a["torrents"]:
            return {"state": "failed", "message": "Torrent removed from Transmission"}
        t = a["torrents"][0]
        out = {"progress": round(t["percentDone"] * 100, 1), "size": t["totalSize"], "eta": t.get("eta"),
               "output_path": os.path.join(t["downloadDir"], t["name"])}
        if t.get("error"):
            out.update(state="failed", message=t.get("errorString"))
        elif t["percentDone"] >= 1:
            out["state"] = "completed"
        elif t["status"] in (0, 3):
            out["state"] = "queued"
        else:
            out["state"] = "downloading"
        return out

    @classmethod
    def remove(cls, c, did, delete_files=False):
        cls.rpc(c, "torrent-remove", {"ids": [did], "delete-local-data": delete_files})


# ---------------- Deluge (Web UI JSON-RPC) ----------------
class Deluge:
    _s = {}

    @classmethod
    def call(cls, c, method, *params):
        se = cls._s.get(c["id"])
        url = _base(c) + "/json"
        if se is None:
            se = requests.Session()
            r = se.post(url, json={"method": "auth.login", "params": [c["password"]], "id": 1}, timeout=15).json()
            if not r.get("result"):
                raise RuntimeError("Deluge login failed")
            cls._s[c["id"]] = se
        r = se.post(url, json={"method": method, "params": list(params), "id": 2}, timeout=20).json()
        if r.get("error"):
            cls._s.pop(c["id"], None)
            raise RuntimeError(r["error"].get("message"))
        return r["result"]

    @classmethod
    def test(cls, c):
        cls.call(c, "web.connected")
        return {"ok": True, "message": "Connected to Deluge"}

    @classmethod
    def grab(cls, c, rel, tag):
        opts = {"download_location": c["download_path"]} if c.get("download_path") else {}
        if rel["link"].startswith("magnet:"):
            h = cls.call(c, "core.add_torrent_magnet", rel["link"], opts)
        else:
            content, magnet = _fetch_file(rel["link"])
            if content:
                h = cls.call(c, "core.add_torrent_file", "musicarr.torrent", base64.b64encode(content).decode(), opts)
            else:
                h = cls.call(c, "core.add_torrent_magnet", magnet, opts)
        try:
            cls.call(c, "label.set_torrent", h, c.get("category") or "music")
        except Exception:
            pass
        return h

    @classmethod
    def status(cls, c, did):
        t = cls.call(c, "core.get_torrent_status", did, ["name", "progress", "state", "total_size", "eta", "save_path"])
        if not t:
            return {"state": "failed", "message": "Torrent removed from Deluge"}
        out = {"progress": round(t["progress"], 1), "size": t["total_size"], "eta": t.get("eta"),
               "output_path": os.path.join(t["save_path"], t["name"])}
        out["state"] = {"Seeding": "completed", "Error": "failed", "Queued": "queued", "Paused": "queued"}.get(t["state"], "downloading")
        if t["progress"] >= 100:
            out["state"] = "completed"
        return out

    @classmethod
    def remove(cls, c, did, delete_files=False):
        cls.call(c, "core.remove_torrent", did, delete_files)


# ---------------- SABnzbd ----------------
class Sab:
    @classmethod
    def api(cls, c, **p):
        p.update(apikey=c["api_key"], output="json")
        url = _base(c)
        if not url.endswith("/api"):
            url += "/api"
        r = http().get(url, params=p, timeout=30)
        r.raise_for_status()
        return r.json()

    @classmethod
    def test(cls, c):
        v = cls.api(c, mode="version")
        cls.api(c, mode="queue", limit=1)
        return {"ok": True, "message": f"SABnzbd {v.get('version')}"}

    @classmethod
    def grab(cls, c, rel, tag):
        content, _ = _fetch_file(rel["link"])
        url = _base(c) + ("" if _base(c).endswith("/api") else "/api")
        r = http().post(url, params={"mode": "addfile", "apikey": c["api_key"], "output": "json",
                                     "cat": c.get("category") or "music", "nzbname": rel["title"]},
                        files={"name": (re.sub(r"[^\w.\- ]", "_", rel["title"]) + ".nzb", content)}, timeout=60)
        j = r.json()
        if not j.get("status"):
            raise RuntimeError(j.get("error", "SABnzbd refused NZB"))
        return j["nzo_ids"][0]

    @classmethod
    def status(cls, c, did):
        q = cls.api(c, mode="queue", nzo_ids=did)
        for s in q.get("queue", {}).get("slots", []):
            if s["nzo_id"] == did:
                return {"state": "queued" if s["status"] in ("Queued", "Paused") else "downloading",
                        "progress": float(s.get("percentage", 0)), "size": int(float(s.get("mb", 0)) * 1048576),
                        "eta": None}
        h = cls.api(c, mode="history", nzo_ids=did, limit=50)
        for s in h.get("history", {}).get("slots", []):
            if s["nzo_id"] == did:
                if s["status"] == "Completed":
                    return {"state": "completed", "progress": 100, "size": s.get("bytes"), "output_path": s.get("storage")}
                if s["status"] == "Failed":
                    return {"state": "failed", "message": s.get("fail_message") or "Failed in SABnzbd"}
                return {"state": "downloading", "progress": 99, "message": s["status"]}  # extracting / verifying
        return {"state": "failed", "message": "Not found in SABnzbd"}

    @classmethod
    def remove(cls, c, did, delete_files=False):
        cls.api(c, mode="queue", name="delete", value=did, del_files=int(delete_files))
        cls.api(c, mode="history", name="delete", value=did, del_files=int(delete_files))


# ---------------- NZBGet ----------------
class NzbGet:
    @classmethod
    def rpc(cls, c, method, *params):
        url = _base(c) + "/jsonrpc"
        r = http().post(url, json={"method": method, "params": list(params)},
                        auth=(c["username"], c["password"]) if c.get("username") else None, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            raise RuntimeError(j["error"])
        return j["result"]

    @classmethod
    def test(cls, c):
        return {"ok": True, "message": f"NZBGet {cls.rpc(c, 'version')}"}

    @classmethod
    def grab(cls, c, rel, tag):
        content, _ = _fetch_file(rel["link"])
        nzb_id = cls.rpc(c, "append", rel["title"] + ".nzb", base64.b64encode(content).decode(),
                         c.get("category") or "music", 0, False, False, "", 0, "SCORE", [["musicarr", tag]])
        if nzb_id <= 0:
            raise RuntimeError("NZBGet rejected NZB")
        return str(nzb_id)

    @classmethod
    def status(cls, c, did):
        for g in cls.rpc(c, "listgroups", 0):
            if str(g["NZBID"]) == did:
                total = g["FileSizeMB"] or 1
                return {"state": "queued" if g["Status"] in ("QUEUED", "PAUSED") else "downloading",
                        "progress": round((total - g["RemainingSizeMB"]) / total * 100, 1),
                        "size": g["FileSizeMB"] * 1048576}
        for h in cls.rpc(c, "history", False):
            if str(h["NZBID"]) == did:
                if h["Status"].startswith("SUCCESS"):
                    return {"state": "completed", "progress": 100, "output_path": h.get("DestDir"),
                            "size": h["FileSizeMB"] * 1048576}
                return {"state": "failed", "message": h["Status"]}
        return {"state": "failed", "message": "Not found in NZBGet"}

    @classmethod
    def remove(cls, c, did, delete_files=False):
        try:
            cls.rpc(c, "editqueue", "GroupFinalDelete" if delete_files else "GroupDelete", "", [int(did)])
            cls.rpc(c, "editqueue", "HistoryFinalDelete" if delete_files else "HistoryDelete", "", [int(did)])
        except Exception:
            pass


# ---------------- Blackhole (watch folder) ----------------
class Blackhole:
    """Drops .torrent/.nzb into a watch folder. Completed downloads are picked up from the 'completed' folder
    set in extra.completed_path, matched by name."""

    @classmethod
    def test(cls, c):
        if not os.path.isdir(c["download_path"]):
            raise RuntimeError(f"Watch folder {c['download_path']} not found")
        return {"ok": True, "message": "Watch folder is reachable"}

    @classmethod
    def grab(cls, c, rel, tag):
        name = re.sub(r"[^\w.\- ()\[\]]", "_", rel["title"])
        if rel["link"].startswith("magnet:"):
            path = os.path.join(c["download_path"], name + ".magnet")
            with open(path, "w") as f:
                f.write(rel["link"])
        else:
            content, magnet = _fetch_file(rel["link"])
            ext = ".nzb" if rel["protocol"] == "usenet" else ".torrent"
            path = os.path.join(c["download_path"], name + ext)
            with open(path, "wb") as f:
                f.write(content or magnet.encode())
        return name

    @classmethod
    def status(cls, c, did):
        done = (c.get("extra") or {}).get("completed_path") if isinstance(c.get("extra"), dict) else None
        if done and os.path.exists(os.path.join(done, did)):
            return {"state": "completed", "progress": 100, "output_path": os.path.join(done, did)}
        return {"state": "downloading", "progress": 0}

    @classmethod
    def remove(cls, c, did, delete_files=False):
        pass


class Slskd:
    test = staticmethod(soulseek.test)
    grab = staticmethod(lambda c, rel, tag: soulseek.grab(c, rel))
    status = staticmethod(soulseek.status)
    remove = staticmethod(soulseek.remove)


IMPL = {"qbittorrent": QBit, "transmission": Transmission, "deluge": Deluge, "sabnzbd": Sab, "nzbget": NzbGet,
        "torrent_blackhole": Blackhole, "usenet_blackhole": Blackhole, "slskd": Slskd}


def impl(c):
    return IMPL[c["kind"]]


def safe_status(c, did):
    try:
        return impl(c).status(c, did)
    except Exception as e:
        log.warning("Status check on %s failed: %s", c["name"], e)
        return {"state": "unknown", "message": str(e)}
