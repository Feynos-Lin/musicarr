"""Embeds artwork into real audio files (generated with ffmpeg) and checks it reads back correctly."""
import io
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp("musicarr")
    os.environ["MUSICARR_CONFIG"] = str(base / "config")
    from app import db
    db.init()
    return base


def make_image(color, size=(1600, 1600), fmt="PNG"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    return buf.getvalue()


FORMATS = {
    ".flac": ["-c:a", "flac"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    ".m4a": ["-c:a", "aac", "-b:a", "128k"],
    ".ogg": ["-c:a", "libvorbis"],
    ".opus": ["-c:a", "libopus"],
    ".wav": ["-c:a", "pcm_s16le"],
}


def make_audio(folder, name, ext):
    path = os.path.join(folder, name + ext)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    *FORMATS[ext], path], check=True)
    return path


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
@pytest.mark.parametrize("ext", list(FORMATS))
def test_embed_replace_and_keep(env, ext):
    from PIL import Image
    from app import artwork
    folder = str(env / "files")
    os.makedirs(folder, exist_ok=True)
    path = make_audio(folder, "track", ext)
    assert not artwork.has_art(path)

    red, mime, w, h = artwork.normalize(make_image((255, 0, 0)))
    assert mime == "image/jpeg" and (w, h) == (1200, 1200)  # resized from 1600 to the default 1200
    assert artwork.embed(path, red, mime, replace=True, w=w, h=h)
    assert artwork.has_art(path)
    got = Image.open(io.BytesIO(artwork.extract(path))).convert("RGB")
    assert got.getpixel((10, 10))[0] > 200

    blue, mime, w, h = artwork.normalize(make_image((0, 0, 255)))
    assert artwork.embed(path, blue, mime, replace=False) is False  # keeps existing art
    assert Image.open(io.BytesIO(artwork.extract(path))).convert("RGB").getpixel((10, 10))[0] > 200

    assert artwork.embed(path, blue, mime, replace=True)  # swaps it
    px = Image.open(io.BytesIO(artwork.extract(path))).convert("RGB").getpixel((10, 10))
    assert px[2] > 200 and px[0] < 60

    import mutagen
    assert mutagen.File(path) is not None  # file is still valid audio


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg needed")
def test_update_album_end_to_end(env, monkeypatch):
    from PIL import Image
    from app import artwork, db
    folder = str(env / "music" / "Artist" / "Album (2020)")
    os.makedirs(folder)
    aid = db.insert("artists", dict(mbid="art-1", name="Artist", sort_name="Artist", added=db.now()))
    alb = db.insert("albums", dict(mbid="rg-1", artist_id=aid, title="Album", release_date="2020-01-01", path=folder))
    for i, ext in enumerate([".flac", ".mp3", ".m4a"]):
        p = make_audio(folder, f"0{i+1} - Song", ext)
        db.insert("tracks", dict(album_id=alb, title="Song", disc=1, position=i + 1, file_path=p))

    green = make_image((0, 255, 0), fmt="JPEG")
    monkeypatch.setattr(artwork, "best_cover", lambda album: green)
    r = artwork.update_album(alb)
    assert r["embedded"] == 3 and r["cover_file"] and r["source"] == "Cover Art Archive"
    assert os.path.exists(os.path.join(folder, "cover.jpg"))

    # missing-only pass does nothing now
    assert artwork.update_album(alb, missing_only=True)["source"] == "skipped"

    # custom upload replaces everything
    purple = make_image((128, 0, 128))
    r = artwork.update_album(alb, data=purple)
    assert r["source"] == "custom" and r["embedded"] == 3
    for t in db.q("SELECT file_path FROM tracks WHERE album_id=?", (alb,)):
        px = Image.open(io.BytesIO(artwork.extract(t["file_path"]))).convert("RGB").getpixel((5, 5))
        assert px[0] > 100 and px[1] < 40 and px[2] > 100
    assert db.q1("SELECT cover FROM albums WHERE id=?", (alb,))["cover"].startswith(f"/api/v1/albums/{alb}/cover")

    # with no Cover Art Archive art, existing cover.jpg is spread to new files
    monkeypatch.setattr(artwork, "best_cover", lambda album: None)
    extra = make_audio(folder, "04 - Bonus", ".flac")
    db.insert("tracks", dict(album_id=alb, title="Bonus", disc=1, position=4, file_path=extra))
    r = artwork.update_album(alb, missing_only=True)
    assert r["source"] == "existing cover file" and artwork.has_art(extra)
