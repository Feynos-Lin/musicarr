"""Matching audio files to MusicBrainz tracks, tagging, renaming, moving into the library, and disk scans."""
import os
import re
import shutil

from . import db, notify
from .quality import probe, rank
from .util import clean_name, http, is_audio, log, similarity, tokens

try:
    import mutagen
    from mutagen.easyid3 import EasyID3  # noqa
    HAVE_MUTAGEN = True
except Exception:  # pragma: no cover
    HAVE_MUTAGEN = False


# ---------------- tags ----------------
def read_tags(path):
    out = {}
    if not HAVE_MUTAGEN:
        return out
    try:
        f = mutagen.File(path, easy=True)
        if f is None or not f.tags:
            return out
        g = lambda k: (f.tags.get(k) or [None])[0]
        out = {"title": g("title"), "album": g("album"), "artist": g("artist"), "albumartist": g("albumartist"),
               "tracknumber": g("tracknumber"), "discnumber": g("discnumber"),
               "rg_mbid": g("musicbrainz_releasegroupid"), "release_mbid": g("musicbrainz_albumid"),
               "track_mbid": g("musicbrainz_releasetrackid") or g("musicbrainz_trackid")}
        for k in ("tracknumber", "discnumber"):
            if out.get(k):
                m = re.match(r"\d+", str(out[k]))
                out[k] = int(m.group()) if m else None
    except Exception as e:
        log.debug("Tag read failed for %s: %s", path, e)
    return out


def write_tags(path, artist, album, track, cover_bytes=None):
    if not HAVE_MUTAGEN or not db.get_setting("write_tags"):
        return
    try:
        f = mutagen.File(path, easy=True)
        if f is None:
            return
        if f.tags is None:
            f.add_tags()
        total_discs = db.q1("SELECT MAX(disc) m FROM tracks WHERE album_id=?", (album["id"],))["m"] or 1
        total_tracks = db.q1("SELECT COUNT(*) c FROM tracks WHERE album_id=? AND disc=?", (album["id"], track["disc"]))["c"]
        vals = {
            "title": track["title"], "album": album["title"], "artist": track.get("artist_credit") or artist["name"],
            "albumartist": artist["name"], "tracknumber": f"{track['position']}/{total_tracks}",
            "discnumber": f"{track['disc']}/{total_discs}", "date": (album.get("release_date") or "")[:10],
            "musicbrainz_releasegroupid": album["mbid"], "musicbrainz_albumartistid": artist["mbid"],
            "musicbrainz_artistid": artist["mbid"],
        }
        if album.get("release_mbid"):
            vals["musicbrainz_albumid"] = album["release_mbid"]
        if track.get("mbid"):
            vals["musicbrainz_releasetrackid"] = track["mbid"]
        if track.get("recording_mbid"):
            vals["musicbrainz_trackid"] = track["recording_mbid"]
        genres = album.get("genres") or artist.get("genres") or []
        if isinstance(genres, list) and genres:
            vals["genre"] = genres[0].title()
        for k, v in vals.items():
            if not v:
                continue
            try:
                f.tags[k] = [str(v)]
            except Exception:
                pass  # key not supported by this container's easy interface
        f.save()
        if cover_bytes and db.get_setting("embed_cover"):
            from . import artwork
            img, mime, w, h = cover_bytes
            artwork.embed(path, img, mime, replace=bool(db.get_setting("artwork_replace_existing")), w=w, h=h)
    except Exception as e:
        log.warning("Tagging failed for %s: %s", path, e)


# ---------------- naming ----------------
def _fmt(template, ctx):
    def rep(m):
        key, pad = m.group(1), m.group(2)
        v = ctx.get(key.lower(), "")
        if pad is not None and str(v).isdigit():
            return str(v).zfill(len(pad))
        return str(v)
    return re.sub(r"\{([A-Za-z ]+)(?::(0+))?\}", rep, template)


def context(artist, album, track=None, quality=""):
    year = (album.get("release_date") or "")[:4]
    c = {"album artist": artist["name"], "artist name": artist["name"], "artist cleanname": re.sub(r"\W", "", artist["name"]),
         "artist disambiguation": artist.get("disambiguation") or "", "album title": album["title"],
         "release year": year or "Unknown", "album type": album.get("album_type") or "", "quality": quality}
    if track:
        c.update({"track title": track["title"], "track": track["position"], "medium": track["disc"],
                  "track artist": track.get("artist_credit") or artist["name"]})
    return c


def artist_path(artist):
    if artist.get("path"):
        return artist["path"]
    root = artist.get("root_folder") or (db.q1("SELECT path FROM root_folders ORDER BY id") or {}).get("path") or "/music"
    return os.path.join(root, clean_name(_fmt(db.get_setting("artist_folder_format"), context(artist, {"title": ""}))))


def album_path(artist, album):
    return os.path.join(artist_path(artist), clean_name(_fmt(db.get_setting("album_folder_format"), context(artist, album))))


def track_filename(artist, album, track, ext, quality, multi_disc):
    tmpl = db.get_setting("multi_disc_track_format" if multi_disc else "track_format")
    return clean_name(_fmt(tmpl, context(artist, album, track, quality))) + ext.lower()


# ---------------- matching ----------------
def collect_audio(folder):
    if os.path.isfile(folder):
        return [folder] if is_audio(folder) else []
    files = []
    for root, _, names in os.walk(folder):
        for n in names:
            if is_audio(n) and not n.startswith("._"):
                files.append(os.path.join(root, n))
    return sorted(files)


def _fname_track(path):
    base = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^\s*(?:(\d)[-.])?(\d{1,3})[\s.\-_)]+(.*)$", base)
    if m:
        return (int(m.group(1)) if m.group(1) else None), int(m.group(2)), m.group(3)
    return None, None, base


def match_files(files, tracks):
    """Return {track_id: path}. Scores tag numbers, MB ids, tag titles and filenames; greedy best-first."""
    multi = len({t["disc"] for t in tracks}) > 1
    pairs = []
    info = {}
    for p in files:
        tg = read_tags(p)
        fd, fn, ftitle = _fname_track(p)
        parent = os.path.basename(os.path.dirname(p)).lower()
        dm = re.search(r"(?:cd|disc|disk)\s*(\d+)", parent)
        info[p] = (tg, fd or (int(dm.group(1)) if dm else None), fn, ftitle)
    for t in tracks:
        for p in files:
            tg, fd, fn, ftitle = info[p]
            s = 0.0
            if tg.get("track_mbid") and tg["track_mbid"] in (t.get("mbid"), t.get("recording_mbid")):
                s += 5
            title_sim = max(similarity(t["title"], tg.get("title") or ""), similarity(t["title"], ftitle))
            if tokens(t["title"]) and all(x in tokens(ftitle) + tokens(tg.get("title") or "") for x in tokens(t["title"])):
                title_sim = max(title_sim, 0.85)
            s += title_sim * 2
            num = tg.get("tracknumber") or fn
            disc = tg.get("discnumber") or fd or 1
            if num == t["position"] and (not multi or disc == t["disc"]):
                s += 1.5
            elif not multi and num and len(files) == len(tracks) and num == t["position"]:
                s += 1.0
            if s >= 1.6:
                pairs.append((s, t["id"], p))
    pairs.sort(reverse=True)
    used_t, used_p, out = set(), set(), {}
    for s, tid, p in pairs:
        if tid in used_t or p in used_p:
            continue
        used_t.add(tid)
        used_p.add(p)
        out[tid] = p
    # Same file count & nothing better: fall back to ordinal order
    if len(out) < len(tracks) * 0.5 and len(files) == len(tracks):
        ordered = sorted(tracks, key=lambda t: (t["disc"], t["position"]))
        out = {t["id"]: f for t, f in zip(ordered, files)}
    return out


# ---------------- importing ----------------
def _transfer(src, dst, mode):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.abspath(src) == os.path.abspath(dst):
        return
    if os.path.exists(dst):
        os.remove(dst)
    if mode == "hardlink":
        try:
            os.link(src, dst)
            return
        except OSError:
            mode = "copy"
    if mode == "copy":
        shutil.copy2(src, dst)
    else:
        shutil.move(src, dst)


def _retire(path):
    if not path or not os.path.exists(path):
        return
    rb = db.get_setting("recycle_bin")
    if rb:
        dst = os.path.join(rb, os.path.relpath(path, "/").replace(os.sep, "_"))
        os.makedirs(rb, exist_ok=True)
        shutil.move(path, dst)
    else:
        os.remove(path)


def fetch_cover(album):
    """Cover for a new import, normalised once: (bytes, mime, width, height) or None."""
    from . import artwork
    try:
        data = artwork.best_cover(album)
        return artwork.normalize(data) if data else None
    except Exception as e:
        log.debug("Cover download failed: %s", e)
        return None


def import_album(album_id, folder, mode=None, force=False, source_title="", queue_id=None, overrides=None):
    """Import an album from a folder. overrides = {track_id: path} for manual imports."""
    album = db.decode(db.q1("SELECT * FROM albums WHERE id=?", (album_id,)))
    artist = db.decode(db.q1("SELECT * FROM artists WHERE id=?", (album["artist_id"],)))
    tracks = db.q("SELECT * FROM tracks WHERE album_id=? ORDER BY disc, position", (album_id,))
    if not tracks:
        raise RuntimeError("Album has no tracklist yet — refresh the artist first")
    files = collect_audio(folder)
    if not files:
        raise RuntimeError(f"No audio files found in {folder}")
    mapping = overrides or match_files(files, tracks)
    pct = len(mapping) / len(tracks) * 100
    min_pct = int(db.get_setting("min_track_match_pct") or 80)
    if pct < min_pct and not force:
        raise RuntimeError(f"Only matched {len(mapping)}/{len(tracks)} tracks ({pct:.0f}% < {min_pct}%). Use manual import.")
    mode = mode or db.get_setting("import_mode") or "move"
    multi = len({t["disc"] for t in tracks}) > 1
    dest_dir = album_path(artist, album)
    cover = fetch_cover(album) if (db.get_setting("embed_cover") or db.get_setting("save_cover_jpg")) else None
    upgraded = any(t["file_path"] for t in tracks)
    imported, qualities = 0, []
    tmap = {t["id"]: t for t in tracks}
    for tid, src in mapping.items():
        t = tmap[int(tid)]
        q, br = probe(src)
        if t["file_path"] and os.path.exists(t["file_path"]) and not force and t["quality"] and rank(q) > rank(t["quality"]):
            continue  # never downgrade a track
        ext = os.path.splitext(src)[1]
        dst = os.path.join(dest_dir, track_filename(artist, album, t, ext, q, multi)) if db.get_setting("naming_enabled") \
            else os.path.join(dest_dir, os.path.basename(src))
        if t["file_path"] and t["file_path"] != dst:
            _retire(t["file_path"])
        _transfer(src, dst, mode)
        write_tags(dst, artist, album, t, cover)
        db.update("tracks", t["id"], {"file_path": dst, "quality": q, "bitrate": br, "file_size": os.path.getsize(dst)})
        imported += 1
        qualities.append(q)
    if cover and db.get_setting("save_cover_jpg"):
        try:
            with open(os.path.join(dest_dir, "cover.jpg" if cover[1] == "image/jpeg" else "cover.png"), "wb") as fh:
                fh.write(cover[0])
        except OSError:
            pass
    db.update("albums", album_id, {"path": dest_dir})
    if not artist.get("path"):
        db.update("artists", artist["id"], {"path": artist_path(artist)})
    if mode == "move" and db.get_setting("delete_empty_folders") and os.path.isdir(folder):
        _cleanup(folder)
    q = max(qualities, key=rank) if qualities else ""
    event = "upgraded" if upgraded else "imported"
    db.add_history(event, album_id, artist["id"], source_title or os.path.basename(folder), q, "",
                   {"files": imported, "tracks": len(tracks), "path": dest_dir})
    log.info("%s %d/%d tracks of %s - %s [%s]", event.title(), imported, len(tracks), artist["name"], album["title"], q)
    notify.send("upgrade" if upgraded else "import", f"{'Upgraded' if upgraded else 'Imported'}: {artist['name']} – {album['title']}",
                f"{imported}/{len(tracks)} tracks · {q}", album=album, artist=artist)
    return {"imported": imported, "total": len(tracks), "quality": q, "path": dest_dir}


def _cleanup(folder):
    """Remove leftover non-audio junk folders after a move import (only if no audio remains)."""
    if collect_audio(folder):
        return
    try:
        shutil.rmtree(folder)
    except OSError:
        pass


def preview(album_id, folder):
    tracks = db.q("SELECT * FROM tracks WHERE album_id=? ORDER BY disc, position", (album_id,))
    files = collect_audio(folder)
    mapping = match_files(files, tracks)
    rev = {v: k for k, v in mapping.items()}
    return {"tracks": tracks, "files": [{"path": f, "track_id": rev.get(f), "quality": probe(f)[0]} for f in files]}


# ---------------- disk scanning ----------------
def scan_artist(artist_id):
    """Link existing files under the artist folder to albums/tracks, and drop links to deleted files."""
    artist = db.decode(db.q1("SELECT * FROM artists WHERE id=?", (artist_id,)))
    path = artist_path(artist)
    for t in db.q("SELECT t.id, t.file_path FROM tracks t JOIN albums a ON a.id=t.album_id WHERE a.artist_id=? AND t.file_path IS NOT NULL", (artist_id,)):
        if not os.path.exists(t["file_path"]):
            db.update("tracks", t["id"], {"file_path": None, "quality": None, "file_size": None})
            if db.get_setting("unmonitor_deleted"):
                db.ex("UPDATE albums SET monitored=0 WHERE id=(SELECT album_id FROM tracks WHERE id=?)", (t["id"],))
    if not os.path.isdir(path):
        return 0
    linked = {r["file_path"] for r in db.q("SELECT file_path FROM tracks WHERE file_path IS NOT NULL")}
    albums = db.q("SELECT * FROM albums WHERE artist_id=?", (artist_id,))
    by_mbid = {a["mbid"]: a for a in albums}
    groups = {}
    for f in collect_audio(path):
        if f in linked:
            continue
        tg = read_tags(f)
        album = by_mbid.get(tg.get("rg_mbid"))
        if not album:
            name = tg.get("album") or os.path.basename(os.path.dirname(f))
            name = re.sub(r"\s*[\(\[]\d{4}[\)\]]\s*$", "", name)
            best = max(albums, key=lambda a: similarity(a["title"], name), default=None)
            if best and similarity(best["title"], name) >= 0.8:
                album = best
        if album:
            groups.setdefault(album["id"], []).append(f)
    found = 0
    for aid, files in groups.items():
        tracks = db.q("SELECT * FROM tracks WHERE album_id=? AND file_path IS NULL", (aid,))
        for tid, p in match_files(files, tracks).items():
            q, br = probe(p)
            db.update("tracks", tid, {"file_path": p, "quality": q, "bitrate": br, "file_size": os.path.getsize(p)})
            found += 1
        db.update("albums", aid, {"path": os.path.dirname(files[0])})
    if found:
        log.info("Disk scan linked %d existing files for %s", found, artist["name"])
    if not artist.get("path"):
        db.update("artists", artist_id, {"path": path})
    return found


def unmapped_folders():
    """Top-level folders under root folders that don't belong to any artist (for Library Import)."""
    known = set()
    for a in db.q("SELECT * FROM artists"):
        known.add(os.path.normcase(os.path.normpath(a["path"] or artist_path(a))))
    out = []
    for rf in db.q("SELECT path FROM root_folders"):
        try:
            for n in sorted(os.listdir(rf["path"])):
                p = os.path.join(rf["path"], n)
                if os.path.isdir(p) and os.path.normcase(os.path.normpath(p)) not in known and not n.startswith("."):
                    out.append({"path": p, "name": n, "root_folder": rf["path"]})
        except OSError:
            continue
    return out
