# Orama local recommendations — Minisforum UN1250

An optional local AI ranking service for Orama / watchlist-sync, using media
history and metadata only. No video files are transferred or analyzed.

## Capabilities and limits

- One explicitly configured Plex account or Jellyfin user **per AI instance**.
  The NAS routes the active Orama profile UUID to a configured instance.
  Additional household users need separate instances with their own scoped credentials.
  Profile IDs supplied by clients are not cryptographically verified: use a strong
  UI_PASSWORD and private LAN, and do not expose this API to untrusted clients.
- Plex: server playback-history entries, grouped into movies and shows;
  rewatching the same episode is stronger than simply watching more episodes.
  Plex history records a server-counted play, not necessarily a genuine
  full completion or explicit like.
- Jellyfin: requires Played=true or an explicit >=90% progress indicator before
  PlayCount contributes to preference strength. A play count by itself is not completion.
- Immediate genre/plot/title ranking after history synchronization.
- Background Ollama Qwen3 embedding cache in SQLite. The first semantic
  indexing pass takes longer on CPU; ranking does not block on embeddings.
- Offline Ollama still allows a metadata-based ranking; offline AI service
  results in watchlist-sync's unchanged Radarr/TMDB Discover results.
- Feedback choices (more/less/hide/clear) are stored in local SQLite; the UI
  shows a short reason for suggested matches and controls exploration 0-100%.
- Hidden Gems uses well-rated non-trending candidates; when a TMDB API key is
  configured, an additional page of TMDB movie discovery broadens the pool.
  Series beyond existing trending results remain a follow-up enhancement.

## 1. Install on the UN1250 (Linux + Docker + Compose)

~~~bash
git clone --branch feat/local-ai-recommendations https://github.com/DarkAngelGR/watchlist-sync.git
cd watchlist-sync/recommendations
cp .env.example .env
openssl rand -hex 32
~~~

Put the generated secret in the API_KEY field of .env. Set AI_BIND_IP to
the Minisforum's fixed LAN IP, not 0.0.0.0. Select a data source.

### Plex source

Set MEDIA_SOURCE=plex, PLEX_URL to http://NAS_IP:32400, PLEX_TOKEN to a
token authorized to read your playback history, and PLEX_ACCOUNT_ID to the
numeric accountID corresponding to your own profile. This ID is required
to avoid accidentally using every user on an admin account.

To inspect recent account IDs locally (do not share or commit your token):

~~~bash
read -rs PLEX_TOKEN
curl -fsS -H "Accept: application/json" -H "X-Plex-Token: $PLEX_TOKEN" \
  "http://NAS_IP:32400/status/sessions/history/all?sort=viewedAt:desc" \
  | python3 -m json.tool | less
unset PLEX_TOKEN
~~~

Find a known recent entry you watched and its accountID; verify it is your
profile if more than one account appears in the history.

### Jellyfin source

Set MEDIA_SOURCE=jellyfin, JELLYFIN_URL to http://NAS_IP:8096,
JELLYFIN_API_KEY to a key allowed to read playback history, and
JELLYFIN_USER_ID to the one user's UUID.

### Start and validate

~~~bash
docker compose up -d --build
docker compose exec ollama ollama pull qwen3-embedding:0.6b
docker compose logs --tail=80 recommender
curl http://MINISFORUM_IP:8788/health
~~~

The health JSON should eventually report ok true and history_items > 0.
An initial refresh may take some time. Review logs if it reports an error.
This version needs ONLY the embedding model, not the optional 4B chat model.

Only port 8788 is published on the Minisforum's LAN IP. Ollama is not
exposed to the host network. Allow TCP 8788 only from your NAS in the
Minisforum's firewall; never forward that port from the internet.

## 2. Configure watchlist-sync on your NAS

Build the watchlist-sync server from this feature branch, or use a future
released version that includes the integration. The current published
latest container will NOT have unmerged code. Preserve the current
watchlist-sync data volume, settings and image backup during upgrades.

Add these variables to your *existing* watchlist-sync Compose service
environment (keep all existing variables and settings):

~~~yaml
environment:
  RECOMMENDER_URL: "http://MINISFORUM_IP:8788"
  RECOMMENDER_API_KEY: "THE_SAME_SECRET_AS_API_KEY"
  RECOMMENDER_PROFILE_UUID: "CURRENT_ORAMA_PROFILE_UUID"
  UI_PASSWORD: "A_STRONG_SEPARATE_NAS_UI_PASSWORD"
  # TMDB_API_KEY: "..."  # optional, expands movie discovery
~~~

If UI_PASSWORD is not set, only generic Discover is returned and taste feedback
is disabled to avoid exposing viewing preferences. Use the password on Orama's
Settings -> Downloads screen as well.

If watchlist-sync uses host networking, the NAS can reach the UN1250 using
its LAN IP. Never commit these credentials. Rebuild and redeploy the
feature-branch watchlist-sync service only when you have verified your
existing Compose paths and data-volume backup.

Check the service on the NAS:

~~~bash
curl -fsS -H "Authorization: Bearer YOUR_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"kind":"movie","tmdb":1,"title":"Example","genres":["Science Fiction"]}]}' \
  http://MINISFORUM_IP:8788/v1/rank
~~~

A personalized true result means the configured viewer's history has been
loaded. Embedding caches improve results as they populate asynchronously.

Install the companion Orama PR on a staging TV (its changes are not in main).
The Orama feature-branch client passes its active profile UUID and diversity
level to the TV discovery endpoint. The server returns personalized rows only
for configured profiles. When history is available, Home shows "For you · Based
on your viewing" and, when there are eligible items, "Hidden gems for you".
If the AI is unavailable, the original "Recommended for you" row remains.

The TV adds "Your taste" on external catalog item details with More like this,
Less like this, Hide and Clear. Feedback uses the configured watchlist-sync
UI_PASSWORD; setting the password in Orama Settings -> Downloads is necessary.
A "Why recommended" note is shown on items ranked by the local engine.
Settings -> AI discovery cycles exploration from 0 to 100%.

## Troubleshooting / rollback

- Revert the NAS variables RECOMMENDER_URL, RECOMMENDER_API_KEY and RECOMMENDER_PROFILE_UUID
  and restart watchlist-sync to restore the original Radarr/TMDB discovery.
- If Ollama cannot respond, genre/title similarity still works. Use
  docker compose logs -f recommender to inspect embedding failures.
- If history_items is zero, verify source URL, chosen account ID, credentials
  and NAS connectivity. One AI instance only reads one selected user.
- Run offline tests with:
  python -m unittest discover -s recommendations -p 'test_*.py' -v
  python test_app.py
- All AI and history data stays local. No cloud LLM or external API calls
  are required by the recommender.

## Multiple profiles (optional)

For multiple Plex/Jellyfin viewers, run one isolated recommender container for
**each** user, with a separate .env source account ID, data volume, API key and
LAN port. Bind each to a specific profile UUID using a NAS-only JSON file,
for example /data/recommender-profiles.json in the existing watchlist-sync data
volume (never commit this file):

~~~json
{
  "ORAMA_PROFILE_UUID_A": {"url": "http://MINISFORUM_IP:8788", "api_key": "SECRET_A"},
  "ORAMA_PROFILE_UUID_B": {"url": "http://MINISFORUM_IP:8787", "api_key": "SECRET_B"}
}
~~~

Set RECOMMENDER_PROFILES_FILE=/data/recommender-profiles.json on the NAS. When
this is set, it overrides the single-profile variables; unlisted profiles fall
back to generic Discover. This is explicit routing, not verified identity.
You can find the active Orama profile UUID in its Settings -> AI profile ID
row on the companion Orama feature branch. Require the UI password and limit
access to the NAS service to trusted devices; if household-level isolation
requires strict access controls, do not expose profile switching to others
until authenticated profile verification is implemented.
