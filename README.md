# watchlist-sync

Reads the Plex watchlist of the server owner **and every friend you share the server with**, then:

- adds **shows → Sonarr** (matched by TVDB id, searched immediately)
- adds **movies → Radarr** (matched by TMDB id) — optional
- triggers Bazarr's *Sync with Sonarr/Radarr* task so new titles appear there right away

Every title is tagged `plex-<username>` so you can see who asked for what.

## Install on the NAS

```bash
cd /volume2/docker/mediastack
git clone https://github.com/<you>/watchlist-sync.git
cd watchlist-sync
docker compose up -d --build
```

To update later: `git pull && docker compose up -d --build`.

Open **http://<nas-ip>:8789** and:

1. **Sign in with Plex** — use the account that owns the server. Plex's own login page opens; nothing to copy.
2. Under **Settings**, enter the Sonarr URL + API key (Sonarr → Settings → General), press **Test & load options**, pick a quality profile and root folder. Same for Radarr and Bazarr if you want them.
3. **Save settings.** It starts in **dry run** — the "Last sync" card shows what it *would* add. When it looks right, untick *Dry run* and save.

Settings, login and history live in `watchlist-sync/data/`.

## Friends' watchlists

Only your login is needed. Each friend must set their Plex account **Privacy → Watchlist** to **Friends only** (or wider). The **Users** card shows who is readable and who is private. Managed Plex Home users (no own plex.tv account) aren't covered.

## Excluding a user

In the **Users** card, click **Exclude…** next to someone. Before anything happens you see:

- **What gets deleted:** titles watchlist-sync added *only* because of that user. They're removed from Sonarr/Radarr **together with their downloaded files**. You can untick this to just stop syncing their watchlist.
- **What's kept:** titles someone else also requested or still has on their watchlist. Those stay; only the user's `plex-<name>` tag is removed.

Anything that was already in your library before watchlist-sync is never deleted. With **Dry run** on, nothing is deleted, the result just shows what would have been. **Include again** brings the user back; their titles are re-added on the next sync.

## TV requests (FlexPlex)

[FlexPlex](https://github.com/DarkAngelGR/samsucks)'s **Lucky Draw** can suggest movies you don't have and add them on the spot. It finds watchlist-sync by itself when it runs on the same machine as Plex (port 8789); otherwise set the address in FlexPlex's Settings.

- `GET /api/tv/suggestions`: Radarr's Discover list (TMDB recommendations based on your library, plus your import lists) without what's already in Radarr, excluded, or already requested. Cached for 30 minutes.
- `POST /api/tv/request` with `{"tmdbId": 438631}`: adds the movie to Radarr right away, with the same quality profile, root folder and `plex-<owner>` tag as watchlist additions, and starts the search. **Dry run** is respected. The title is recorded like any other, so the watchlist sync won't add it twice.
- `GET /api/tv`: what's set up (Radarr, Sonarr, dry run), for the TV's status line.

Shows aren't suggested: Sonarr has no Discover list. Your Sonarr/Radarr API keys never leave watchlist-sync. Only these endpoints allow cross-origin calls, and they need `UI_PASSWORD` when it's set (enter it in FlexPlex's Settings).

## Security

The UI has no login by default and stores your Plex sign-in, so keep it on your LAN / Tailscale and don't route it through the Cloudflare tunnel. To add a password, set `UI_PASSWORD=...` in docker-compose (any username works).

## Behaviour notes

- Each title is handled once. If you delete something from Sonarr later, it won't be re-added just because it's still on someone's watchlist.
- Removing an item from a watchlist doesn't delete it from Sonarr/Radarr.
- The Plex login is refreshed daily so it doesn't expire.
- Settings can also be pre-filled with env vars (see `.env.example`); values saved in the UI take priority.
