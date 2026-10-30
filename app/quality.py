"""Quality detection from release names, Soulseek file attributes and files on disk."""
import os
import re

from .db import QUALITIES

RANK = {q: i for i, q in enumerate(QUALITIES)}  # lower = better


def rank(q):
    return RANK.get(q or "Unknown", len(QUALITIES))


def from_title(title):
    t = (title or "").lower()
    if re.search(r"\b(24[ -]?bit|24bit|hi[ -]?res|24-(44|48|88|96|176|192))\b", t) and "flac" in t:
        return "FLAC 24bit"
    if re.search(r"\b(24[ -]?bit|hi[ -]?res)\b", t) and not re.search(r"mp3|aac", t):
        return "FLAC 24bit"
    if "flac" in t or "lossless" in t:
        return "FLAC"
    if "alac" in t:
        return "ALAC"
    if re.search(r"\bwav\b", t):
        return "WAV"
    if re.search(r"\bape\b", t):
        return "APE"
    if re.search(r"\bwavpack|\bwv\b", t):
        return "WavPack"
    if re.search(r"\bv0\b|vbr[ -]?v?0\b|\(v0\)", t):
        return "MP3-V0"
    if re.search(r"\bv2\b|\(v2\)", t):
        return "MP3-V2"
    m = re.search(r"\b(320|256|224|192|160|128|96)\s*(k|kbps|kbit)?\b", t)
    is_aac = re.search(r"\baac\b|\bm4a\b|itunes", t)
    if m:
        br = m.group(1)
        if is_aac and br in ("320", "256", "192", "128"):
            return f"AAC-{br}"
        if f"MP3-{br}" in RANK:
            return f"MP3-{br}"
    if is_aac:
        return "AAC-256"
    if "opus" in t:
        return "Opus"
    if "ogg" in t or "vorbis" in t:
        return "OGG Vorbis"
    if "mp3" in t:
        return "MP3-320" if "320" in t else "MP3-192"
    if "web" in t and "flac" not in t:
        return "Unknown"
    return "Unknown"


def from_file(ext, bitrate=None, bit_depth=None):
    ext = (ext or "").lower().lstrip(".")
    if ext == "flac":
        return "FLAC 24bit" if bit_depth and bit_depth > 16 else "FLAC"
    if ext in ("alac",):
        return "ALAC"
    if ext == "m4a":
        if bitrate and bitrate > 500:
            return "ALAC"
        return _aac(bitrate)
    if ext in ("wav", "aiff", "aif"):
        return "WAV"
    if ext == "ape":
        return "APE"
    if ext == "wv":
        return "WavPack"
    if ext == "opus":
        return "Opus"
    if ext in ("ogg", "oga"):
        return "OGG Vorbis Q10" if bitrate and bitrate >= 450 else "OGG Vorbis"
    if ext == "aac":
        return _aac(bitrate)
    if ext == "mp3":
        if not bitrate:
            return "MP3-192"
        if bitrate >= 315:
            return "MP3-320"
        if 220 <= bitrate < 315 and bitrate not in (224, 256):
            return "MP3-V0"
        if bitrate >= 250:
            return "MP3-256"
        if bitrate >= 222:
            return "MP3-224"
        if bitrate >= 185:
            return "MP3-V2" if bitrate not in (192,) else "MP3-192"
        if bitrate >= 155:
            return "MP3-160"
        if bitrate >= 120:
            return "MP3-128"
        return "MP3-96"
    return "Unknown"


def _aac(bitrate):
    if not bitrate:
        return "AAC-256"
    if bitrate >= 300:
        return "AAC-320"
    if bitrate >= 240:
        return "AAC-256"
    if bitrate >= 180:
        return "AAC-192"
    return "AAC-128"


def probe(path):
    """Read quality + bitrate from a file on disk using mutagen if available."""
    ext = os.path.splitext(path)[1]
    bitrate = depth = None
    try:
        import mutagen
        f = mutagen.File(path)
        if f is not None and f.info:
            bitrate = int(getattr(f.info, "bitrate", 0) or 0) // 1000 or None
            depth = getattr(f.info, "bits_per_sample", None)
            codec = getattr(f.info, "codec", "") or ""
            if ext.lower() == ".m4a" and "alac" in codec.lower():
                return "ALAC", bitrate
    except Exception:
        pass
    return from_file(ext, bitrate, depth), bitrate


def worst(qualities):
    qs = [q for q in qualities if q]
    return max(qs, key=rank) if qs else "Unknown"


def meets_cutoff(quality, profile):
    return rank(quality) <= rank(profile["cutoff"])


def allowed(quality, profile):
    return quality in profile["allowed"]
