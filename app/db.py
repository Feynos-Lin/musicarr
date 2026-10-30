"""SQLite storage for Musicarr. Single connection guarded by a lock; small and fast enough for a home library."""
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

CONFIG_DIR = os.environ.get("MUSICARR_CONFIG", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "musicarr.db")

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS root_folders (id INTEGER PRIMARY KEY, path TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS quality_profiles (
  id INTEGER PRIMARY KEY, name TEXT, allowed TEXT, cutoff TEXT, upgrade_allowed INTEGER DEFAULT 1,
  preferred_words TEXT DEFAULT '', required_words TEXT DEFAULT '', ignored_words TEXT DEFAULT '',
  min_seeders INTEGER DEFAULT 1, protocol_order TEXT DEFAULT '["usenet","torrent","soulseek"]');
CREATE TABLE IF NOT EXISTS metadata_profiles (
  id INTEGER PRIMARY KEY, name TEXT, primary_types TEXT, secondary_types TEXT, release_statuses TEXT);
CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, label TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS artists (
  id INTEGER PRIMARY KEY, mbid TEXT UNIQUE, name TEXT, sort_name TEXT, disambiguation TEXT, artist_type TEXT,
  country TEXT, overview TEXT, genres TEXT DEFAULT '[]', image TEXT, monitored INTEGER DEFAULT 1,
  monitor_new TEXT DEFAULT 'all', quality_profile_id INTEGER, metadata_profile_id INTEGER, root_folder TEXT,
  path TEXT, tags TEXT DEFAULT '[]', added TEXT, last_refresh TEXT, links TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS albums (
  id INTEGER PRIMARY KEY, mbid TEXT UNIQUE, artist_id INTEGER, title TEXT, album_type TEXT, secondary_types TEXT DEFAULT '[]',
  release_date TEXT, monitored INTEGER DEFAULT 1, cover TEXT, release_mbid TEXT, track_count INTEGER DEFAULT 0,
  any_release_ok INTEGER DEFAULT 1, last_search TEXT, added TEXT, path TEXT, genres TEXT DEFAULT '[]',
  FOREIGN KEY(artist_id) REFERENCES artists(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY, album_id INTEGER, mbid TEXT, recording_mbid TEXT, title TEXT, disc INTEGER, position INTEGER,
  length_ms INTEGER, artist_credit TEXT, file_path TEXT, quality TEXT, file_size INTEGER, bitrate INTEGER,
  FOREIGN KEY(album_id) REFERENCES albums(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS indexers (
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT, url TEXT, api_key TEXT, categories TEXT DEFAULT '3000',
  enabled INTEGER DEFAULT 1, rss INTEGER DEFAULT 1, search INTEGER DEFAULT 1, priority INTEGER DEFAULT 25,
  min_seeders INTEGER DEFAULT 1, tags TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS download_clients (
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT, protocol TEXT, url TEXT, username TEXT DEFAULT '', password TEXT DEFAULT '',
  api_key TEXT DEFAULT '', category TEXT DEFAULT 'music', enabled INTEGER DEFAULT 1, priority INTEGER DEFAULT 1,
  download_path TEXT DEFAULT '', remove_completed INTEGER DEFAULT 1, extra TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS queue (
  id INTEGER PRIMARY KEY, album_id INTEGER, client_id INTEGER, download_id TEXT, title TEXT, protocol TEXT,
  indexer TEXT, quality TEXT, size INTEGER, status TEXT, progress REAL DEFAULT 0, eta INTEGER, output_path TEXT,
  message TEXT, added TEXT, updated TEXT, payload TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY, album_id INTEGER, artist_id INTEGER, event TEXT, source_title TEXT, quality TEXT,
  protocol TEXT, data TEXT DEFAULT '{}', date TEXT);
CREATE TABLE IF NOT EXISTS blocklist (
  id INTEGER PRIMARY KEY, album_id INTEGER, source_title TEXT, protocol TEXT, indexer TEXT, reason TEXT, date TEXT);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT, config TEXT DEFAULT '{}', on_grab INTEGER DEFAULT 1,
  on_import INTEGER DEFAULT 1, on_upgrade INTEGER DEFAULT 1, on_failure INTEGER DEFAULT 1,
  on_health INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS import_lists (
  id INTEGER PRIMARY KEY, name TEXT, kind TEXT, config TEXT DEFAULT '{}', enabled INTEGER DEFAULT 1,
  quality_profile_id INTEGER, metadata_profile_id INTEGER, root_folder TEXT, monitor TEXT DEFAULT 'all',
  search_on_add INTEGER DEFAULT 1, last_sync TEXT);
CREATE TABLE IF NOT EXISTS import_exclusions (id INTEGER PRIMARY KEY, mbid TEXT UNIQUE, name TEXT);
CREATE TABLE IF NOT EXISTS remote_path_mappings (id INTEGER PRIMARY KEY, client_id INTEGER, remote_path TEXT, local_path TEXT);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_history_album ON history(album_id);
"""

DEFAULT_SETTINGS = {
    "naming_enabled": True,
    "artist_folder_format": "{Album Artist}",
    "album_folder_format": "{Album Title} ({Release Year})",
    "track_format": "{track:00} - {Track Title}",
    "multi_disc_track_format": "{medium:0}-{track:00} - {Track Title}",
    "replace_illegal": True,
    "import_mode": "move",          # move | copy | hardlink
    "write_tags": True,
    "embed_cover": True,
    "save_cover_jpg": True,
    "delete_empty_folders": True,
    "recycle_bin": "",
    "rss_interval_min": 15,
    "missing_search_interval_hours": 24,
    "refresh_interval_hours": 12,
    "download_check_interval_sec": 60,
    "library_scan_interval_hours": 24,
    "search_on_add": True,
    "min_track_match_pct": 80,
    "allow_partial_soulseek": False,
    "soulseek_search_timeout_sec": 30,
    "soulseek_min_upload_speed_kbps": 0,
    "soulseek_max_queue": 50,
    "prefer_free_slot": True,
    "failed_download_handling": True,
    "redownload_failed": True,
    "auth_username": "",
    "auth_password": "",
    "lastfm_api_key": "",
    "fanart_api_key": "",
    "ui_page_size": 50,
    "unmonitor_deleted": False,
}

QUALITIES = [  # best first
    "FLAC 24bit", "FLAC", "ALAC", "WAV", "APE", "WavPack",
    "MP3-320", "MP3-V0", "AAC-320", "AAC-256", "OGG Vorbis Q10", "Opus",
    "MP3-256", "MP3-V2", "MP3-224", "MP3-192", "OGG Vorbis", "AAC-192",
    "MP3-160", "MP3-128", "AAC-128", "MP3-96", "Unknown",
]


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def conn():
    global _conn
    if _conn is None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init():
    with _lock:
        c = conn()
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('api_key',?)", (json.dumps(secrets.token_hex(16)),))
        if not c.execute("SELECT 1 FROM quality_profiles").fetchone():
            lossless = ["FLAC 24bit", "FLAC", "ALAC", "WAV", "APE", "WavPack"]
            hq = ["MP3-320", "MP3-V0", "AAC-320", "AAC-256", "OGG Vorbis Q10", "Opus"]
            c.execute("INSERT INTO quality_profiles(name,allowed,cutoff) VALUES(?,?,?)",
                      ("Lossless", json.dumps(lossless), "FLAC"))
            c.execute("INSERT INTO quality_profiles(name,allowed,cutoff) VALUES(?,?,?)",
                      ("Lossless preferred", json.dumps(lossless + hq), "FLAC"))
            c.execute("INSERT INTO quality_profiles(name,allowed,cutoff) VALUES(?,?,?)",
                      ("High quality lossy", json.dumps(hq), "MP3-320"))
            c.execute("INSERT INTO quality_profiles(name,allowed,cutoff,upgrade_allowed) VALUES(?,?,?,0)",
                      ("Any", json.dumps(QUALITIES), "Unknown"))
        if not c.execute("SELECT 1 FROM metadata_profiles").fetchone():
            c.execute("INSERT INTO metadata_profiles(name,primary_types,secondary_types,release_statuses) VALUES(?,?,?,?)",
                      ("Standard", json.dumps(["Album", "EP"]), json.dumps([]), json.dumps(["Official"])))
            c.execute("INSERT INTO metadata_profiles(name,primary_types,secondary_types,release_statuses) VALUES(?,?,?,?)",
                      ("Albums only", json.dumps(["Album"]), json.dumps([]), json.dumps(["Official"])))
            c.execute("INSERT INTO metadata_profiles(name,primary_types,secondary_types,release_statuses) VALUES(?,?,?,?)",
                      ("Everything", json.dumps(["Album", "EP", "Single", "Broadcast", "Other"]),
                       json.dumps(["Compilation", "Soundtrack", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo", "Spokenword", "Interview", "Audiobook", "Audio drama", "Field recording"]),
                       json.dumps(["Official", "Promotion", "Bootleg"])))
        mf = os.environ.get("MUSICARR_MUSIC_DIR")
        if mf:
            c.execute("INSERT OR IGNORE INTO root_folders(path) VALUES(?)", (mf,))


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    with _lock:
        r = conn().execute(sql, args).fetchone()
        return dict(r) if r else None


def ex(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def insert(table, data):
    keys = list(data.keys())
    sql = f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})"
    return ex(sql, [_enc(data[k]) for k in keys])


def update(table, rid, data):
    if not data:
        return
    sets = ",".join(f"{k}=?" for k in data)
    ex(f"UPDATE {table} SET {sets} WHERE id=?", [_enc(v) for v in data.values()] + [rid])


def _enc(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    if isinstance(v, bool):
        return int(v)
    return v


def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    if not r:
        return DEFAULT_SETTINGS.get(key, default)
    return json.loads(r["value"])


def all_settings():
    return {r["key"]: json.loads(r["value"]) for r in q("SELECT key,value FROM settings")}


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, json.dumps(value)))


def add_history(event, album_id=None, artist_id=None, source_title="", quality="", protocol="", data=None):
    if album_id and not artist_id:
        a = q1("SELECT artist_id FROM albums WHERE id=?", (album_id,))
        artist_id = a["artist_id"] if a else None
    insert("history", dict(album_id=album_id, artist_id=artist_id, event=event, source_title=source_title,
                           quality=quality, protocol=protocol, data=data or {}, date=now()))


JSON_FIELDS = {"genres", "tags", "links", "secondary_types", "allowed", "protocol_order", "primary_types",
               "release_statuses", "config", "extra", "payload", "data"}


def decode(row):
    if not row:
        return row
    for k in list(row.keys()):
        if k in JSON_FIELDS and isinstance(row[k], str):
            try:
                row[k] = json.loads(row[k])
            except Exception:
                pass
    return row
