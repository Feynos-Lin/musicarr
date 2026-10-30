"""Manage the slskd daemon from Musicarr: Soulseek account, sharing, folders, limits and connection.

slskd must run with SLSKD_REMOTE_CONFIGURATION=true. Musicarr signs in with slskd's web login (an admin
session), reads slskd.yml, merges the settings from the GUI and writes it back. Values set through
SLSKD_* environment variables override the file, so leave those out of compose for anything managed here.
"""
import os

import yaml

from . import db
from .util import http, log

ENV_URL = os.environ.get("MUSICARR_SLSKD_URL", "")
ENV_USER = os.environ.get("MUSICARR_SLSKD_USERNAME", "slskd")
ENV_PASS = os.environ.get("MUSICARR_SLSKD_PASSWORD", "slskd")
DEFAULT_DOWNLOADS = os.environ.get("MUSICARR_SLSKD_DOWNLOADS", "/downloads/soulseek")


def client():
    return db.decode(db.q1("SELECT * FROM download_clients WHERE kind='slskd' ORDER BY enabled DESC, priority LIMIT 1"))


def ensure_client():
    """On first start with MUSICARR_SLSKD_URL set, create the slskd download client automatically."""
    if not ENV_URL or client():
        return
    db.insert("download_clients", dict(name="Soulseek", kind="slskd", protocol="soulseek", url=ENV_URL,
                                       username=ENV_USER, password=ENV_PASS, download_path=DEFAULT_DOWNLOADS,
                                       enabled=1, priority=1, remove_completed=1))
    log.info("Added slskd at %s as the Soulseek download client", ENV_URL)


def _url(c, path):
    return c["url"].rstrip("/") + "/api/v0" + path


def _headers(c, admin=False):
    """Admin actions need a session from slskd's web login; plain transfers can use an API key."""
    if c.get("username") and c.get("password"):
        r = http().post(_url(c, "/session"), json={"username": c["username"], "password": c["password"]}, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("slskd rejected the web login — check the slskd username/password")
        r.raise_for_status()
        return {"Authorization": "Bearer " + r.json()["token"]}
    if admin:
        raise RuntimeError("Enter slskd's web username and password so Musicarr can manage its settings")
    return {"X-API-Key": c["api_key"]} if c.get("api_key") else {}


def _req(c, method, path, admin=True, **kw):
    r = http().request(method, _url(c, path), headers=_headers(c, admin), timeout=20, **kw)
    if r.status_code == 403 and "options" in path:
        raise RuntimeError("slskd won't accept config changes. Add SLSKD_REMOTE_CONFIGURATION=true to slskd's environment and restart it.")
    if r.status_code >= 400:
        raise RuntimeError(f"slskd {method} {path} failed ({r.status_code}): {r.text[:200]}")
    return r


def _get(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict) or k not in d or d[k] is None:
            return default
        d = d[k]
    return d


def _set(d, keys, value):
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    if value in (None, ""):
        d.pop(keys[-1], None)
    else:
        d[keys[-1]] = value


def _prune(d):
    """Drop empty mappings left behind after clearing values."""
    if isinstance(d, dict):
        out = {k: _prune(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v != {}}
    return d


# GUI field -> path in slskd.yml
FIELDS = {
    "soulseek_username": ("soulseek", "username"),
    "soulseek_password": ("soulseek", "password"),
    "listen_port": ("soulseek", "listen_port"),
    "description": ("soulseek", "description"),
    "downloads_dir": ("directories", "downloads"),
    "incomplete_dir": ("directories", "incomplete"),
    "upload_slots": ("global", "upload", "slots"),
    "upload_speed_limit": ("global", "upload", "speed_limit"),
    "download_slots": ("global", "download", "slots"),
    "download_speed_limit": ("global", "download", "speed_limit"),
}


def read_yaml(c):
    text = _req(c, "GET", "/options/yaml").text
    # slskd returns the file as a JSON string
    if text.startswith('"'):
        import json
        text = json.loads(text)
    return yaml.safe_load(text) or {}, text


def status():
    c = client()
    if not c:
        return {"configured": False}
    out = {"configured": True, "client": {k: c.get(k) for k in ("id", "name", "url", "username", "download_path", "enabled")}}
    out["client"]["has_password"] = bool(c.get("password"))
    try:
        app = _req(c, "GET", "/application", admin=False).json()
        server = app.get("server") or {}
        out.update(reachable=True, version=(app.get("version") or {}).get("full", ""),
                   state=server.get("state", "Unknown"), connected="Connected" in server.get("state", ""),
                   logged_in="LoggedIn" in server.get("state", ""),
                   shares=(app.get("shares") or {}).get("files"))
    except Exception as e:
        out.update(reachable=False, error=str(e))
        return out
    try:
        conf, _ = read_yaml(c)
        out["settings"] = {k: _get(conf, *p) for k, p in FIELDS.items() if k != "soulseek_password"}
        out["settings"]["has_soulseek_password"] = bool(_get(conf, "soulseek", "password"))
        dirs = _get(conf, "shares", "directories", default=[]) or []
        out["settings"]["share_dirs"] = "\n".join(dirs) if dirs else None
        out["remote_config"] = True
    except Exception as e:
        out["remote_config"] = False
        out["config_error"] = str(e)
    return out


def save(data):
    c = client()
    if not c:
        raise RuntimeError("Connect Musicarr to slskd first")
    conf, _ = read_yaml(c)
    before = (_get(conf, "soulseek", "username"), _get(conf, "soulseek", "password"), _get(conf, "soulseek", "listen_port"))
    for k, path in FIELDS.items():
        if k not in data:
            continue
        v = data[k]
        if k == "soulseek_password" and not v:
            continue  # blank = keep the current password
        if k in ("listen_port", "upload_slots", "download_slots", "upload_speed_limit", "download_speed_limit"):
            v = int(v) if str(v).strip() not in ("", "None") else None
        _set(conf, list(path), v)
    if "share_dirs" in data:
        dirs = [d.strip() for d in str(data["share_dirs"] or "").splitlines() if d.strip()]
        _set(conf, ["shares", "directories"], dirs or None)
    text = yaml.safe_dump(_prune(conf) or {}, sort_keys=False, allow_unicode=True)
    v = _req(c, "POST", "/options/yaml/validate", json=text)
    if v.text.strip() not in ("", '""', "null"):
        raise RuntimeError("slskd rejected the settings: " + v.text[:300])
    _req(c, "POST", "/options/yaml", json=text)
    # keep Musicarr's import path in step with slskd's download folder
    if data.get("downloads_dir"):
        db.update("download_clients", c["id"], {"download_path": data["downloads_dir"]})
    after = (_get(conf, "soulseek", "username"), _get(conf, "soulseek", "password"), _get(conf, "soulseek", "listen_port"))
    reconnected = False
    if before != after and after[0] and after[1]:
        try:
            reconnect()
            reconnected = True
        except Exception as e:
            log.warning("Reconnect after Soulseek settings change failed: %s", e)
    log.info("Updated slskd settings%s", " and reconnected" if reconnected else "")
    return {"ok": True, "reconnected": reconnected}


def connect():
    c = client()
    _req(c, "PUT", "/server")
    return {"ok": True}


def disconnect():
    c = client()
    _req(c, "DELETE", "/server", json="Disconnected by Musicarr")
    return {"ok": True}


def reconnect():
    try:
        disconnect()
    except Exception:
        pass
    import time
    time.sleep(1.5)
    return connect()


def save_connection(data):
    """Create or update the slskd connection (URL + web login + where its downloads appear)."""
    c = client()
    row = {"url": data["url"].rstrip("/"), "username": data.get("username") or "",
           "download_path": data.get("download_path") or DEFAULT_DOWNLOADS, "enabled": 1}
    if data.get("password"):
        row["password"] = data["password"]
    if c:
        db.update("download_clients", c["id"], row)
    else:
        row.update(name="Soulseek", kind="slskd", protocol="soulseek", priority=1, remove_completed=1,
                   password=data.get("password") or "")
        db.insert("download_clients", row)
    return status()
