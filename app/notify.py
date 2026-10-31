"""Notifications and media-server connections: Discord, ntfy, Gotify, Pushover, Telegram, Webhook,
Navidrome/Subsonic, Jellyfin, Plex."""
import hashlib
import json
import secrets
import threading

from . import db
from .util import http, log

EVENT_FIELD = {"grab": "on_grab", "import": "on_import", "upgrade": "on_upgrade", "failure": "on_failure", "health": "on_health"}
KINDS = ["discord", "ntfy", "gotify", "pushover", "telegram", "webhook", "navidrome", "jellyfin", "plex"]
LIBRARY_KINDS = {"navidrome", "jellyfin", "plex"}


def _cover(album):
    return f"https://coverartarchive.org/release-group/{album['mbid']}/front-250" if album and album.get("mbid") else None


def deliver(n, event, title, body, album=None, artist=None):
    cfg = n["config"] if isinstance(n["config"], dict) else json.loads(n["config"] or "{}")
    k = n["kind"]
    s = http()
    if k == "discord":
        embed = {"title": title, "description": body, "color": 0x1DB954 if event != "failure" else 0xE74C3C}
        if _cover(album):
            embed["thumbnail"] = {"url": _cover(album)}
        s.post(cfg["webhook_url"], json={"username": "Musicarr", "embeds": [embed]}, timeout=15).raise_for_status()
    elif k == "ntfy":
        url = cfg.get("server", "https://ntfy.sh").rstrip("/") + "/" + cfg["topic"]
        h = {"Title": title.encode("utf-8"), "Tags": "musical_note"}
        if cfg.get("token"):
            h["Authorization"] = "Bearer " + cfg["token"]
        s.post(url, data=body.encode(), headers=h, timeout=15).raise_for_status()
    elif k == "gotify":
        s.post(cfg["server"].rstrip("/") + "/message", params={"token": cfg["token"]},
               json={"title": title, "message": body, "priority": int(cfg.get("priority", 5))}, timeout=15).raise_for_status()
    elif k == "pushover":
        s.post("https://api.pushover.net/1/messages.json", data={"token": cfg["app_token"], "user": cfg["user_key"],
                                                                "title": title, "message": body}, timeout=15).raise_for_status()
    elif k == "telegram":
        s.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
               json={"chat_id": cfg["chat_id"], "text": f"*{title}*\n{body}", "parse_mode": "Markdown"}, timeout=15).raise_for_status()
    elif k == "webhook":
        payload = {"eventType": event, "title": title, "message": body,
                   "artist": {k2: artist.get(k2) for k2 in ("id", "name", "mbid", "path")} if artist else None,
                   "album": {k2: album.get(k2) for k2 in ("id", "title", "mbid", "release_date")} if album else None}
        s.request(cfg.get("method", "POST"), cfg["url"], json=payload, timeout=15).raise_for_status()
    elif k == "navidrome":
        if event in ("import", "upgrade", "artwork", "test"):
            salt = secrets.token_hex(6)
            tok = hashlib.md5((cfg["password"] + salt).encode()).hexdigest()
            r = s.get(cfg["url"].rstrip("/") + "/rest/startScan", params={
                "u": cfg["username"], "t": tok, "s": salt, "v": "1.16.1", "c": "musicarr", "f": "json",
                "fullScan": "false"}, timeout=15)
            r.raise_for_status()
            st = r.json().get("subsonic-response", {})
            if st.get("status") != "ok":
                raise RuntimeError(st.get("error", {}).get("message", "Navidrome scan failed"))
    elif k == "jellyfin":
        if event in ("import", "upgrade", "artwork", "test"):
            s.post(cfg["url"].rstrip("/") + "/Library/Refresh", headers={"X-Emby-Token": cfg["api_key"]}, timeout=15).raise_for_status()
    elif k == "plex":
        if event in ("import", "upgrade", "artwork", "test"):
            base = cfg["url"].rstrip("/")
            sec = cfg.get("section_id")
            path = f"/library/sections/{sec}/refresh" if sec else "/library/sections/all/refresh"
            s.get(base + path, params={"X-Plex-Token": cfg["token"]}, timeout=15).raise_for_status()


def send(event, title, body, album=None, artist=None):
    field = EVENT_FIELD.get(event)
    rows = db.q("SELECT * FROM notifications WHERE enabled=1")

    def run():
        for n in rows:
            if field and not n.get(field):
                continue
            if n["kind"] in LIBRARY_KINDS and event not in ("import", "upgrade", "artwork"):
                continue
            if event == "artwork" and n["kind"] not in LIBRARY_KINDS:
                continue
            try:
                deliver(n, event, title, body, album, artist)
            except Exception as e:
                log.warning("Notification '%s' failed: %s", n["name"], e)

    threading.Thread(target=run, daemon=True).start()


def test(n):
    deliver(n, "test", "Musicarr test", "If you can read this, notifications work.")
    return {"ok": True, "message": "Sent"}
