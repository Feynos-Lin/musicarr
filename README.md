# Musicarr

Sonarr/Radarr-style automation for music. Add artists, Musicarr tracks their discography on MusicBrainz, finds releases on **Usenet, torrents and Soulseek**, sends them to your download client, then tags, renames and files them into your library — and tells Navidrome/Plex/Jellyfin to rescan.

## Features

**Library**
- Artists and albums from MusicBrainz (artist search, or search by album to find the artist)
- Monitoring per artist and per album: all / future / missing / existing / latest / first / none, plus "monitor new albums"
- Metadata profiles: choose release types (Album, EP, Single…), secondary types (Live, Compilation, Soundtrack, Remix…) and statuses (Official, Bootleg…)
- Pick a specific edition (deluxe, remaster, different track count) per album
- Library Import: scans your existing music folders, matches them on MusicBrainz and links files without moving anything
- Mass editor, poster and table views, sort/filter, global search
- Optional Last.fm bios and fanart.tv artist photos

**Searching & downloading**
- Torznab (torrents) and Newznab (Usenet) indexers — works with Prowlarr, Jackett, NZBHydra2
- Soulseek through [slskd](https://github.com/slskd/slskd), fully managed from Musicarr's Settings → Soulseek (account, sharing, limits, folders, connect/disconnect). Finds complete album folders and scores them against the real tracklist (match %, free slot, speed, queue length)
- Download clients: qBittorrent, Transmission, Deluge, SABnzbd, NZBGet, slskd, torrent/Usenet blackhole
- Quality profiles: FLAC 24-bit → FLAC → ALAC → MP3-320/V0 → AAC… with cutoff, upgrades, preferred / required / ignored words, minimum seeders, protocol preference
- Decision engine rejects wrong artist/album, discographies, dead torrents, too-small releases, blocklisted releases, non-upgrades
- Automatic search, interactive search (see every result and why it was rejected), RSS sync, scheduled missing search, cutoff-unmet search
- Failed download handling: stalled/failed downloads are blocklisted, removed and re-searched

**Importing**
- Matches files to tracks by MusicBrainz IDs, tags, track numbers and titles; blocks imports below a match threshold for manual review
- Manual import with per-track file picker
- Writes Picard-compatible MusicBrainz tags, embeds cover art, saves `cover.jpg`
- Renaming with tokens (`{Album Artist}`, `{Album Title}`, `{Release Year}`, `{track:00}`, `{medium:0}`, `{Track Title}`, `{Quality}`…), multi-disc aware
- Move / copy / hardlink (torrents are copied so they keep seeding), recycle bin, remote path mappings
- Upgrades replace lower-quality files track by track, never downgrade

**Everything else**
- Calendar (month + agenda) with iCal feed
- Activity: queue with live progress, history, blocklist; Wanted: missing and cutoff unmet
- Import lists: ListenBrainz top artists, ListenBrainz similar artists (experimental), Last.fm top artists, MusicBrainz collections, custom MBID lists
- Connect: Discord, ntfy, Gotify, Pushover, Telegram, webhooks, and library scans for Navidrome, Jellyfin and Plex
- System: health checks, scheduled tasks with "run now", logs, weekly database backups (last 14 kept), API key, optional login
- Full REST API at `/api/v1` (`X-Api-Key` header)

## Install (Docker Desktop on Windows)

1. Push this folder to `github.com/Feynos-Lin/musicarr`. The included workflow builds `ghcr.io/feynos-lin/musicarr:latest` on every push.
   Then on GitHub: your profile → Packages → musicarr → Package settings → change visibility to **Public** (or `docker login ghcr.io` on the server).
2. Copy `compose.yaml` into `Documents\Docker\compose\musicarr\`, edit the two folder paths marked `<--`, and add `- musicarr/compose.yaml` to your main compose file's `include:` list. No Soulseek details go in the compose file.
3. `docker compose up -d` from PowerShell in your compose folder.
4. Open `http://192.168.0.25:8689` (or add a `musicarr.feynos.com` proxy host in Nginx Proxy Manager — set a username/password in Settings → General first).

Prefer not to use GHCR? Comment out `image:` and uncomment `build: .` in `compose.yaml`, and keep the source next to it.

## First-run setup

1. **Settings → Media Management** — root folder `/music` is added automatically. Check the naming example.
2. **Settings → Soulseek** — slskd is already connected. Choose a Soulseek username and password (the account is created on first login), check the shared folders and limits, and Save. Musicarr writes slskd's config and logs in.
3. **Settings → Download Clients** — add your torrent/Usenet clients. If a client runs in another container with different paths, add a Remote Path Mapping.
4. **Settings → Indexers** — in Prowlarr, open each indexer and copy its Torznab/Newznab URL + Prowlarr's API key.
5. **Settings → Connect** — add Navidrome so new music shows up immediately.
6. **Artists → Library Import** to bring in what you already have, or **Add New**.

Every connection has a **Test** button.

## Notes

- MusicBrainz allows one request per second, so adding a big artist takes a minute while albums and tracklists load. The artist page fills in on its own.
- Soulseek searches take about 30 seconds. Automatic search tries Usenet/torrents first and only falls back to Soulseek when nothing meets your cutoff.
- Musicarr only automates the clients and indexers you configure; you're responsible for what you download and share.

## Development

```
pip install -r requirements.txt
MUSICARR_CONFIG=./config MUSICARR_MUSIC_DIR=./music python -m app.main
```
Backend: Starlette + SQLite (`app/`). Frontend: dependency-free JS in `app/static/`.
