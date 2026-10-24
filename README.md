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

## FlexPlex (Samsung TV)

[FlexPlex](https://github.com/DarkAngelGR/samsucks) can search Plex's catalog for movies and shows you don't have and add them to your Plex Watchlist. It then asks watchlist-sync to sync right away, so the title reaches Sonarr/Radarr in seconds instead of on the next cycle. FlexPlex finds watchlist-sync by itself when it runs on the same machine as Plex (port 8789); otherwise set the address in FlexPlex's Settings › Downloads.

- `POST /api/tv/sync`: start a sync now.
- `GET /api/tv`: what's set up and whether Sonarr/Radarr answer (`radarr_ok`, `sonarr_ok`), for the TV's status line.

Only these endpoints allow cross-origin calls, and they need `UI_PASSWORD` when it's set (enter it in FlexPlex's Settings). Your Sonarr/Radarr API keys never leave watchlist-sync.

## Security

The UI has no login by default and stores your Plex sign-in, so keep it on your LAN / Tailscale and don't route it through the Cloudflare tunnel. To add a password, set `UI_PASSWORD=...` in docker-compose (any username works).

## Behaviour notes

- Each title is handled once. If you delete something from Sonarr later, it won't be re-added just because it's still on someone's watchlist.
- Removing an item from a watchlist doesn't delete it from Sonarr/Radarr.
- The Plex login is refreshed daily so it doesn't expire.
- Settings can also be pre-filled with env vars (see `.env.example`); values saved in the UI take priority.
