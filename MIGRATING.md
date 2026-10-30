# Moving to Orama Server

From separate **watchlist-sync**, **mediadash**, **Orama installer** (flexplex-installer)
and **Tdarr** containers to one `orama-server`. Nothing is deleted until the new one has
been checked, and every step says how to go back.

Commands assume you're in your compose folder (e.g. `/volume2/docker/mediastack`).

## 0. Before you start

- Have a backup of the compose folder (or at least copy the app folders below).
- Note how Sonarr/Radarr mount your media, e.g. `- /volume1/data:/data`. Orama Server mounts
  it **the same way**, so the paths in their webhooks match and no path mapping is needed.
- For Dolby Vision 5 only: the render group's number for the Intel graphics,
  `getent group render | cut -d: -f3`

## 1. Add Orama Server to the compose file

Add this service, keeping the old ones for now. Use your own values for the settings the
old containers had:

```yaml
  orama-server:
    image: ghcr.io/darkangelgr/orama-server:latest
    container_name: orama-server
    network_mode: host
    environment:
      - TZ=Europe/Athens
      - DATA_DIR=/appdata
      - UI_PASSWORD=...            # what watchlist-sync / the installer used, if anything
      - MEDIADASH_PASSWORD=...     # what mediadash used
      - TV_IP=...                  # what the installer used (and its other settings)
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "105"                      # the render group's number from step 0
    volumes:
      - ./orama-server:/appdata
      - /volume1/data:/data        # exactly like Sonarr/Radarr
      - ./mediadash/config.yaml:/config/config.yaml:ro
      - /var/run/docker.sock:/var/run/docker.sock
      - /volume1:/volume1:ro       # dashboard: disk space
      - /volume2:/volume2:ro       # dashboard: disk space and the compose file for updates
    restart: unless-stopped
```

## 2. Swap (the old containers are only stopped)

They use the same ports, so stop them first, copy their data, then start the new one:

```
sudo docker compose stop watchlist-sync mediadash flexplex-installer
sudo mkdir -p orama-server/installer
sudo cp -a watchlist-sync/data/. orama-server/
sudo cp -a <installer data folder>/. orama-server/installer/
sudo docker compose up -d orama-server
```

`<installer data folder>` is the folder mounted at `/data` in the installer's compose entry;
it holds your TV certificate.

## 3. Check

- `http://<nas>:8789`: signed in to Plex, same settings, **Sync now** works.
- `http://<nas>:8765`: every service card, CPU/RAM, the Updates panel.
- `http://<nas>:8790`: the TV and the installed Orama version.
- On the TV: Orama › Settings › Downloads (watchlist-sync connected) and › Updates.
- `sudo docker logs orama-server --tail 50` shows no errors.

**Going back:**
`sudo docker compose stop orama-server && sudo docker compose start watchlist-sync mediadash flexplex-installer`

## 4. The converter instead of Tdarr

1. Settings › **Converter**: media folder `/data/media`, DTS → E-AC3 on, bitrate 1536,
   *Keep the original DTS track* as you like. Leave **On** unticked for now.
2. **Scan library now**: with the converter off it only lists what it *would* convert.
3. Tick **On**, then put one movie with DTS into *Convert one file*. When it's done, play it
   on the TV: sound through the soundbar, picture fine. Try a show episode too.
4. Add the webhook in Sonarr and Radarr: Settings › Connect › + › Webhook, *On Import* and
   *On Upgrade*, URL `http://<nas>:8789/api/hooks/arr` (the UI password as the password, if
   you set one). **Test** should succeed.
5. Stop Tdarr: `sudo docker compose stop tdarr` (going back: `start tdarr`, and untick On).

Dolby Vision 7 → 8.1 is safe to turn on (no re-encoding). Dolby Vision 5 → HDR10:
**Check the graphics** first, then try one file and watch it before leaving it on.

## 5. A week later

When everything has run fine for a while, remove the old containers:

```
sudo docker compose rm -sf watchlist-sync mediadash flexplex-installer tdarr
```

then delete their entries from the compose file. Keep `mediadash/config.yaml` (Orama Server
uses it). Delete the old folders only after that.
