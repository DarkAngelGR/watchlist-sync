# watchlist-sync

**Your household's Plex Watchlists, downloaded automatically.** Add a movie or show to
your Plex Watchlist, from any Plex app, and watchlist-sync hands it to Radarr or Sonarr.
It reads your friends' Watchlists too, so everyone you share your server with can
request things without a separate request app.

[![Latest release](https://img.shields.io/github/v/release/DarkAngelGR/watchlist-sync?label=release)](https://github.com/DarkAngelGR/watchlist-sync/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

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

## Getting started

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
| `POST /api/tv/recommend` | Rank a shortlist from Orama and return up to three title IDs and reasons. Requires `UI_PASSWORD` and a configured AI model |
| `POST /api/tv/request` | `{kind: "movie"\|"show", tmdb\|tvdb, user}`: add it to Radarr/Sonarr like a Watchlist add would (nothing in dry run) |
| `POST /api/tv/search-again` | Search for missing files on a tracked title using its TMDB or TVDB ID. Requires `UI_PASSWORD` |
| `POST /api/tv/auto-fetch` | Request one missing movie using `{tmdb, triggerId}` and the household’s auto-fetch policy |
| `GET /api/tv/auto-fetch` | Check the outcome for an automatic request using `tmdb` and `triggerId` |
| `POST /api/tv/log` | Report TV app errors, warnings, and performance entries (`{"level": "error"\|"warn"\|"perf", "message", "where", "version", "device"}`). Bounded (last 200 per category) and rate limited (30/min per IP, excess dropped with 204), secrets redacted, displayed in admin UI |
| `POST /api/tv/delete` | `{kind: "movie"\|"show", tmdb\|tvdb, imdb}`: delete from Radarr/Sonarr with files (nothing in dry run). It isn't re-added while it stays on the same Watchlists; adding it to a Watchlist again later brings it back |
</details>

### Optional AI ranking for Orama

Set `UI_PASSWORD`, `RECOMMEND_URL` (the full URL of a chat completions endpoint), and `RECOMMEND_MODEL`
in the container environment. Set `RECOMMEND_API_KEY` when the endpoint requires a bearer token.
Choose a model endpoint reachable from the container. With a recent Orama build, add the companion
password under **Settings › Downloads**, then enable **AI recommendations** for the current viewer.

Orama sends preferred genres, the just-watched title when available, and a shortlist of playable
or requestable titles. A hosted model provider receives that selection. The recommendation endpoint
ranks titles from the shortlist and returns picks and reasons. Plex Watchlist sync operates
independently of the model.

### Optional movie auto-fetch

Set a `UI_PASSWORD` and enable **Allow movie auto-fetch** in Watchlist Sync’s settings. Each
viewer can then opt in under **Settings › Auto-fetch movies** on the TV. Watchlist Sync accepts
up to one automatic movie request per rolling seven days for the household. It checks the
configured Radarr library folder for at least 50 GiB free by default. Adjust the minimum in the
companion’s settings.

Auto-fetch uses the configured Radarr quality profile and search-on-add policy. Turn off
**Allow movie auto-fetch** to disable automatic requests across the household. Dry run also
prevents automatic writes. Manual movie and series requests are available from the TV.

## Notifications

Optional. When a title is added, a download fails or stalls, or Sonarr/Radarr imports a
file, watchlist-sync can post to [ntfy](https://ntfy.sh) and a Discord webhook. Set them
under **Settings › Notifications**, or with `NTFY_URL`, `NTFY_TOKEN`, `DISCORD_WEBHOOK`,
`NOTIFY_ADDED`, `NOTIFY_DOWNLOADED`, `NOTIFY_FAILED` and `NOTIFY_CLEANUP` (see `.env.example`). Values saved
on the page win. Each channel has a **Send test** button.

`NTFY_URL` is the topic URL, for example `https://ntfy.sh/mytopic` or a self-hosted topic.
`NTFY_TOKEN` is the optional bearer token for a protected topic. `DISCORD_WEBHOOK` is the
webhook URL Discord gives you. Both secrets are stored like the Sonarr API key: the page
only shows that one is set, and a blank field keeps the saved value.

- **Added** is a sync or a TV request that put a title in Sonarr/Radarr, including who
  requested it. A poster is attached when the channel can show one.
- **Downloaded** is read from Sonarr and Radarr history (`downloadFolderImported`) each
  sync. The first run remembers the current history and does not replay it. Episodes of
  one series in the same sync are a single message.
- **Failed** is a title that could not be added, or a download that is failed or stalled
  in the queue. Each title is reported once for that failure, not on every sync.
- **Cleanup** is the disk-cleanup warning (`Will delete in 2 days: …`) and the note of
  how much space a delete freed. Turn it off with `NOTIFY_CLEANUP`.

Dry run writes the message to the log instead of sending it. Messages go out in the
background with a short timeout and a few retries. A delivery problem does not stop a sync,
and a restart does not send the same one again.

## Disk cleanup

Separate from the daily database tidy. Off until you turn a rule on under
**Settings › Disk cleanup**. Once a day, watchlist-sync builds a list of titles it
added and shows them on the page (title, size, why, last watched) with a **Keep**
button. It sends a cleanup notification, waits the grace period (2 days unless you
change it), then deletes.

- **Watched:** a movie, or an ended show whose every episode has been watched, once
  that has been true for N days and the title is at least N days old.
- **Unplayed:** a title nobody has played, N days after it was added.
- **Seasons:** a season of a show that is still airing, once that season is watched.
  Sonarr unmonitors the season and deletes its episode files. The rest of the show stays.

Watch state comes from the owner's Plex server: active sessions, and
`/status/sessions/history/all` matched to accounts. When Plex does not say who
watched what, a title counts as watched once **you** have watched it, and the page
says so. Library `viewCount` fills in plays the history feed does not list.

It will not delete anything that is still on a watchlist, playing in Plex, added
inside the rule's N days, tagged in Sonarr/Radarr with the keep tag (default
`keep`), or on the Keep list. Titles that were already in your library are left
alone. A delete is the same Arr delete-with-files path as a TV delete, and the
title is marked deleted so it is not downloaded again. Dry run only logs it.
The page shows the space that was freed.

## Uptime

Docker's healthcheck calls `GET /health`. That stays a fast, unauthenticated
`{"ok": true}` and does not ask Plex, Sonarr, Radarr, or Bazarr anything.

`GET /health/full` is for an uptime monitor. It is unauthenticated, rate-limited
(30 requests a minute per address), and caches each service check for about 30
seconds so a monitor cannot hammer Plex or the Arr apps. The JSON has no secrets,
URLs, or hostnames. A service is listed only when it is configured. `status` is
`ok`, `degraded`, or `down`. The HTTP status is 200 for `ok` and 503 otherwise.

- `down`: every configured service failed its check.
- `degraded`: one configured service failed, the last sync reported errors, or the
  last successful sync is older than three sync intervals. Change that multiple
  with **Stale sync after N intervals** in Settings, or `HEALTH_STALE_FACTOR`.

In [Uptime Kuma](https://github.com/louislam/uptime-kuma), add an **HTTP(s)** monitor:

1. URL: `http://<server-ip>:8789/health/full`
2. Heartbeat interval: 60 seconds (a shorter interval just hits the cache).
3. Accepted status codes: `200`. A 503 marks the check down.
4. Leave the monitor on your own network. The endpoint has no password.

Use `/health` for Docker. Use `/health/full` when you want to know that a sync
actually succeeded.

## For developers

`app.py` is the whole service (Python standard library plus `requests`); `ui.html` is the
web page; `python test_app.py` runs the tests against fake Plex/Sonarr/Radarr servers.
Releases are published when `VERSION` in `app.py` changes on `main`.

## License

[MIT](LICENSE) © 2026 Aggelos (DarkAngelGR).

Not affiliated with or endorsed by Plex, Inc., Sonarr, Radarr or Bazarr.
