#!/usr/bin/env python3
"""
watchlist-sync
--------------
Sign in with your Plex account in the web UI, point it at Sonarr (and
optionally Radarr / Bazarr), and it will keep polling the watchlists of you
and every friend you share the server with:
  * shows  -> Sonarr (by TVDB id)
  * movies -> Radarr (by TMDB id)
  * Bazarr is told to re-sync so new titles appear there for subtitles

Friends' watchlists are read through Plex's community GraphQL API with the
owner's token; each friend's Plex privacy setting for "Watchlist" must be
"Friends only" (or wider).
"""
import base64
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlencode, urlparse, parse_qs

import requests

log = logging.getLogger("watchlist-sync")
HERE = os.path.dirname(os.path.abspath(__file__))
PLEXTV = "https://plex.tv"
PRODUCT = "watchlist-sync"
VERSION = "1.3.0"


# --------------------------------------------------------------------------- settings
def as_bool(v):
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


# key: (env var, default, type). Env vars are defaults; anything saved in the UI wins.
FIELDS = {
    "plex_token": ("PLEX_TOKEN", "", str),
    "interval_min": ("SYNC_INTERVAL_MINUTES", 15, int),
    "include_owner": ("INCLUDE_OWNER", True, bool),
    "include_friends": ("INCLUDE_FRIENDS", True, bool),
    "only_server_users": ("ONLY_SERVER_USERS", True, bool),
    "user_allow": ("USER_ALLOWLIST", "", str),
    "user_block": ("USER_BLOCKLIST", "", str),
    "tag_by_user": ("TAG_BY_USER", True, bool),
    "dry_run": ("DRY_RUN", True, bool),
    "sonarr_url": ("SONARR_URL", "", str),
    "sonarr_api_key": ("SONARR_API_KEY", "", str),
    "sonarr_quality_profile": ("SONARR_QUALITY_PROFILE", "", str),
    "sonarr_root_folder": ("SONARR_ROOT_FOLDER", "", str),
    "sonarr_monitor": ("SONARR_MONITOR", "all", str),
    "sonarr_series_type": ("SONARR_SERIES_TYPE", "standard", str),
    # Anime gets its own profile/folder and type Anime; off while the folder is empty
    "sonarr_anime_quality_profile": ("SONARR_ANIME_QUALITY_PROFILE", "", str),
    "sonarr_anime_root_folder": ("SONARR_ANIME_ROOT_FOLDER", "", str),
    "sonarr_search": ("SONARR_SEARCH_ON_ADD", True, bool),
    "radarr_url": ("RADARR_URL", "", str),
    "radarr_api_key": ("RADARR_API_KEY", "", str),
    "radarr_quality_profile": ("RADARR_QUALITY_PROFILE", "", str),
    "radarr_root_folder": ("RADARR_ROOT_FOLDER", "", str),
    "radarr_min_availability": ("RADARR_MINIMUM_AVAILABILITY", "released", str),
    "radarr_search": ("RADARR_SEARCH_ON_ADD", True, bool),
    "bazarr_url": ("BAZARR_URL", "", str),
    "bazarr_api_key": ("BAZARR_API_KEY", "", str),
}
SECRETS = ("plex_token", "sonarr_api_key", "radarr_api_key", "bazarr_api_key")


def cast(typ, v):
    if typ is bool:
        return as_bool(v)
    if typ is int:
        return max(1, int(v))
    return str(v if v is not None else "").strip()


class Settings:
    def __init__(self, state):
        self.state = state

    def _saved(self):
        return json.loads(self.state.get("settings", "{}"))

    def all(self):
        d = {}
        for k, (env_name, default, typ) in FIELDS.items():
            raw = os.environ.get(env_name)
            d[k] = cast(typ, raw) if raw not in (None, "") else default
        d.update(self._saved())
        return d

    def update(self, changes):
        saved = self._saved()
        for k, v in changes.items():
            if k not in FIELDS:
                continue
            if k in SECRETS and v in (None, "") and k != "plex_token":
                continue  # blank secret in the form = keep the existing one
            saved[k] = cast(FIELDS[k][2], v)
        self.state.set("settings", json.dumps(saved))

    def public(self):
        """Settings safe to send to the browser (secrets replaced by 'is set' flags)."""
        d = self.all()
        for k in SECRETS:
            d[k + "_set"] = bool(d.pop(k))
        return d


class ArrConfig:
    def __init__(self, s, prefix):
        self.url = s[f"{prefix}_url"]
        self.api_key = s[f"{prefix}_api_key"]
        self.quality_profile = s[f"{prefix}_quality_profile"]
        self.root_folder = s[f"{prefix}_root_folder"]
        self.search = s[f"{prefix}_search"]
        self.enabled = bool(self.url and self.api_key)


class Config:
    def __init__(self, s):
        self.plex_token = s["plex_token"]
        self.interval_min = s["interval_min"]
        self.include_owner = s["include_owner"]
        self.include_friends = s["include_friends"]
        self.only_server_users = s["only_server_users"]
        self.user_allow = {x.strip().lower() for x in s["user_allow"].split(",") if x.strip()}
        self.user_block = {x.strip().lower() for x in s["user_block"].split(",") if x.strip()}
        self.tag_by_user = s["tag_by_user"]
        self.dry_run = s["dry_run"]
        self.sonarr = ArrConfig(s, "sonarr")
        self.sonarr_monitor = s["sonarr_monitor"]
        self.sonarr_series_type = s["sonarr_series_type"]
        self.sonarr_anime_profile = s["sonarr_anime_quality_profile"]
        self.sonarr_anime_root = s["sonarr_anime_root_folder"]
        self.radarr = ArrConfig(s, "radarr")
        self.radarr_min_availability = s["radarr_min_availability"]
        self.bazarr_url = s["bazarr_url"]
        self.bazarr_key = s["bazarr_api_key"]


# --------------------------------------------------------------------------- state
class State:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS guid_cache (rating_key TEXT PRIMARY KEY, guids TEXT)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS handled (service TEXT, ext_id INTEGER, title TEXT, users TEXT, "
                "result TEXT, ts INTEGER, PRIMARY KEY (service, ext_id))"
            )
            # who has each title on their watchlist right now (refreshed every sync)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS wants (service TEXT, ext_id INTEGER, user TEXT, "
                "PRIMARY KEY (service, ext_id, user))"
            )

    def get(self, k, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k, v):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, v))

    def cached_guids(self, rk):
        with self.lock:
            row = self.db.execute("SELECT guids FROM guid_cache WHERE rating_key=?", (rk,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_guids(self, rk, guids):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO guid_cache VALUES (?,?)", (rk, json.dumps(guids)))

    def is_handled(self, service, ext_id):
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM handled WHERE service=? AND ext_id=?", (service, ext_id)
            ).fetchone() is not None

    def mark(self, service, ext_id, title, users, result):
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO handled VALUES (?,?,?,?,?,?)",
                (service, ext_id, title, ",".join(sorted(users)), result, int(time.time())),
            )

    def set_wants(self, service, grouped):
        with self.lock, self.db:
            self.db.execute("DELETE FROM wants WHERE service=?", (service,))
            self.db.executemany(
                "INSERT OR IGNORE INTO wants VALUES (?,?,?)",
                [(service, ext_id, u.lower()) for ext_id, it in grouped.items() for u in it["users"]],
            )

    def wanters(self, service, ext_id):
        with self.lock:
            rows = self.db.execute(
                "SELECT user FROM wants WHERE service=? AND ext_id=?", (service, ext_id)).fetchall()
        return {r[0] for r in rows}

    def added_rows(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT service, ext_id, title, users FROM handled WHERE result='added'").fetchall()
        return [{"service": s, "id": i, "title": t, "users": {x.lower() for x in (u or "").split(",") if x}}
                for s, i, t, u in rows]

    def deleted_users(self, service, ext_id):
        """For a title deleted from a TV: who still had it on their watchlist
        then (and since). None if it wasn't deleted that way."""
        with self.lock:
            row = self.db.execute("SELECT users FROM handled WHERE service=? AND ext_id=? AND result='deleted'",
                                  (service, ext_id)).fetchone()
        return None if row is None else {x.lower() for x in (row[0] or "").split(",") if x}

    def set_users(self, service, ext_id, users):
        with self.lock, self.db:
            self.db.execute("UPDATE handled SET users=? WHERE service=? AND ext_id=?",
                            (",".join(sorted(users)), service, ext_id))

    def forget(self, service, ext_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM handled WHERE service=? AND ext_id=?", (service, ext_id))

    def recent(self, limit=200):
        with self.lock:
            rows = self.db.execute(
                "SELECT service, ext_id, title, users, result, ts FROM handled ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(zip(("service", "id", "title", "users", "result", "ts"), r)) for r in rows]


# --------------------------------------------------------------------------- http
def request(session, method, url, retries=4, **kw):
    """HTTP with backoff on 429/5xx and network errors."""
    kw.setdefault("timeout", 30)
    r = None
    for attempt in range(retries + 1):
        try:
            r = session.request(method, url, **kw)
        except requests.RequestException as e:
            if attempt == retries:
                raise
            log.debug("network error %s %s: %s", method, url, e)
        else:
            if r.status_code != 429 and r.status_code < 500:
                return r
            if attempt == retries:
                return r
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 60))
                continue
        time.sleep(2 ** attempt)
    return r


# --------------------------------------------------------------------------- plex sign-in
def plex_headers(client_id, token=None):
    h = {
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": "1.1",
        "X-Plex-Device-Name": PRODUCT,
        "Accept": "application/json",
    }
    if token:
        h["X-Plex-Token"] = token
    return h


def plex_create_pin(client_id):
    s = requests.Session()
    r = request(s, "POST", f"{PLEXTV}/api/v2/pins", params={"strong": "true"}, headers=plex_headers(client_id))
    r.raise_for_status()
    pin = r.json()
    url = "https://app.plex.tv/auth#?" + urlencode({
        "clientID": client_id,
        "code": pin["code"],
        "context[device][product]": PRODUCT,
    })
    return {"id": pin["id"], "url": url}


def plex_check_pin(client_id, pin_id):
    s = requests.Session()
    r = request(s, "GET", f"{PLEXTV}/api/v2/pins/{int(pin_id)}", headers=plex_headers(client_id))
    r.raise_for_status()
    return r.json().get("authToken")


def plex_account(client_id, token):
    s = requests.Session()
    r = request(s, "GET", f"{PLEXTV}/api/v2/user", headers=plex_headers(client_id, token))
    r.raise_for_status()
    d = r.json()
    return {"username": d.get("username") or d.get("title") or "owner", "thumb": d.get("thumb")}


# --------------------------------------------------------------------------- plex data
class Plex:
    DISCOVER = "https://discover.provider.plex.tv"
    COMMUNITY = "https://community.plex.tv/api"

    WATCHLIST_QUERY = """
    query GetWatchlistHub($uuid: ID = "", $first: PaginationInt!, $after: String) {
      user(id: $uuid) {
        watchlist(first: $first, after: $after) {
          nodes { id title type }
          pageInfo { hasNextPage endCursor }
        }
      }
    }"""

    FRIENDS_QUERY = """
    query GetAllFriends {
      allFriendsV2 { user { id username } }
    }"""

    def __init__(self, token, client_id, state):
        self.state = state
        self.client_id = client_id
        self.s = requests.Session()
        self.s.headers.update(plex_headers(client_id, token))

    def _get_json(self, url, **params):
        r = request(self.s, "GET", url, params=params)
        r.raise_for_status()
        return r.json()

    def _graphql(self, query, variables=None):
        r = request(self.s, "POST", self.COMMUNITY, json={"query": query, "variables": variables or {}})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"Plex GraphQL error: {body['errors']}")
        return body["data"]

    def ping(self):
        try:
            request(self.s, "GET", f"{PLEXTV}/api/v2/ping")
        except requests.RequestException:
            pass

    def owner_name(self):
        data = self._get_json(f"{PLEXTV}/api/v2/user")
        return data.get("username") or data.get("title") or "owner"

    def own_watchlist(self):
        start, size = 0, 100
        while True:
            mc = self._get_json(
                f"{self.DISCOVER}/library/sections/watchlist/all",
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": size},
            ).get("MediaContainer", {})
            for m in mc.get("Metadata", []) or []:
                yield str(m["ratingKey"]), m.get("title", "?"), m.get("type", "").lower()
            start += size
            if start >= int(mc.get("totalSize", 0)):
                break

    def friends(self):
        data = self._graphql(self.FRIENDS_QUERY)
        return [f["user"] for f in data.get("allFriendsV2", []) if f.get("user")]

    def friend_watchlist(self, user_uuid):
        after = None
        while True:
            variables = {"uuid": user_uuid, "first": 100}
            if after:
                variables["after"] = after
            wl = self._graphql(self.WATCHLIST_QUERY, variables)["user"]["watchlist"]
            for n in wl.get("nodes", []):
                yield str(n["id"]), n.get("title", "?"), n.get("type", "").lower()
            info = wl.get("pageInfo", {})
            if not (info.get("hasNextPage") and info.get("endCursor")):
                break
            after = info["endCursor"]

    def server_usernames(self):
        """Usernames/titles of everyone the owner shares a server with."""
        r = request(self.s, "GET", f"{PLEXTV}/api/users", headers={"Accept": "application/xml"})
        r.raise_for_status()
        names = set()
        for u in ET.fromstring(r.content).iter("User"):
            for attr in ("username", "title"):
                if u.get(attr):
                    names.add(u.get(attr).lower())
        return names

    def guids(self, rating_key):
        cached = self.state.cached_guids(rating_key)
        if cached is not None:
            return cached
        mc = self._get_json(f"{self.DISCOVER}/library/metadata/{rating_key}", includeGuids=1)
        meta = (mc.get("MediaContainer", {}).get("Metadata") or [{}])[0]
        guids = {}
        for g in meta.get("Guid", []) or []:
            m = re.match(r"(tvdb|tmdb|imdb)://(.+)", g.get("id", ""))
            if m:
                guids[m.group(1)] = m.group(2)
        if guids:
            self.state.cache_guids(rating_key, guids)
        return guids


# --------------------------------------------------------------------------- *arr
def tag_label(username):
    return "plex-" + (re.sub(r"[^a-z0-9]+", "-", username.lower()).strip("-") or "user")


class ArrError(Exception):
    def __init__(self, status, text):
        super().__init__(f"HTTP {status}: {text}")
        self.status = status
        self.text = text

    @property
    def already_exists(self):
        t = self.text.lower()
        return "already been added" in t or "already exists" in t


class Arr:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.base = cfg.url.rstrip("/") + "/api/v3"
        self.s = requests.Session()
        self.s.headers.update({"X-Api-Key": cfg.api_key})
        self._tags = None

    def get(self, path, **params):
        r = request(self.s, "GET", self.base + path, params=params, retries=1)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = request(self.s, "POST", self.base + path, json=body, timeout=60)
        if r.status_code >= 400:
            raise ArrError(r.status_code, r.text[:500])
        return r.json()

    def quality_profile_id(self, want=None):
        profiles = self.get("/qualityprofile")
        want = self.cfg.quality_profile if want is None else want
        if not profiles:
            raise RuntimeError(f"{self.name}: no quality profiles found")
        if want:
            for p in profiles:
                if str(p["id"]) == want or p["name"].lower() == want.lower():
                    return p["id"]
            raise RuntimeError(f"{self.name}: quality profile '{want}' not found "
                               f"(have: {', '.join(p['name'] for p in profiles)})")
        return profiles[0]["id"]

    def root_folder(self, want=None):
        paths = [f["path"] for f in self.get("/rootfolder")]
        if not paths:
            raise RuntimeError(f"{self.name}: no root folders configured")
        want = self.cfg.root_folder if want is None else want
        if want:
            for p in paths:
                if p.rstrip("/") == want.rstrip("/"):
                    return p
            raise RuntimeError(f"{self.name}: root folder '{want}' not found (have: {', '.join(paths)})")
        return paths[0]

    def tag_ids(self, usernames):
        if self._tags is None:
            self._tags = {t["label"].lower(): t["id"] for t in self.get("/tag")}
        ids = []
        for u in sorted(usernames):
            label = tag_label(u)
            if label not in self._tags:
                self._tags[label] = self.post("/tag", {"label": label})["id"]
            ids.append(self._tags[label])
        return ids

    def find(self, ext_id, imdb=None):
        """Return the series/movie object in Sonarr/Radarr for a TVDB/TMDB id
        (or, failing that, an IMDb id), or None."""
        key, path = ("tvdbId", "/series") if self.name == "Sonarr" else ("tmdbId", "/movie")
        items = self.get(path)
        for x in items:
            if ext_id and x.get(key) == ext_id:
                return x
        if imdb:
            for x in items:
                if (x.get("imdbId") or "").lower() == imdb.lower():
                    return x
        return None

    def delete(self, obj):
        path = "/series" if self.name == "Sonarr" else "/movie"
        excl = "addImportListExclusion" if self.name == "Sonarr" else "addImportExclusion"
        r = request(self.s, "DELETE", f"{self.base}{path}/{obj['id']}",
                    params={"deleteFiles": "true", excl: "false"}, timeout=120)
        if r.status_code >= 400:
            raise ArrError(r.status_code, r.text[:500])

    def remove_tag(self, obj, username):
        tags = {t["label"].lower(): t["id"] for t in self.get("/tag")}
        tid = tags.get(tag_label(username))
        if tid is None or tid not in obj.get("tags", []):
            return
        path = "/series" if self.name == "Sonarr" else "/movie"
        body = dict(obj)
        body["tags"] = [t for t in obj["tags"] if t != tid]
        r = request(self.s, "PUT", f"{self.base}{path}/{obj['id']}", json=body, timeout=60)
        if r.status_code >= 400:
            raise ArrError(r.status_code, r.text[:500])

    def probe(self):
        """Used by the UI's Test button: version + choices for the dropdowns."""
        status = self.get("/system/status")
        return {
            "version": status.get("version"),
            "profiles": [{"id": p["id"], "name": p["name"]} for p in self.get("/qualityprofile")],
            "roots": [f["path"] for f in self.get("/rootfolder")],
        }


def is_anime(series):
    """A Sonarr lookup result is anime if TheTVDB gives it the Anime genre,
    or it's Japanese animation."""
    genres = {g.lower() for g in series.get("genres") or []}
    lang = ((series.get("originalLanguage") or {}).get("name") or "").lower()
    return "anime" in genres or ("animation" in genres and lang == "japanese")


def bazarr_probe(url, key):
    r = requests.get(url.rstrip("/") + "/api/system/status", headers={"X-API-KEY": key}, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", {})
    return {"version": data.get("bazarr_version")}


# --------------------------------------------------------------------------- sync
class Syncer:
    def __init__(self, data_dir):
        self.state = State(os.path.join(data_dir, "watchlist-sync.db"))
        self.settings = Settings(self.state)
        self.client_id = self.state.get("client_id")
        if not self.client_id:
            self.client_id = f"{PRODUCT}-{uuid.uuid4()}"
            self.state.set("client_id", self.client_id)
        self.last_run = {}
        self.users = []
        self.running = False
        self._last_ping = 0
        self.wake = threading.Event()
        self.cfg = None
        self.lock = threading.Lock()      # one sync or exclusion at a time
        self.action = None                # progress/result of the last exclude, shown in the UI

    # -- setup helpers
    def build(self):
        cfg = Config(self.settings.all())
        self.cfg = cfg
        self.plex = Plex(cfg.plex_token, self.client_id, self.state)
        self.sonarr = Arr("Sonarr", cfg.sonarr) if cfg.sonarr.enabled else None
        self.radarr = Arr("Radarr", cfg.radarr) if cfg.radarr.enabled else None
        return cfg

    def setup_problem(self, cfg):
        if not cfg.plex_token:
            return "Sign in with Plex to start syncing."
        if not (cfg.sonarr.enabled or cfg.radarr.enabled):
            return "Add Sonarr (or Radarr) in Settings to start syncing."
        return None

    def user_ok(self, name):
        n = name.lower()
        if self.cfg.user_allow and n not in self.cfg.user_allow:
            return False
        return n not in self.cfg.user_block

    # -- step 1: gather every watchlisted item and who wants it
    def collect(self):
        items = {}  # rating_key -> {"title", "type", "users": set}
        users = []

        def add(rk, title, typ, user):
            it = items.setdefault(rk, {"title": title, "type": typ, "users": set()})
            it["users"].add(user)

        owner = self.plex.owner_name()
        if self.cfg.include_owner:
            if self.user_ok(owner):
                n = 0
                for rk, title, typ in self.plex.own_watchlist():
                    add(rk, title, typ, owner)
                    n += 1
                users.append({"name": owner, "role": "owner", "items": n, "status": "ok"})
                log.info("Owner %s: %d watchlist items", owner, n)
            else:
                users.append({"name": owner, "role": "owner", "items": None, "status": "excluded"})

        if self.cfg.include_friends:
            friends = self.plex.friends()
            allowed = self.plex.server_usernames() if self.cfg.only_server_users else None
            for f in friends:
                name = f["username"]
                if allowed is not None and name.lower() not in allowed:
                    continue
                if not self.user_ok(name):
                    users.append({"name": name, "role": "friend", "items": None, "status": "excluded"})
                    continue
                try:
                    n = 0
                    for rk, title, typ in self.plex.friend_watchlist(f["id"]):
                        add(rk, title, typ, name)
                        n += 1
                    users.append({"name": name, "role": "friend", "items": n, "status": "ok"})
                    log.info("Friend %s: %d watchlist items", name, n)
                except Exception as e:  # private watchlist etc.
                    users.append({"name": name, "role": "friend", "items": None, "status": "private"})
                    log.warning("Could not read %s's watchlist (is their Watchlist visibility "
                                "set to Friends?): %s", name, e)
        self.users = users
        return items

    # -- step 2: push to Sonarr / Radarr
    def run_once(self):
        with self.lock:
            return self._run_once()

    def _run_once(self):
        cfg = self.build()
        problem = self.setup_problem(cfg)
        if problem:
            self.last_run = {"skipped": problem}
            log.info("Not syncing yet: %s", problem)
            return self.last_run

        self.running = True
        t0 = time.time()
        summary = {"started": int(t0), "added_series": [], "added_movies": [], "errors": [],
                   "dry_run": cfg.dry_run}
        try:
            if time.time() - self._last_ping > 86400:  # keeps the Plex token from expiring
                self.plex.ping()
                self._last_ping = time.time()

            items = self.collect()
            shows = {k: v for k, v in items.items() if v["type"] == "show"}
            movies = {k: v for k, v in items.items() if v["type"] == "movie"}
            summary["shows"], summary["movies"] = len(shows), len(movies)
            log.info("Collected %d shows and %d movies across all watchlists", len(shows), len(movies))

            if self.sonarr and shows:
                self._sync_sonarr(shows, summary)
            if self.radarr and movies:
                self._sync_radarr(movies, summary)
            self._nudge_bazarr(summary)
        except Exception as e:
            log.exception("Sync cycle failed")
            summary["errors"].append(str(e))
        finally:
            self.running = False

        summary["duration_s"] = round(time.time() - t0, 1)
        self.last_run = summary
        log.info("Cycle done in %ss: +%d series, +%d movies, %d errors", summary["duration_s"],
                 len(summary["added_series"]), len(summary["added_movies"]), len(summary["errors"]))
        return summary

    # -- excluding a user
    def deletion_plan(self, username):
        """What excluding `username` would delete vs keep.

        Only titles watchlist-sync itself added are considered. A title is kept if any
        other user who isn't excluded requested it or still has it on their watchlist."""
        user = username.lower()
        cfg = Config(self.settings.all())
        excluded = cfg.user_block | {user}
        delete, keep = [], []
        for row in self.state.added_rows():
            wanted_now = self.state.wanters(row["service"], row["id"])
            if user not in row["users"] | wanted_now:
                continue
            others = sorted((row["users"] | wanted_now) - excluded)
            if cfg.user_allow:
                others = [o for o in others if o in cfg.user_allow]
            item = {"service": row["service"], "id": row["id"], "title": row["title"], "others": others}
            (keep if others else delete).append(item)
        key = lambda x: x["title"].lower()
        return {"user": username, "delete": sorted(delete, key=key), "keep": sorted(keep, key=key),
                "dry_run": cfg.dry_run}

    def _set_blocked(self, username, blocked):
        cur = [x.strip() for x in self.settings.all()["user_block"].split(",") if x.strip()]
        cur = [x for x in cur if x.lower() != username.lower()]
        if blocked:
            cur.append(username)
        self.settings.update({"user_block": ", ".join(cur)})
        for u in self.users:
            if u["name"].lower() == username.lower():
                u["status"] = "excluded" if blocked else "ok"
                if blocked:
                    u["items"] = None

    def include_user(self, username):
        self._set_blocked(username, False)
        self.wake.set()

    def exclude_user(self, username, delete_media):
        """Block the user now; delete their media in the background."""
        plan = self.deletion_plan(username)
        self._set_blocked(username, True)
        self.action = {"user": username, "state": "running", "deleted": [], "untagged": [], "errors": [],
                       "total": len(plan["delete"]) if delete_media else 0}
        threading.Thread(target=self._do_exclude, args=(username, plan, delete_media), daemon=True).start()
        return plan

    def _do_exclude(self, username, plan, delete_media):
        act = self.action
        with self.lock:
            try:
                cfg = self.build()
                arrs = {"sonarr": self.sonarr, "radarr": self.radarr}
                log.info("Excluded %s", username)
                if delete_media:
                    for it in plan["delete"]:
                        arr = arrs.get(it["service"])
                        try:
                            if cfg.dry_run:
                                log.info("[dry-run] would delete '%s' and its files", it["title"])
                                act["deleted"].append(it["title"])
                                continue
                            if not arr:
                                raise RuntimeError(f"{it['service'].title()} isn't configured")
                            obj = arr.find(it["id"])
                            if obj:
                                arr.delete(obj)
                            self.state.forget(it["service"], it["id"])
                            act["deleted"].append(it["title"])
                            log.info("Deleted '%s' and its files (requested by %s)", it["title"], username)
                        except Exception as e:
                            act["errors"].append(f"{it['title']}: {e}")
                            log.error("Could not delete '%s': %s", it["title"], e)
                # titles other people still want: just drop this user's tag
                if cfg.tag_by_user and not cfg.dry_run:
                    for it in plan["keep"]:
                        arr = arrs.get(it["service"])
                        try:
                            obj = arr.find(it["id"]) if arr else None
                            if obj:
                                arr.remove_tag(obj, username)
                                act["untagged"].append(it["title"])
                        except Exception as e:
                            act["errors"].append(f"{it['title']} (tag): {e}")
            finally:
                act["state"] = "done"

    def _group_by(self, items, id_type, summary):
        """Merge watchlist entries that point at the same TVDB/TMDB id."""
        grouped = {}
        for rk, it in items.items():
            try:
                ext = self.plex.guids(rk).get(id_type)
            except Exception as e:
                log.error("Plex metadata lookup failed for '%s': %s", it["title"], e)
                summary["errors"].append(f"plex:{it['title']}: {e}")
                continue
            if not ext:
                log.debug("No %s id for '%s', skipping", id_type.upper(), it["title"])
                continue
            g = grouped.setdefault(int(ext), {"title": it["title"], "users": set()})
            g["users"] |= it["users"]
        return grouped

    @staticmethod
    def _sonarr_ctx(s, cfg):
        """Profiles and folders new series go to (normal and anime)."""
        profile_id = s.quality_profile_id()
        root = s.root_folder()
        anime_root = s.root_folder(cfg.sonarr_anime_root) if cfg.sonarr_anime_root else None
        anime_profile = (s.quality_profile_id(cfg.sonarr_anime_profile)
                         if anime_root and cfg.sonarr_anime_profile else profile_id)
        lang_profile = None
        try:  # Sonarr v3 needs a language profile; v4 ignores it
            lp = s.get("/languageprofile")
            lang_profile = lp[0]["id"] if lp else None
        except Exception:
            pass
        return {"profile": profile_id, "root": root, "anime_root": anime_root,
                "anime_profile": anime_profile, "lang_profile": lang_profile}

    @staticmethod
    def _sonarr_body(s, cfg, ctx, found, users):
        """The Sonarr add-series payload, shared by the watchlist sync and TV
        requests. Returns (body, is_anime)."""
        body = dict(found)
        anime = bool(ctx["anime_root"]) and is_anime(body)
        body.update({
            "qualityProfileId": ctx["anime_profile"] if anime else ctx["profile"],
            "rootFolderPath": ctx["anime_root"] if anime else ctx["root"],
            "monitored": True,
            "seasonFolder": True,
            "seriesType": "anime" if anime else cfg.sonarr_series_type,
            "tags": s.tag_ids(users) if cfg.tag_by_user else [],
            "addOptions": {
                "monitor": cfg.sonarr_monitor,
                "searchForMissingEpisodes": cfg.sonarr.search,
                "searchForCutoffUnmetEpisodes": False,
            },
        })
        if ctx["lang_profile"]:
            body["languageProfileId"] = ctx["lang_profile"]
        return body, anime

    def _sync_sonarr(self, shows, summary):
        s = self.sonarr
        ctx = self._sonarr_ctx(s, self.cfg)
        existing = {x.get("tvdbId") for x in s.get("/series")}

        grouped = self._group_by(shows, "tvdb", summary)
        summary["shows"] = len(grouped)
        self.state.set_wants("sonarr", grouped)
        for tvdb, it in grouped.items():
            title = it["title"]
            try:
                if self._skip_deleted("sonarr", tvdb, it["users"]):
                    continue
                if self.state.is_handled("sonarr", tvdb):
                    continue
                if tvdb in existing:
                    self.state.mark("sonarr", tvdb, title, it["users"], "already-present")
                    continue
                found = s.get("/series/lookup", term=f"tvdb:{tvdb}")
                if not found:
                    log.warning("Sonarr lookup found nothing for '%s' (tvdb:%s)", title, tvdb)
                    continue
                body, anime = self._sonarr_body(s, self.cfg, ctx, found[0], it["users"])
                who = ", ".join(sorted(it["users"]))
                kind = "anime" if anime else "series"
                if self.cfg.dry_run:
                    log.info("[dry-run] would add %s '%s' (tvdb:%s) for %s", kind, title, tvdb, who)
                    summary["added_series"].append(title)
                    continue
                try:
                    s.post("/series", body)
                except ArrError as e:
                    if e.already_exists:
                        self.state.mark("sonarr", tvdb, title, it["users"], "already-present")
                        continue
                    raise
                self.state.mark("sonarr", tvdb, title, it["users"], "added")
                existing.add(tvdb)
                summary["added_series"].append(title)
                log.info("Added %s '%s' (tvdb:%s) requested by %s", kind, title, tvdb, who)
            except Exception as e:
                log.error("Sonarr: failed on '%s': %s", title, e)
                summary["errors"].append(f"sonarr:{title}: {e}")

    def _sync_radarr(self, movies, summary):
        r = self.radarr
        profile_id = r.quality_profile_id()
        root = r.root_folder()
        existing = {x.get("tmdbId") for x in r.get("/movie")}

        grouped = self._group_by(movies, "tmdb", summary)
        summary["movies"] = len(grouped)
        self.state.set_wants("radarr", grouped)
        for tmdb, it in grouped.items():
            title = it["title"]
            try:
                if self._skip_deleted("radarr", tmdb, it["users"]):
                    continue
                if self.state.is_handled("radarr", tmdb):
                    continue
                if tmdb in existing:
                    self.state.mark("radarr", tmdb, title, it["users"], "already-present")
                    continue
                movie = r.get("/movie/lookup/tmdb", tmdbId=tmdb)
                if isinstance(movie, list):
                    movie = movie[0] if movie else None
                if not movie:
                    log.warning("Radarr lookup found nothing for '%s' (tmdb:%s)", title, tmdb)
                    continue
                body = self._radarr_body(r, self.cfg, movie, it["users"], profile_id, root)
                who = ", ".join(sorted(it["users"]))
                if self.cfg.dry_run:
                    log.info("[dry-run] would add movie '%s' (tmdb:%s) for %s", title, tmdb, who)
                    summary["added_movies"].append(title)
                    continue
                try:
                    r.post("/movie", body)
                except ArrError as e:
                    if e.already_exists:
                        self.state.mark("radarr", tmdb, title, it["users"], "already-present")
                        continue
                    raise
                self.state.mark("radarr", tmdb, title, it["users"], "added")
                existing.add(tmdb)
                summary["added_movies"].append(title)
                log.info("Added movie '%s' (tmdb:%s) requested by %s", title, tmdb, who)
            except Exception as e:
                log.error("Radarr: failed on '%s': %s", title, e)
                summary["errors"].append(f"radarr:{title}: {e}")

    @staticmethod
    def _radarr_body(r, cfg, movie, users, profile_id, root):
        """The Radarr add-movie payload, shared by the watchlist sync and TV requests."""
        body = dict(movie)
        body.update({
            "qualityProfileId": profile_id,
            "rootFolderPath": root,
            "monitored": True,
            "minimumAvailability": cfg.radarr_min_availability,
            "tags": r.tag_ids(users) if cfg.tag_by_user else [],
            "addOptions": {"searchForMovie": cfg.radarr.search, "monitor": "movieOnly"},
        })
        return body

    # -- TV clients (Orama): status line, and "sync now" after a Watchlist add
    @staticmethod
    def _reachable(arr_cfg):
        """Can we open a connection to this Sonarr/Radarr? (3 s, no API key needed)"""
        if not arr_cfg.enabled:
            return None
        try:
            requests.get(arr_cfg.url.rstrip("/") + "/ping", timeout=3)
            return True
        except requests.RequestException:
            return False

    def tv_info(self):
        cfg = Config(self.settings.all())
        return {"product": PRODUCT, "version": VERSION, "radarr": cfg.radarr.enabled, "sonarr": cfg.sonarr.enabled,
                "radarr_url": cfg.radarr.url if cfg.radarr.enabled else "",
                "sonarr_url": cfg.sonarr.url if cfg.sonarr.enabled else "",
                "radarr_ok": self._reachable(cfg.radarr), "sonarr_ok": self._reachable(cfg.sonarr),
                "dry_run": cfg.dry_run, "signed_in": bool(cfg.plex_token)}

    # -- downloads in progress, for Orama (GET /api/tv/queue)
    QUEUE_TTL = 4           # seconds; the TV asks every 5 s
    SEARCHING_DAYS = 3      # titles added this recently, with nothing downloaded yet, show as "Searching"

    @staticmethod
    def _secs(timeleft):
        """Sonarr/Radarr's 'D.HH:MM:SS' / 'HH:MM:SS' to seconds (None if unknown)."""
        m = re.fullmatch(r"(?:(\d+)\.)?(\d+):(\d+):(\d+)(?:\.\d+)?", timeleft or "")
        if not m:
            return None
        d, h, mi, se = (int(x or 0) for x in m.groups())
        return ((d * 24 + h) * 60 + mi) * 60 + se

    @staticmethod
    def _status(rec):
        st = (rec.get("trackedDownloadState") or "").lower()
        raw = (rec.get("status") or "").lower()
        if st in ("importpending", "importing", "imported") or raw == "completed":
            return "importing"
        if st in ("failed", "failedpending") or raw == "failed":
            return "failed"
        if (rec.get("trackedDownloadStatus") or "").lower() == "warning" or raw == "warning":
            return "stalled"
        if raw in ("paused",):
            return "paused"
        if raw in ("queued", "delay", "downloadclientunavailable"):
            return "queued"
        return "downloading"

    @staticmethod
    def _poster(obj):
        for img in obj.get("images") or []:
            if img.get("coverType") == "poster":
                return img.get("remoteUrl") or ""
        return ""

    def _queue_of(self, arr, kind):
        """Group one Sonarr/Radarr queue by movie/series."""
        rank = {"downloading": 0, "importing": 1, "stalled": 2, "paused": 3, "queued": 4, "failed": 5}
        params = {"page": 1, "pageSize": 200}
        params["includeMovie" if kind == "movie" else "includeSeries"] = "true"
        if kind == "show":
            params["includeEpisode"] = "true"
        r = request(arr.s, "GET", arr.base + "/queue", retries=0, params=params, timeout=8)
        r.raise_for_status()
        groups = {}
        for rec in r.json().get("records", []):
            obj = rec.get("movie" if kind == "movie" else "series") or {}
            key = rec.get("movieId" if kind == "movie" else "seriesId")
            if key is None:
                continue
            g = groups.setdefault(key, {
                "kind": kind, "title": obj.get("title") or rec.get("title") or "?", "year": obj.get("year"),
                "tmdb": obj.get("tmdbId"), "tvdb": obj.get("tvdbId"), "imdb": obj.get("imdbId") or "",
                "poster": self._poster(obj), "size": 0, "sizeleft": 0, "timeleft": None,
                "status": "queued", "episodes": [],
            })
            g["size"] += rec.get("size") or 0
            g["sizeleft"] += rec.get("sizeleft") or 0
            t = self._secs(rec.get("timeleft"))
            if t is not None:
                g["timeleft"] = max(g["timeleft"] or 0, t)
            st = self._status(rec)
            if rank[st] < rank[g["status"]]:
                g["status"] = st
            ep = rec.get("episode") or {}
            if ep.get("episodeNumber") is not None:
                g["episodes"].append("S%02dE%02d" % (ep.get("seasonNumber") or 0, ep["episodeNumber"]))
        out = []
        for g in groups.values():
            g["progress"] = round(100 * (1 - g["sizeleft"] / g["size"]), 1) if g["size"] else 0
            eps = sorted(set(g.pop("episodes")))
            g["detail"] = eps[0] if len(eps) == 1 else (f"{len(eps)} episodes" if eps else "")
            out.append(g)
        return out

    def _searching(self, arr, kind, queued_ids):
        """Titles watchlist-sync added in the last few days that have nothing
        downloaded and aren't in the queue: Sonarr/Radarr are still looking."""
        service = "radarr" if kind == "movie" else "sonarr"
        since = time.time() - self.SEARCHING_DAYS * 86400
        recent = {int(r["id"]) for r in self.state.recent(500)
                  if r["service"] == service and r["result"] == "added" and r["ts"] >= since and str(r["id"]).isdigit()}
        if not recent:
            return []
        r = request(arr.s, "GET", arr.base + ("/movie" if kind == "movie" else "/series"), retries=0, timeout=10)
        r.raise_for_status()
        out = []
        for obj in r.json():
            ext = obj.get("tmdbId" if kind == "movie" else "tvdbId")
            if ext not in recent or ext in queued_ids:
                continue
            if kind == "movie":
                empty = not obj.get("hasFile")
            else:
                empty = not (obj.get("statistics") or {}).get("episodeFileCount")
            if empty and obj.get("monitored", True):
                out.append({"kind": kind, "title": obj.get("title") or "?", "year": obj.get("year"),
                            "tmdb": obj.get("tmdbId"), "tvdb": obj.get("tvdbId"), "imdb": obj.get("imdbId") or "",
                            "poster": self._poster(obj), "size": 0, "sizeleft": 0, "timeleft": None,
                            "status": "searching", "progress": 0, "detail": ""})
        return out

    # -- a movie's collection, for Orama (GET /api/tv/collection?tmdb=ID)
    COLLECTION_TTL = 600

    def tv_collection(self, tmdb):
        """The Radarr collection a movie belongs to, with every part (owned or
        not). Radarr keeps a collection for any movie it manages; for one it
        doesn't, there's nothing to list. {'collection': None} when unknown."""
        cache = self.__dict__.setdefault("_collection_cache", {})
        hit = cache.get(tmdb)
        if hit and time.time() - hit[0] < self.COLLECTION_TTL:
            return hit[1]
        cfg = Config(self.settings.all())
        if not cfg.radarr.enabled:
            return {"collection": None, "reason": "radarr not set up"}
        arr = Arr("Radarr", cfg.radarr)
        get = lambda path, **params: request(arr.s, "GET", arr.base + path, retries=0, params=params, timeout=10)
        r = get("/movie", tmdbId=tmdb)
        r.raise_for_status()
        movies = r.json()
        movie = movies[0] if movies else None
        coll = (movie or {}).get("collection") or {}
        if not coll.get("tmdbId"):
            # Not in Radarr: its lookup still names the collection.
            r = get("/movie/lookup/tmdb", tmdbId=tmdb)
            if r.status_code == 200:
                coll = (r.json() or {}).get("collection") or {}
        if not coll.get("tmdbId"):
            result = {"collection": None}
        else:
            r = get("/collection", tmdbId=coll["tmdbId"])
            r.raise_for_status()
            found = r.json() or []
            c = found[0] if found else None
            if not c:
                result = {"collection": None, "title": coll.get("title")}
            else:
                parts = []
                for m in c.get("movies") or []:
                    tid = m.get("tmdbId")
                    have = m.get("isExisting")
                    entry = {"tmdb": tid, "title": m.get("title") or "?", "year": m.get("year"),
                             "imdb": m.get("imdbId") or "", "poster": self._poster(m), "inRadarr": False,
                             "hasFile": False, "monitored": False}
                    if have is None or have:
                        rr = get("/movie", tmdbId=tid)
                        if rr.status_code == 200 and rr.json():
                            rm = rr.json()[0]
                            entry.update(inRadarr=True, hasFile=bool(rm.get("hasFile")), monitored=bool(rm.get("monitored")))
                    parts.append(entry)
                parts.sort(key=lambda x: (x["year"] or 9999, x["title"]))
                result = {"collection": {"title": c.get("title") or coll.get("title"), "tmdb": coll["tmdbId"], "movies": parts}}
        cache[tmdb] = (time.time(), result)
        return result

    # -- finding and requesting titles from a TV (Jellyfin has no Watchlist)
    LIBRARY_TTL = 30

    def _library(self, arr, kind):
        """{tmdb or tvdb id: 'have' | 'requested'} for what Radarr/Sonarr already hold."""
        cache = self.__dict__.setdefault("_library_cache", {})
        hit = cache.get(kind)
        if hit and time.time() - hit[0] < self.LIBRARY_TTL:
            return hit[1]
        out = {}
        if kind == "movie":
            for m in arr.get("/movie"):
                out[m.get("tmdbId")] = "have" if m.get("hasFile") else "requested"
        else:
            for x in arr.get("/series"):
                files = ((x.get("statistics") or {}).get("episodeFileCount") or 0)
                out[x.get("tvdbId")] = "have" if files else "requested"
        cache[kind] = (time.time(), out)
        return out

    def _lookup_item(self, kind, x, library):
        ext = x.get("tmdbId") if kind == "movie" else x.get("tvdbId")
        ratings = x.get("ratings") or {}
        rating = ((ratings.get("tmdb") or {}).get("value") or (ratings.get("imdb") or {}).get("value")
                  or ratings.get("value") or 0)
        return {
            "kind": kind, "title": x.get("title") or "?", "year": x.get("year") or None,
            "overview": x.get("overview") or "", "genres": (x.get("genres") or [])[:4],
            "runtime": x.get("runtime") or 0, "rating": rating,
            "network": x.get("network") or x.get("studio") or "",
            "certification": x.get("certification") or "",
            "tmdb": x.get("tmdbId") or 0, "tvdb": x.get("tvdbId") or 0, "imdb": x.get("imdbId") or "",
            "poster": self._poster(x), "fanart": self._image(x, "fanart"),
            "status": library.get(ext, "none"),
        }

    def tv_lookup(self, kind, q="", ext_id=0):
        """Movies (Radarr) or shows (Sonarr) matching `q`, or the one with this
        TMDB/TVDB id, each with whether it's already there: 'have' (has files),
        'requested' (added, nothing downloaded yet) or 'none'."""
        cfg = Config(self.settings.all())
        arr_cfg = cfg.radarr if kind == "movie" else cfg.sonarr
        if not arr_cfg.enabled:
            raise LookupError(("Radarr" if kind == "movie" else "Sonarr") + " isn't set up in watchlist-sync")
        arr = Arr("Radarr" if kind == "movie" else "Sonarr", arr_cfg)
        if ext_id:
            if kind == "movie":
                found = arr.get("/movie/lookup/tmdb", tmdbId=ext_id)
                found = found if isinstance(found, list) else [found] if found else []
            else:
                found = arr.get("/series/lookup", term=f"tvdb:{ext_id}")
        else:
            found = arr.get("/movie/lookup" if kind == "movie" else "/series/lookup", term=q)
        library = self._library(arr, kind)
        key = "tmdbId" if kind == "movie" else "tvdbId"
        return {"items": [self._lookup_item(kind, x, library) for x in (found or [])[:30] if x.get(key)]}

    def tv_request(self, kind, ext_id, user=""):
        """Add a movie to Radarr or a show to Sonarr, the way a watchlist add
        would (same profiles, folders, anime handling and search)."""
        cfg = Config(self.settings.all())
        arr_cfg = cfg.radarr if kind == "movie" else cfg.sonarr
        svc = "Radarr" if kind == "movie" else "Sonarr"
        if not arr_cfg.enabled:
            raise LookupError(svc + " isn't set up in watchlist-sync")
        arr = Arr(svc, arr_cfg)
        service = svc.lower()
        users = {user} if user else {"tv"}
        if arr.find(ext_id):
            return {"ok": True, "status": "already-there", "service": svc}
        if kind == "movie":
            movie = arr.get("/movie/lookup/tmdb", tmdbId=ext_id)
            movie = movie[0] if isinstance(movie, list) and movie else movie
            if not movie:
                raise LookupError(f"{svc} couldn't find that movie")
            body = self._radarr_body(arr, cfg, movie, users, arr.quality_profile_id(), arr.root_folder())
            title, anime = movie.get("title") or "?", False
        else:
            found = arr.get("/series/lookup", term=f"tvdb:{ext_id}")
            if not found:
                raise LookupError(f"{svc} couldn't find that show")
            body, anime = self._sonarr_body(arr, cfg, self._sonarr_ctx(arr, cfg), found[0], users)
            title = found[0].get("title") or "?"
        if cfg.dry_run:
            log.info("[dry-run] would add %s '%s' requested from a TV by %s", kind, title, ", ".join(users))
            return {"ok": True, "status": "dry-run", "service": svc}
        try:
            arr.post("/movie" if kind == "movie" else "/series", body)
        except ArrError as e:
            if e.already_exists:
                return {"ok": True, "status": "already-there", "service": svc}
            raise
        self.state.mark(service, ext_id, title, users, "requested")
        self.__dict__.get("_library_cache", {}).pop(kind, None)
        log.info("Added %s '%s' requested from a TV by %s", "anime" if anime else kind, title, ", ".join(users))
        return {"ok": True, "status": "added", "service": svc, "title": title}

    # -- deleting a title from a TV (POST /api/tv/delete)
    def tv_delete(self, kind, ext_id, imdb, title):
        """Remove a movie (Radarr) or show (Sonarr) and its files. The title is
        remembered as deleted, so a watchlist it is still on doesn't bring it
        back; adding it to a watchlist again later does."""
        cfg = Config(self.settings.all())
        service, acfg, name = ("radarr", cfg.radarr, "Radarr") if kind == "movie" else ("sonarr", cfg.sonarr, "Sonarr")
        if not acfg.enabled:
            return {"service": service, "configured": False, "found": False, "deleted": False}
        arr = Arr(name, acfg)
        obj = arr.find(ext_id, imdb)
        if not obj:
            return {"service": service, "configured": True, "found": False, "deleted": False}
        key = "tmdbId" if kind == "movie" else "tvdbId"
        real_id = obj.get(key) or ext_id
        label = obj.get("title") or title or "?"
        if cfg.dry_run:
            log.info("[dry-run] TV would delete '%s' and its files from %s", label, name)
            return {"service": service, "configured": True, "found": True, "deleted": False, "dry_run": True}
        with self.lock:
            arr.delete(obj)
            if real_id:
                self.state.mark(service, int(real_id), label, self.state.wanters(service, int(real_id)), "deleted")
        self._queue_cache = None
        self.__dict__.get("_collection_cache", {}).clear()
        log.info("Deleted '%s' and its files from %s (from a TV)", label, name)
        return {"service": service, "configured": True, "found": True, "deleted": True, "title": label}

    def _skip_deleted(self, service, ext_id, users):
        """True while a TV-deleted title should stay deleted. Users who drop it
        from their watchlist are forgotten; anyone adding it (again) brings it back."""
        before = self.state.deleted_users(service, ext_id)
        if before is None:
            return False
        now = {u.lower() for u in users}
        if now - before:
            self.state.forget(service, ext_id)
            return False
        if now != before:
            self.state.set_users(service, ext_id, now)
        return True

    # -- subtitles through Bazarr, for Orama (GET/POST /api/tv/subtitles)
    # Bazarr's search results carry an opaque, pickled subtitle id that Bazarr
    # unpickles when downloading, so it never leaves this process: the TV gets
    # a token and picks a result by number.
    SUBS_TTL = 900

    def _bazarr(self):
        cfg = Config(self.settings.all())
        if not (cfg.bazarr_url and cfg.bazarr_key):
            raise LookupError("Bazarr isn't set up in watchlist-sync")
        return cfg, cfg.bazarr_url.rstrip("/"), {"X-API-KEY": cfg.bazarr_key}

    def _subtitle_target(self, cfg, kind, tmdb, tvdb, imdb, season, episode):
        """Bazarr ids for a movie (Radarr id) or an episode (Sonarr ids)."""
        if kind == "movie":
            if not cfg.radarr.enabled:
                raise LookupError("Radarr isn't set up in watchlist-sync")
            obj = Arr("Radarr", cfg.radarr).find(tmdb, imdb)
            if not obj:
                raise LookupError("This movie isn't in Radarr, so Bazarr doesn't know it")
            return {"radarrid": obj["id"]}
        if not cfg.sonarr.enabled:
            raise LookupError("Sonarr isn't set up in watchlist-sync")
        arr = Arr("Sonarr", cfg.sonarr)
        series = arr.find(tvdb, imdb)
        if not series:
            raise LookupError("This show isn't in Sonarr, so Bazarr doesn't know it")
        eps = arr.get("/episode", seriesId=series["id"])
        ep = next((e for e in eps if e.get("seasonNumber") == season and e.get("episodeNumber") == episode), None)
        if not ep:
            raise LookupError(f"Sonarr has no S{season:02d}E{episode:02d} for this show")
        return {"seriesid": series["id"], "episodeid": ep["id"]}

    def tv_subtitle_search(self, kind, tmdb=0, tvdb=0, imdb="", season=0, episode=0, lang=""):
        cfg, base, headers = self._bazarr()
        target = self._subtitle_target(cfg, kind, tmdb, tvdb, imdb, season, episode)
        path = "/api/providers/movies" if kind == "movie" else "/api/providers/episodes"
        params = {"radarrid": target["radarrid"]} if kind == "movie" else {"episodeid": target["episodeid"]}
        r = requests.get(base + path, params=params, headers=headers, timeout=150)
        if r.status_code in (401, 403):
            raise LookupError("Bazarr rejected the API key")
        if r.status_code >= 400:
            raise LookupError("Bazarr: " + (r.text.strip().strip('"')[:200] or f"HTTP {r.status_code}"))
        found = (r.json() or {}).get("data") or []
        want = (lang or "").lower()
        results = []
        for x in found:
            code = str(x.get("language") or "").split(":")[0].lower()
            if want and code != want:
                continue
            results.append({
                "provider": x.get("provider") or "", "language": code,
                "hi": str(x.get("hearing_impaired")).lower() == "true",
                "forced": str(x.get("forced")).lower() == "true",
                "score": x.get("score") or 0, "release": (x.get("release_info") or [""])[0] or "",
                "uploader": x.get("uploader") or "",
                "_post": {"hi": str(x.get("hearing_impaired") or "False"), "forced": str(x.get("forced") or "False"),
                          "original_format": str(x.get("original_format") or "False"),
                          "provider": x.get("provider") or "", "subtitle": x.get("subtitle") or ""},
            })
        results.sort(key=lambda x: -x["score"])
        results = results[:40]
        token = uuid.uuid4().hex
        cache = self.__dict__.setdefault("_subs_cache", {})
        now = time.time()
        for k in [k for k, v in cache.items() if now - v["at"] > self.SUBS_TTL]:
            del cache[k]
        cache[token] = {"at": now, "kind": kind, "target": target, "results": results}
        return {"token": token, "results": [{k: v for k, v in x.items() if k != "_post"} for x in results]}

    def tv_subtitle_download(self, token, index):
        cfg, base, headers = self._bazarr()
        entry = self.__dict__.get("_subs_cache", {}).get(token)
        if not entry or time.time() - entry["at"] > self.SUBS_TTL:
            raise LookupError("Those search results expired; search again")
        if not (0 <= index < len(entry["results"])):
            raise LookupError("No such result")
        pick = entry["results"][index]
        path = "/api/providers/movies" if entry["kind"] == "movie" else "/api/providers/episodes"
        form = dict(pick["_post"], **{k: str(v) for k, v in entry["target"].items()})
        r = requests.post(base + path, data=form, headers=headers, timeout=150)
        if r.status_code >= 400:
            raise LookupError("Bazarr couldn't download it: " + (r.text.strip().strip('"')[:200] or f"HTTP {r.status_code}"))
        log.info("Downloaded %s subtitles from %s (from a TV)", pick["language"], pick["provider"])
        return {"ok": True, "language": pick["language"], "provider": pick["provider"]}

    # -- upcoming episodes, for Orama (GET /api/tv/upcoming?days=7)
    UPCOMING_TTL = 600

    def tv_upcoming(self, days=7):
        days = max(1, min(30, int(days)))
        hit = self.__dict__.get("_upcoming_cache")
        if hit and hit[0] == days and time.time() - hit[1] < self.UPCOMING_TTL:
            return hit[2]
        cfg = Config(self.settings.all())
        if not cfg.sonarr.enabled:
            return {"items": [], "reason": "sonarr not set up"}
        arr = Arr("Sonarr", cfg.sonarr)
        start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 6 * 3600))
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))
        eps = arr.get("/calendar", start=start, end=end, includeSeries="true", unmonitored="false")
        items = []
        for e in eps:
            se = e.get("series") or {}
            if e.get("hasFile"):
                continue  # already here: it's in Plex
            items.append({
                "show": se.get("title") or "?", "year": se.get("year"), "tvdb": se.get("tvdbId"),
                "imdb": se.get("imdbId") or "", "season": e.get("seasonNumber"), "episode": e.get("episodeNumber"),
                "title": e.get("title") or "", "overview": e.get("overview") or "",
                "air": e.get("airDateUtc") or "", "poster": self._poster(se), "fanart": self._image(se, "fanart"),
            })
        items.sort(key=lambda x: x["air"])
        res = {"items": items[:40]}
        self._upcoming_cache = (days, time.time(), res)
        return res

    @staticmethod
    def _image(obj, kind):
        for im in obj.get("images") or []:
            if im.get("coverType") == kind:
                return im.get("remoteUrl") or ""
        return ""

    def tv_queue(self):
        now = time.time()
        cached = getattr(self, "_queue_cache", None)
        if cached and now - cached[0] < self.QUEUE_TTL:
            return cached[1]
        cfg = Config(self.settings.all())
        items, errors = [], {}
        for name, acfg, kind in (("radarr", cfg.radarr, "movie"), ("sonarr", cfg.sonarr, "show")):
            if not acfg.enabled:
                continue
            arr = Arr(name.capitalize(), acfg)
            try:
                q = self._queue_of(arr, kind)
                ids = {x["tmdb"] if kind == "movie" else x["tvdb"] for x in q}
                items += q
                try:
                    items += self._searching(arr, kind, ids)
                except requests.RequestException as e:
                    log.debug("searching check failed for %s: %s", name, e)
            except requests.RequestException as e:
                errors[name] = str(e)[:200]
        order = {"downloading": 0, "importing": 1, "stalled": 2, "paused": 3, "queued": 4, "searching": 5, "failed": 6}
        items.sort(key=lambda x: (order.get(x["status"], 9), -(x["progress"] or 0)))
        result = {"items": items, "errors": errors}
        self._queue_cache = (now, result)
        return result

    def _nudge_bazarr(self, summary, cfg=None):
        """Make Bazarr pick up the new series/movies now instead of on its next sync."""
        cfg = cfg or self.cfg
        if not (cfg.bazarr_url and cfg.bazarr_key) or cfg.dry_run:
            return
        tasks = []
        if summary["added_series"]:
            tasks.append("update_series")
        if summary["added_movies"]:
            tasks.append("update_movies")
        url = cfg.bazarr_url.rstrip("/") + "/api/system/tasks"
        for task in tasks:
            try:
                r = requests.post(url, params={"taskid": task},
                                  headers={"X-API-KEY": cfg.bazarr_key}, timeout=30)
                r.raise_for_status()
                log.info("Bazarr: triggered %s", task)
            except Exception as e:
                log.warning("Bazarr: could not trigger %s: %s", task, e)
                summary["errors"].append(f"bazarr:{task}: {e}")


# --------------------------------------------------------------------------- request origin
# Browsers let any website send requests to addresses on your home network. So
# commands are only accepted from this page itself, the TV app, or another
# device on your network; and requests must name this server by an address or
# a local name (a public domain pointing at it is how "DNS rebinding" works).
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".ts.net")


def _extra_hosts():
    return {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}


def _local_name(host):
    """An IP address, localhost, a bare machine name, or a home-network name."""
    host = (host or "").strip("[]").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES) or host in _extra_hosts()


def _private_host(host):
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES) or host in _extra_hosts():
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return "." not in host and bool(host)


def host_allowed(host_header):
    return _local_name(urlparse("//" + (host_header or "")).hostname)


def origin_allowed(origin, host_header):
    """No Origin (curl, scripts), a non-web origin (the TV app runs from file://),
    this server's own page, or a page on another home-network device."""
    if not origin or origin == "null":
        return True
    u = urlparse(origin)
    if u.scheme not in ("http", "https"):
        return True
    own = urlparse("//" + (host_header or "")).hostname
    return (u.hostname or "") == (own or "") or _private_host(u.hostname)


# --------------------------------------------------------------------------- web ui / api
def make_handler(syncer, ui_password):
    with open(os.path.join(HERE, "ui.html"), "rb") as f:
        page = f.read()

    class H(BaseHTTPRequestHandler):
        def _tv(self):
            return self.path.split("?")[0].startswith("/api/tv")

        def _cors(self):
            # Only the TV endpoints are cross-origin: a TV app's page isn't served
            # from here. They still require UI_PASSWORD when it is set, and only
            # origins on your own network get an answer (see origin_allowed).
            origin = self.headers.get("Origin")
            if self._tv() and origin_allowed(origin, self.headers.get("Host")):
                self.send_header("Access-Control-Allow-Origin", origin or "*")
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")

        def _send(self, code, obj=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _guard(self, command=False):
            """Refuse requests a foreign website could have made through your browser."""
            if not host_allowed(self.headers.get("Host")):
                self._send(403, {"error": "Unknown host name: open this by its IP address, or add the "
                                          "name to ALLOWED_HOSTS"})
                return False
            if command and not origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
                self._send(403, {"error": "Request from another website refused"})
                return False
            return True

        def do_OPTIONS(self):
            # CORS preflight carries no credentials, so it skips _authed.
            if not self._guard(command=True):
                return
            self.send_response(204 if self._tv() else 404)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authed(self):
            if not ui_password or self.path.startswith("/health"):
                return True
            hdr = self.headers.get("Authorization", "")
            if hdr.startswith("Basic "):
                try:
                    _, pw = base64.b64decode(hdr[6:]).decode().split(":", 1)
                    if hmac.compare_digest(pw, ui_password):
                        return True
                except Exception:
                    pass
            self.send_response(401)
            if self._tv():
                self._cors()  # let the TV read the 401 and ask for the password
            else:
                self.send_header("WWW-Authenticate", 'Basic realm="watchlist-sync"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            if not self.path.startswith("/health") and not self._guard():
                return
            if not self._authed():
                return
            p = self.path.split("?")[0]
            if p == "/health":
                return self._send(200, {"ok": True})
            if p == "/":
                return self._send(200, raw=page, ctype="text/html; charset=utf-8")
            if p == "/api/status":
                cfg = Config(syncer.settings.all())
                return self._send(200, {
                    "version": VERSION,
                    "account": json.loads(syncer.state.get("plex_account", "null")),
                    "problem": syncer.setup_problem(cfg),
                    "running": syncer.running,
                    "interval_min": cfg.interval_min,
                    "dry_run": cfg.dry_run,
                    "last_run": syncer.last_run,
                    "users": syncer.users,
                    "recent": syncer.state.recent(),
                    "action": syncer.action,
                })
            if p == "/api/settings":
                return self._send(200, syncer.settings.public())
            if p == "/api/tv":
                return self._send(200, syncer.tv_info())
            if p == "/api/tv/queue":
                return self._send(200, syncer.tv_queue())
            if p == "/api/tv/collection":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    tmdb = int((qs.get("tmdb") or [""])[0])
                except ValueError:
                    return self._send(400, {"error": "tmdb id needed"})
                try:
                    return self._send(200, syncer.tv_collection(tmdb))
                except requests.RequestException as e:
                    return self._send(502, {"error": "Radarr: " + str(e)[:200]})
            if p == "/api/tv/upcoming":
                qs = parse_qs(urlparse(self.path).query)
                try:
                    return self._send(200, syncer.tv_upcoming(int((qs.get("days") or ["7"])[0])))
                except ValueError:
                    return self._send(400, {"error": "bad days"})
                except requests.RequestException as e:
                    return self._send(502, {"error": "Sonarr: " + str(e)[:200]})
            if p == "/api/tv/lookup":
                qs = parse_qs(urlparse(self.path).query)
                q = lambda k, d="": (qs.get(k) or [d])[0]
                kind = q("kind")
                if kind not in ("movie", "show"):
                    return self._send(400, {"error": "kind must be movie or show"})
                try:
                    ext_id = int(q("id", "0") or 0)
                except ValueError:
                    return self._send(400, {"error": "bad id"})
                term = q("q").strip()[:200]
                if not term and not ext_id:
                    return self._send(400, {"error": "q or id needed"})
                try:
                    return self._send(200, syncer.tv_lookup(kind, term, ext_id))
                except LookupError as e:
                    return self._send(409, {"error": str(e)})
                except requests.RequestException as e:
                    return self._send(502, {"error": ("Radarr: " if kind == "movie" else "Sonarr: ") + str(e)[:200]})
            if p == "/api/tv/subtitles":
                qs = parse_qs(urlparse(self.path).query)
                q = lambda k, d="": (qs.get(k) or [d])[0]
                kind = q("kind")
                if kind not in ("movie", "episode"):
                    return self._send(400, {"error": "kind must be movie or episode"})
                try:
                    num = lambda k: int(q(k, "0") or 0)
                    res = syncer.tv_subtitle_search(kind, tmdb=num("tmdb"), tvdb=num("tvdb"), imdb=q("imdb"),
                                                    season=num("season"), episode=num("episode"), lang=q("lang"))
                except ValueError:
                    return self._send(400, {"error": "bad number"})
                except LookupError as e:
                    return self._send(409, {"error": str(e)})
                except requests.RequestException as e:
                    return self._send(502, {"error": "Couldn't reach Bazarr/Sonarr/Radarr: " + str(e)[:150]})
                return self._send(200, res)
            m = re.fullmatch(r"/api/users/([^/]+)/plan", p)
            if m:
                return self._send(200, syncer.deletion_plan(unquote(m.group(1))))
            m = re.fullmatch(r"/api/plex/pin/(\d+)", p)
            if m:
                try:
                    token = plex_check_pin(syncer.client_id, m.group(1))
                except Exception as e:
                    return self._send(502, {"error": str(e)})
                if not token:
                    return self._send(200, {"done": False})
                acct = plex_account(syncer.client_id, token)
                syncer.settings.update({"plex_token": token})
                syncer.state.set("plex_account", json.dumps(acct))
                log.info("Signed in to Plex as %s", acct["username"])
                syncer.wake.set()
                return self._send(200, {"done": True, "account": acct})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard(command=True):
                return
            if not self._authed():
                return
            p = self.path.split("?")[0]
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if p in ("/sync", "/api/sync"):
                syncer.wake.set()
                return self._send(202, {"queued": True})
            if p == "/api/tv/sync":
                # A TV just added something to the Plex Watchlist: sync now.
                syncer.wake.set()
                return self._send(202, {"queued": True})
            if p == "/api/tv/delete":
                kind = body.get("kind")
                if kind not in ("movie", "show"):
                    return self._send(400, {"error": "kind must be movie or show"})
                try:
                    ext_id = int(body.get("tmdb" if kind == "movie" else "tvdb") or 0)
                except (TypeError, ValueError):
                    ext_id = 0
                imdb = str(body.get("imdb") or "")
                if not ext_id and not imdb:
                    return self._send(400, {"error": "No TMDB/TVDB or IMDb id for this title"})
                try:
                    return self._send(200, syncer.tv_delete(kind, ext_id, imdb, str(body.get("title") or "")))
                except (requests.RequestException, ArrError) as e:
                    svc = "Radarr" if kind == "movie" else "Sonarr"
                    return self._send(502, {"error": f"{svc}: {str(e)[:200]}"})
            if p == "/api/tv/request":
                kind = body.get("kind")
                if kind not in ("movie", "show"):
                    return self._send(400, {"error": "kind must be movie or show"})
                try:
                    ext_id = int(body.get("tmdb" if kind == "movie" else "tvdb") or 0)
                except (TypeError, ValueError):
                    ext_id = 0
                if ext_id <= 0:
                    return self._send(400, {"error": "TMDB (movie) or TVDB (show) id needed"})
                user = re.sub(r"[^\w .@-]", "", str(body.get("user") or ""))[:40].strip()
                try:
                    return self._send(200, syncer.tv_request(kind, ext_id, user))
                except LookupError as e:
                    return self._send(409, {"error": str(e)})
                except (requests.RequestException, ArrError, RuntimeError) as e:
                    svc = "Radarr" if kind == "movie" else "Sonarr"
                    return self._send(502, {"error": f"{svc}: {str(e)[:200]}"})
            if p == "/api/tv/subtitles/download":
                try:
                    return self._send(200, syncer.tv_subtitle_download(str(body.get("token") or ""), int(body.get("index", -1))))
                except (TypeError, ValueError):
                    return self._send(400, {"error": "bad index"})
                except LookupError as e:
                    return self._send(409, {"error": str(e)})
                except requests.RequestException as e:
                    return self._send(502, {"error": "Couldn't reach Bazarr: " + str(e)[:150]})
            if p == "/api/plex/pin":
                try:
                    return self._send(200, plex_create_pin(syncer.client_id))
                except Exception as e:
                    return self._send(502, {"error": f"Could not reach plex.tv: {e}"})
            if p == "/api/plex/logout":
                syncer.settings.update({"plex_token": ""})
                syncer.state.set("plex_account", "null")
                syncer.users = []
                return self._send(200, {"ok": True})
            if p == "/api/settings":
                body.pop("plex_token", None)  # only the sign-in flow sets this
                syncer.settings.update(body)
                syncer.wake.set()
                return self._send(200, syncer.settings.public())
            m = re.fullmatch(r"/api/users/([^/]+)/(exclude|include)", p)
            if m:
                name = unquote(m.group(1))
                if m.group(2) == "include":
                    syncer.include_user(name)
                    return self._send(200, {"ok": True})
                if syncer.action and syncer.action.get("state") == "running":
                    return self._send(409, {"error": "Another exclusion is still running"})
                plan = syncer.exclude_user(name, bool(body.get("delete")))
                return self._send(200, {"ok": True, "plan": plan})
            m = re.fullmatch(r"/api/test/(sonarr|radarr|bazarr)", p)
            if m:
                svc = m.group(1)
                cur = syncer.settings.all()
                url = (body.get("url") or cur[f"{svc}_url"]).strip()
                key = (body.get("api_key") or cur[f"{svc}_api_key"]).strip()
                if not (url and key):
                    return self._send(400, {"error": "URL and API key are required"})
                try:
                    if svc == "bazarr":
                        info = bazarr_probe(url, key)
                    else:
                        fake = type("C", (), {"url": url, "api_key": key, "quality_profile": "", "root_folder": ""})
                        info = Arr(svc.title(), fake).probe()
                    return self._send(200, {"ok": True, **info})
                except Exception as e:
                    return self._send(200, {"ok": False, "error": str(e)})
            self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return H


# --------------------------------------------------------------------------- main
def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    syncer = Syncer(os.environ.get("DATA_DIR", "/data"))
    port = int(os.environ.get("PORT", os.environ.get("STATUS_PORT", "8789")))
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(syncer, os.environ.get("UI_PASSWORD", "")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Web UI on http://<this-host>:%d", port)

    once = "--once" in sys.argv
    while True:
        try:
            syncer.run_once()
        except Exception:
            log.exception("Sync cycle failed")
        if once:
            break
        interval = syncer.cfg.interval_min if syncer.cfg else 15
        syncer.wake.wait(interval * 60)
        syncer.wake.clear()


if __name__ == "__main__":
    main()
