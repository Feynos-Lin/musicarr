"""Album and artist artwork: find covers on the Cover Art Archive, embed them into audio files,
write cover.jpg / artist.jpg, and refresh whole albums, artists or the entire library."""
import base64
import io
import os

from . import db
from .util import http, is_audio, log

try:
    import mutagen
    HAVE_MUTAGEN = True
except Exception:  # pragma: no cover
    HAVE_MUTAGEN = False

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False

CAA = "https://coverartarchive.org"


# ---------------- images ----------------
def sniff_mime(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return None


def normalize(data):
    """Return (jpeg_bytes, 'image/jpeg', width, height). Re-encodes to a baseline JPEG no larger than the
    configured size so every player can read it; without Pillow the image is passed through untouched."""
    max_px = int(db.get_setting("artwork_max_size") or 0)
    if not HAVE_PIL:
        mime = sniff_mime(data)
        if mime not in ("image/jpeg", "image/png"):
            raise ValueError("Unsupported image format")
        return data, mime, 0, 0
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise ValueError("That file isn't a readable image")
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (0, 0, 0))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode == "L":
        im = im.convert("RGB")
    if max_px and max(im.size) > max_px:
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue(), "image/jpeg", im.size[0], im.size[1]


def download(url):
    r = http().get(url, timeout=45)
    r.raise_for_status()
    if not sniff_mime(r.content):
        raise ValueError("Downloaded file is not an image")
    return r.content


def caa_options(album):
    """Every cover image the Cover Art Archive has for this album's chosen release and release group."""
    out, seen = [], set()
    sources = []
    if album.get("release_mbid"):
        sources.append(("release", f"{CAA}/release/{album['release_mbid']}", "Selected release"))
    sources.append(("release-group", f"{CAA}/release-group/{album['mbid']}", "Album (any release)"))
    for kind, url, label in sources:
        try:
            r = http().get(url, timeout=20)
            if r.status_code != 200:
                continue
            for img in r.json().get("images", []):
                key = img.get("id") or img.get("image")
                if key in seen:
                    continue
                seen.add(key)
                th = img.get("thumbnails") or {}
                out.append({
                    "id": str(key), "url": (th.get("1200") or img.get("image")), "full": img.get("image"),
                    "thumb": th.get("500") or th.get("large") or th.get("250") or img.get("image"),
                    "types": img.get("types") or [], "front": bool(img.get("front")), "source": label,
                    "comment": img.get("comment") or "",
                })
        except Exception as e:
            log.debug("Cover Art Archive lookup %s failed: %s", url, e)
    out.sort(key=lambda i: (not i["front"], i["source"] != "Selected release"))
    return out


def best_cover(album):
    """Front cover for the album: chosen release first, then the release group."""
    size = int(db.get_setting("artwork_max_size") or 0)
    suffix = "front-1200" if size and size <= 1200 else "front"
    urls = []
    if album.get("release_mbid"):
        urls.append(f"{CAA}/release/{album['release_mbid']}/{suffix}")
    urls.append(f"{CAA}/release-group/{album['mbid']}/{suffix}")
    for u in urls:
        try:
            return download(u)
        except Exception:
            continue
    return None


# ---------------- embedded art ----------------
def _picture(data, mime, w=0, h=0):
    from mutagen.flac import Picture
    p = Picture()
    p.type, p.mime, p.desc, p.data = 3, mime, "Cover", data
    p.width, p.height, p.depth = w, h, 24 if w else 0
    return p


def has_art(path):
    if not HAVE_MUTAGEN:
        return False
    try:
        f = mutagen.File(path)
        if f is None:
            return False
        cls = type(f).__name__
        if cls == "FLAC":
            return bool(f.pictures)
        if cls in ("MP4",):
            return bool(f.tags and f.tags.get("covr"))
        if cls in ("OggVorbis", "OggOpus", "OggFLAC", "OggSpeex"):
            return bool(f.tags and (f.tags.get("metadata_block_picture") or f.tags.get("coverart")))
        if cls == "ASF":
            return bool(f.tags and f.tags.get("WM/Picture"))
        tags = getattr(f, "tags", None)  # MP3, WAVE, AIFF use ID3
        return bool(tags and hasattr(tags, "getall") and tags.getall("APIC"))
    except Exception:
        return False


def embed(path, data, mime="image/jpeg", replace=True, w=0, h=0):
    """Embed front cover art. Returns True if the file was changed."""
    if not HAVE_MUTAGEN:
        return False
    ext = os.path.splitext(path)[1].lower()
    if not replace and has_art(path):
        return False
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            f = FLAC(path)
            f.clear_pictures()
            f.add_picture(_picture(data, mime, w, h))
            f.save()
        elif ext == ".mp3":
            from mutagen.id3 import ID3, APIC, ID3NoHeaderError
            try:
                t = ID3(path)
            except ID3NoHeaderError:
                t = ID3()
            t.delall("APIC")
            t.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            t.save(path)
        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            from mutagen.mp4 import MP4, MP4Cover
            f = MP4(path)
            if f.tags is None:
                f.add_tags()
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            f.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
            f.save()
        elif ext in (".ogg", ".oga", ".opus"):
            f = mutagen.File(path)
            if f.tags is None:
                f.add_tags()
            for k in ("coverart", "coverartmime"):
                if k in f.tags:
                    del f.tags[k]
            f.tags["metadata_block_picture"] = [base64.b64encode(_picture(data, mime, w, h).write()).decode("ascii")]
            f.save()
        elif ext in (".wav", ".aiff", ".aif"):
            from mutagen.id3 import APIC
            f = mutagen.File(path)
            if f.tags is None:
                f.add_tags()
            f.tags.delall("APIC")
            f.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
            f.save()
        elif ext == ".wma":
            from mutagen.asf import ASF, ASFByteArrayAttribute
            import struct
            f = ASF(path)
            m = mime.encode("utf-16-le") + b"\0\0"
            d = "Cover".encode("utf-16-le") + b"\0\0"
            f.tags["WM/Picture"] = [ASFByteArrayAttribute(struct.pack("<bi", 3, len(data)) + m + d + data)]
            f.save()
        else:
            return False  # APE / WavPack / DSF: leave alone
        return True
    except Exception as e:
        log.warning("Couldn't embed artwork in %s: %s", path, e)
        return False


def extract(path):
    """Return the embedded front cover bytes, if any."""
    if not HAVE_MUTAGEN:
        return None
    try:
        f = mutagen.File(path)
        cls = type(f).__name__
        if cls == "FLAC" and f.pictures:
            return f.pictures[0].data
        if cls == "MP4" and f.tags and f.tags.get("covr"):
            return bytes(f.tags["covr"][0])
        if cls in ("OggVorbis", "OggOpus") and f.tags and f.tags.get("metadata_block_picture"):
            from mutagen.flac import Picture
            return Picture(base64.b64decode(f.tags["metadata_block_picture"][0])).data
        tags = getattr(f, "tags", None)
        if tags is not None and hasattr(tags, "getall"):
            ap = tags.getall("APIC")
            if ap:
                return ap[0].data
    except Exception:
        pass
    return None


# ---------------- albums / artists ----------------
def album_folder(album_id):
    al = db.q1("SELECT path FROM albums WHERE id=?", (album_id,))
    files = [t["file_path"] for t in db.q("SELECT file_path FROM tracks WHERE album_id=? AND file_path IS NOT NULL", (album_id,))]
    if files:
        dirs = {os.path.dirname(p) for p in files}
        # multi-disc albums in CD1/CD2 subfolders: use their common parent
        return os.path.commonpath(list(dirs)) if len(dirs) > 1 else dirs.pop()
    return al["path"] if al else None


def local_cover(album_id):
    folder = album_folder(album_id)
    if folder:
        for n in ("cover.jpg", "cover.png", "folder.jpg", "front.jpg"):
            p = os.path.join(folder, n)
            if os.path.isfile(p):
                return p
    return None


def update_album(album_id, data=None, url=None, replace=True, missing_only=False):
    """Refresh one album's artwork.
    data/url: a specific image (custom upload or a picked Cover Art Archive image).
    Otherwise the best Cover Art Archive front cover is used, falling back to an existing cover.jpg
    or the art already embedded in the first track (so it can be spread to every track)."""
    album = db.decode(db.q1("SELECT * FROM albums WHERE id=?", (album_id,)))
    if not album:
        raise ValueError("Album not found")
    files = [t["file_path"] for t in db.q("SELECT file_path FROM tracks WHERE album_id=? AND file_path IS NOT NULL ORDER BY disc, position", (album_id,))
             if os.path.exists(t["file_path"])]
    folder = album_folder(album_id)
    if missing_only:
        cover_ok = bool(local_cover(album_id)) or not db.get_setting("save_cover_jpg")
        files = [f for f in files if not has_art(f)] if db.get_setting("embed_cover") else []
        if cover_ok and not files:
            return {"album_id": album_id, "embedded": 0, "files": 0, "cover_file": False, "source": "skipped"}
    source = "custom" if data else "picked" if url else "Cover Art Archive"
    if url and not data:
        data = download(url)
    if not data:
        data = best_cover(album)
    if not data:
        lc = local_cover(album_id)
        if lc:
            data, source = open(lc, "rb").read(), "existing cover file"
    if not data:
        for f in files:
            data = extract(f)
            if data:
                source = "existing embedded art"
                break
    if not data:
        raise RuntimeError("No artwork found for this album — upload one instead")
    img, mime, w, h = normalize(data)
    embedded = 0
    if db.get_setting("embed_cover"):
        for f in files:
            if embed(f, img, mime, replace=replace, w=w, h=h):
                embedded += 1
    wrote = False
    if folder and os.path.isdir(folder) and db.get_setting("save_cover_jpg"):
        name = "cover.jpg" if mime == "image/jpeg" else "cover.png"
        target = os.path.join(folder, name)
        if replace or not os.path.exists(target) or missing_only:
            with open(target, "wb") as fh:
                fh.write(img)
            wrote = True
    if source in ("custom", "picked") or wrote:
        db.update("albums", album_id, {"cover": f"/api/v1/albums/{album_id}/cover?v={db.now()}"} if source == "custom"
                  else {"cover": url} if source == "picked" else {})
    try:
        from . import mediacover
        mediacover.invalidate("album", album_id)
    except Exception:
        pass
    db.add_history("artworkUpdated", album_id, source_title=album["title"], data={"source": source, "embedded": embedded, "cover_file": wrote})
    log.info("Artwork for %s: %s, embedded in %d file(s)%s", album["title"], source, embedded, ", saved cover file" if wrote else "")
    return {"album_id": album_id, "embedded": embedded, "files": len(files), "cover_file": wrote, "source": source}


def update_artist_image(artist_id, replace=True):
    """Save the artist photo as artist.jpg in the artist folder (Navidrome, Plex and Jellyfin pick it up)."""
    if not db.get_setting("save_artist_image"):
        return False
    a = db.q1("SELECT * FROM artists WHERE id=?", (artist_id,))
    if not a or not a.get("image") or "coverartarchive" in (a["image"] or ""):
        return False  # no real artist photo (album cover fallback isn't an artist picture)
    from .importer import artist_path
    folder = a.get("path") or artist_path(a)
    if not os.path.isdir(folder):
        return False
    target = os.path.join(folder, "artist.jpg")
    if os.path.exists(target) and not replace:
        return False
    try:
        img, mime, _, _ = normalize(download(a["image"]))
        with open(target, "wb") as fh:
            fh.write(img)
        return True
    except Exception as e:
        log.debug("Artist image for %s failed: %s", a["name"], e)
        return False


def update_artist(artist_id, replace=True, missing_only=False):
    albums = db.q("""SELECT DISTINCT al.id, al.title FROM albums al JOIN tracks t ON t.album_id=al.id
                     WHERE al.artist_id=? AND t.file_path IS NOT NULL""", (artist_id,))
    done = failed = files = 0
    for al in albums:
        try:
            r = update_album(al["id"], replace=replace, missing_only=missing_only)
            if r["source"] != "skipped":
                done += 1
                files += r["embedded"]
        except Exception as e:
            failed += 1
            log.info("No artwork for %s: %s", al["title"], e)
    update_artist_image(artist_id, replace=not missing_only)
    _rescan()
    return f"Updated {done} album(s), {files} file(s); {failed} without artwork"


def update_library(replace=False, missing_only=True):
    done = failed = files = 0
    for a in db.q("SELECT id FROM artists ORDER BY sort_name"):
        albums = db.q("""SELECT DISTINCT al.id, al.title FROM albums al JOIN tracks t ON t.album_id=al.id
                         WHERE al.artist_id=? AND t.file_path IS NOT NULL""", (a["id"],))
        for al in albums:
            try:
                r = update_album(al["id"], replace=replace, missing_only=missing_only)
                if r["source"] != "skipped":
                    done += 1
                    files += r["embedded"]
            except Exception as e:
                failed += 1
                log.debug("No artwork for %s: %s", al["title"], e)
        update_artist_image(a["id"], replace=replace)
    _rescan()
    return f"Updated {done} album(s), {files} file(s); {failed} without artwork"


def _rescan():
    from . import notify
    notify.send("artwork", "Artwork updated", "")
