# Orama Server

**Everything around the [Orama](https://github.com/DarkAngelGR/orama) TV app, in one container.**
Plex stays the media server; Orama Server is its companion:

| Part | Port | What it does |
|---|---|---|
| **Watchlist sync** | 8789 | Your household's and friends' Plex Watchlists go to Sonarr/Radarr; Orama's TV endpoints (downloads, requests, subtitles, collections, Discover, delete) |
| **Converter** | 8789 | Fixes files Samsung TVs can't play directly: DTS audio → E-AC3, Dolby Vision 7 → 8.1, Dolby Vision 5 → HDR10 |
| **Dashboard** | 8765 | Every service's status, CPU/RAM/disks, and updating containers |
| **TV app updates** | 8790 | Installs new Orama releases on the TV (Apps2Samsung) |

It replaces four separate containers (watchlist-sync, mediadash, the Orama installer and
a Tdarr setup for DTS) with one, on the same ports, so Orama, bookmarks and webhooks keep
working. The watchlist sync alone is still published as `ghcr.io/darkangelgr/watchlist-sync`.

[![Latest release](https://img.shields.io/github/v/release/DarkAngelGR/watchlist-sync?label=release)](https://github.com/DarkAngelGR/watchlist-sync/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

## Getting started

Download [`docker-compose.yml`](docker-compose.yml), fill in the paths and options, then
`docker compose up -d`. Open `http://<server>:8789`, sign in with Plex and add Sonarr/Radarr.
Moving from the separate containers? See [MIGRATING.md](MIGRATING.md).

## The converter

Samsung TVs have no DTS and no Dolby Vision. Orama already works around both while playing
(it picks another audio track, or has the server convert), but fixing the files once means
they always play directly, with full quality.

- **DTS → E-AC3.** A DTS track with no playable track in the same language gets an E-AC3
  (Dolby Digital Plus) copy at 1536 kbps, as the default track. The DTS track is kept
  unless you turn that off. 7.1 becomes 5.1 (ffmpeg's E-AC3 limit), keeping the LFE.
- **Dolby Vision profile 7 → 8.1** (UHD Blu-ray remuxes). No re-encoding: only the Dolby
  Vision layer changes (dovi_tool), so the file always plays as HDR10. Off by default.
- **Dolby Vision profile 5 → HDR10** (streaming downloads that show purple and green on
  Samsung). Re-encoded with the Intel graphics (libplacebo + VAAPI); needs `/dev/dri` in the
  container. **Experimental**, off by default; *Check the graphics* on the settings page
  tests it first.

New files are fixed right after Sonarr/Radarr import them: add a webhook in each
(Settings › Connect › + › Webhook, *On Import* and *On Upgrade*, URL
`http://<server>:8789/api/hooks/arr`). A nightly scan catches everything else. One file at
a time, at low priority, optionally only during set hours. A file is only replaced after
the new copy has been checked (same length, every track present), by an atomic rename;
if anything fails, the original stays untouched. Plex is asked to rescan afterwards.

## The dashboard and TV app updates

The dashboard reads the same `config.yaml` as mediadash did (services, API keys, disks);
mount it at `/config/config.yaml`. Updating containers needs `MEDIADASH_PASSWORD` and the
Docker socket. Orama Server updates itself through a short-lived helper container.

TV app updates need `TV_IP` (the TV in Developer Mode) and your Apps2Samsung certificate,
kept in `<data>/installer/`. Orama finds it on port 8790 as before.

# Watchlist sync

**Your household's Plex Watchlists, downloaded automatically.** Add a movie or show to
your Plex Watchlist, from any Plex app, and it goes to Radarr or Sonarr.
It reads your friends' Watchlists too, so everyone you share your server with can
request things without a separate request app.

<p align="center">
  <img src="assets/screenshot.png" alt="The watchlist-sync web page: the Plex account, the last sync, users and their Watchlists, and recent requests" width="760">
</p>

## What it does

- **Movies go to Radarr, shows go to Sonarr**, matched by TMDB and TVDB ids, and are
  searched for right away.
- **Everyone's Watchlist counts:** the server owner's and every friend's you share the
  server with. Each title is tagged `plex-<username>` in Sonarr/Radarr, so you can see
  who asked for what.
- **Anime is handled as anime:** pick an anime root folder (and profile) under Settings,
  and shows TheTVDB calls anime, or Japanese animation, are added there as series type
  *Anime* (absolute episode numbers). Leave it empty and anime is added like any show.
- **Bazarr is told** to pick up new titles straight away, so subtitles follow.
- **Dry run first:** it starts by only showing what it *would* add.
- **Excluding someone** stops syncing their Watchlist and can remove what only they
  asked for, files included. A preview shows exactly what goes and what stays.
- **A web page** for signing in with Plex, settings, users and request history.
  No config files to edit.

## Running the sync on its own

You need Docker, a Plex Media Server you own, and Sonarr and/or Radarr.

1. Save this as `docker-compose.yml` in a new folder (or download the one in this repo):

   ```yaml
   services:
     watchlist-sync:
       image: ghcr.io/darkangelgr/watchlist-sync:latest
       container_name: watchlist-sync
       network_mode: host
       environment:
         - TZ=Europe/London        # your time zone
         - UI_PASSWORD=change-me   # optional, recommended
       volumes:
         - ./data:/data
       restart: unless-stopped
   ```

2. Start it: `docker compose up -d`
3. Open **http://&lt;server-ip&gt;:8789** and:
   1. **Sign in with Plex** using the account that owns the server. Plex's own sign-in
      page opens; there's nothing to copy.
   2. Under **Settings**, enter the Sonarr address and API key (Sonarr › Settings ›
      General), press **Test & load options**, and pick a quality profile and root folder.
      Do the same for Radarr, and for Bazarr if you use it.
   3. **Save.** The first syncs are a dry run: **Last sync** shows what would be added.
      When it looks right, untick **Dry run** and save again.

To update: `docker compose pull && docker compose up -d`. Settings, the Plex sign-in
and the history live in `./data`.

<details>
<summary><b>Build it yourself instead</b></summary>

```sh
git clone https://github.com/DarkAngelGR/watchlist-sync.git
cd watchlist-sync
# in docker-compose.yml, swap the image: line for build: .
docker compose up -d --build
```
</details>

## Friends' Watchlists

Only your sign-in is needed. Each friend has to allow it once: in their Plex account,
**Privacy › Watchlist** set to **Friends** (or wider). The **Users** card shows whose
Watchlist is readable and who still has theirs private. Managed Plex Home users (no
plex.tv account of their own) don't have a Watchlist Plex shares, so they aren't covered.

## Excluding someone

In **Users**, press **Exclude…** next to a name. Before anything happens, you see:

- **What gets deleted:** titles watchlist-sync added *only* because of that person,
  removed from Sonarr/Radarr **with their files**. Untick this to just stop syncing
  their Watchlist.
- **What's kept:** titles someone else also asked for or still has listed. Those stay;
  only the person's `plex-<name>` tag is removed.

Anything that was in your library before watchlist-sync is never deleted. In dry run
nothing is deleted at all. **Include again** brings the person back on the next sync.

## How it behaves

- Each title is handled once. Delete something from Sonarr/Radarr yourself and it
  isn't re-added just because it's still on a Watchlist.
- Removing a title from a Watchlist doesn't delete it from your library.
- It syncs every 15 minutes by default (Settings), or right away with **Sync now**.
- The Plex sign-in is refreshed daily so it doesn't expire.
- Settings can be pre-filled with environment variables (see `.env.example`); values
  saved on the web page win.

## Security

The web page stores your Plex sign-in and Sonarr/Radarr API keys, so:

- Set `UI_PASSWORD` (any username works in the browser prompt).
- Keep it on your home network or a VPN such as Tailscale. Don't expose it to the
  internet through a reverse proxy or tunnel.

It also refuses requests that another website could make through your browser:
commands are only accepted from its own page, the Orama TV app, or other devices on
your network, and it only answers to an IP address or a local name (`nas.local`,
a bare machine name, a Tailscale `*.ts.net` name). If you reach it by another name,
for example behind a reverse proxy, add that name to `ALLOWED_HOSTS`
(comma-separated) in the environment.

Your API keys never leave the container: the web page and the TV endpoints below only
ever see what's been requested, not the keys.

## Orama (Samsung TV)

[Orama](https://github.com/DarkAngelGR/orama), a Plex and Jellyfin player for Samsung TVs,
works with watchlist-sync. On Plex, you can search Plex's whole catalog from the TV and add
titles to your Watchlist, and watchlist-sync syncs right away. On Jellyfin, which has no
Watchlist, Orama searches through Sonarr/Radarr instead and **Request** adds the title
directly, with the same profiles, folders and anime handling as a Watchlist add.
Orama's **Discover** rows (trending, recommended for you) come from Radarr's Discover; add a free
[TMDB API key](https://www.themoviedb.org/settings/api) under Settings to include trending TV shows. Orama also shows download progress,
lists the parts of a collection you don't have yet, shows upcoming episodes, searches
Bazarr for subtitles, and can delete titles from Sonarr/Radarr. It finds watchlist-sync by itself when it runs on the same machine as
Plex or Jellyfin (port 8789).

<details>
<summary><b>TV endpoints</b></summary>

These are the only endpoints that allow cross-origin calls. They need `UI_PASSWORD`
(HTTP Basic, any username) when it's set.

| Endpoint | What it does |
| --- | --- |
| `GET /api/tv` | Version, what's set up, and whether Sonarr/Radarr answer |
| `POST /api/tv/sync` | Start a sync now (after a Watchlist add) |
| `GET /api/tv/queue` | What Sonarr/Radarr are downloading, per movie/series, plus recent adds still being searched for |
| `GET /api/tv/collection?tmdb=ID` | The Radarr collection a movie belongs to, every part, and which ones you have |
| `GET /api/tv/upcoming?days=7` | Episodes airing in the next days (Sonarr's calendar) that aren't downloaded yet |
| `GET /api/tv/subtitles?kind=movie\|episode&…` | Search Bazarr for subtitles (`tmdb`/`imdb` for a movie; the show's `tvdb` plus `season` and `episode`; optional `lang`). Returns a token and the results |
| `POST /api/tv/subtitles/download` | `{token, index}`: download one of those results through Bazarr. Bazarr's own subtitle ids never leave watchlist-sync |
| `GET /api/tv/lookup?kind=movie\|show&q=TEXT` (or `&id=ID`) | Search Radarr/Sonarr's lookup (or one TMDB/TVDB id). Each result says whether it's already there: `have`, `requested` or `none` |
| `GET /api/tv/discover` | Titles you don't have yet: trending, popular and recommended movies (Radarr's Discover, no key needed) and trending shows (with a TMDB key) |
| `POST /api/tv/request` | `{kind: "movie"\|"show", tmdb\|tvdb, user}`: add it to Radarr/Sonarr like a Watchlist add would (nothing in dry run) |
| `POST /api/tv/delete` | `{kind: "movie"\|"show", tmdb\|tvdb, imdb}`: delete from Radarr/Sonarr with files (nothing in dry run). It isn't re-added while it stays on the same Watchlists; adding it to a Watchlist again later brings it back |
</details>

## For developers

`app.py` is the whole service (Python standard library plus `requests`); `ui.html` is the
web page; `python test_app.py` runs the tests against fake Plex/Sonarr/Radarr servers.
Releases are published when `VERSION` in `app.py` changes on `main`.

## License

[MIT](LICENSE) © 2026 Aggelos (DarkAngelGR).

Not affiliated with or endorsed by Plex, Inc., Sonarr, Radarr or Bazarr.
