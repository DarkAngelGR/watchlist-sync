#!/usr/bin/env python3
"""Local, read-only personalized ranking for Orama / watchlist-sync.

Only metadata and watched-state information are read. No media files are accessed.
A single explicitly configured Plex account or Jellyfin user is used per instance.
"""
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

log = logging.getLogger("orama-ai")
SOURCE = os.getenv("MEDIA_SOURCE", "plex").strip().lower()
PLEX_URL = os.getenv("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")
PLEX_ACCOUNT_ID = os.getenv("PLEX_ACCOUNT_ID", "").strip()
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
JELLYFIN_USER_ID = os.getenv("JELLYFIN_USER_ID", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen3-embedding:0.6b")
API_KEY = os.getenv("API_KEY", "")
DATA_DIR = os.getenv("DATA_DIR", "/data")
HISTORY_LIMIT = min(4000, max(50, int(os.getenv("HISTORY_LIMIT", "1500"))))
REFRESH_MINUTES = max(5, int(os.getenv("REFRESH_MINUTES", "15")))
PORT = int(os.getenv("PORT", "8788"))
STOPWORDS = set("a an and at by for from in is it of on or the to with this that some about into after who what are".split())


def get_json(url, headers=None, timeout=15):
    req = Request(url, headers=headers or {})
    with urlopen(req, timeout=timeout) as response:
        return json.load(response)


def get_plex(path, params=None):
    qs = urlencode(params or {})
    url = PLEX_URL + path + ("?" + qs if qs else "")
    req = Request(url, headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"})
    with urlopen(req, timeout=15) as response:
        body = response.read()
    if body.lstrip().startswith(b"<"):
        root = ET.fromstring(body)
        items = []
        for node in root:
            entry = dict(node.attrib)
            entry["Genre"] = [dict(x.attrib) for x in node if x.tag == "Genre"]
            items.append(entry)
        return {"MediaContainer": {"Metadata": items, "totalSize": root.get("totalSize")}}
    return json.loads(body)


def plex_preferences():
    """Plex history is individual completion events (not playback progress)."""
    count = defaultdict(Counter)
    labels = {}
    seen_history = set()
    last_seen = {}
    for start in range(0, HISTORY_LIMIT, 200):
        page = get_plex("/status/sessions/history/all", {
            "accountID": PLEX_ACCOUNT_ID, "sort": "viewedAt:desc",
            "X-Plex-Container-Start": start, "X-Plex-Container-Size": 200,
        }).get("MediaContainer", {})
        rows = page.get("Metadata") or []
        if not rows:
            break
        fresh_events = 0
        for row in rows:
            # A server that ignores paging must not fabricate rewatch signals.
            history_id = str(row.get("historyKey") or row.get("id") or "")
            if history_id and history_id in seen_history:
                continue
            if history_id:
                seen_history.add(history_id)
            fresh_events += 1
            kind = row.get("type")
            if kind == "movie":
                item_key = str(row.get("ratingKey") or "")
                if not item_key:
                    continue
                group_key = "movie:" + item_key
                episode_key = item_key
                title = row.get("title", "")
            elif kind == "episode":
                show_key = str(row.get("grandparentRatingKey") or (row.get("grandparentKey") or "").split("/")[-1])
                episode_key = str(row.get("ratingKey") or "")
                if not show_key or not episode_key:
                    continue
                group_key = "show:" + show_key
                item_key = show_key
                title = row.get("grandparentTitle") or ""
            else:
                continue
            # Plex playback-history records are plays, not proof of a full watch.
            # Only discard events when an explicit completion fraction is present.
            try:
                position, duration = float(row.get("viewOffset") or 0), float(row.get("duration") or 0)
                if position > 0 and duration > 0 and position / duration < .9:
                    continue
            except (ValueError, TypeError):
                pass
            count[group_key][episode_key] += 1
            labels[group_key] = (kind, item_key, title)
            try:
                last_seen[group_key] = max(last_seen.get(group_key, 0), int(row.get("viewedAt") or 0))
            except (TypeError, ValueError):
                pass
        if len(rows) < 200 or not fresh_events:
            break
    favorites = []
    for key, episodes in count.items():
        kind, item_key, title = labels[key]
        repeats = sum(max(0, n - 1) for n in episodes.values())
        unique = len(episodes)
        # One full movie viewing is positive; rewatching it is a stronger signal.
        weight = 3 + min(24, repeats * 5) if kind == "movie" else 3 + min(18, unique * .35) + min(24, repeats * 5)
        # Keep historical favorites strong, but let recent interests influence ranking.
        when = last_seen.get(key, 0)
        if when:
            age_days = max(0, (time.time() - when) / 86400)
            weight *= .75 + .25 * math.exp(-age_days / 90)
        favorites.append({"kind": "movie" if kind == "movie" else "show",
                          "key": item_key, "title": title, "weight": weight})
    favorites.sort(key=lambda f: -f["weight"])
    for entry in favorites[:50]:
        try:
            rows = get_plex("/library/metadata/" + entry["key"]).get("MediaContainer", {}).get("Metadata") or []
            if rows:
                m = rows[0]
                entry["title"] = m.get("title") or entry["title"]
                entry["overview"] = m.get("summary") or ""
                entry["genres"] = [g.get("tag", "") for g in (m.get("Genre") or [])]
                entry["year"] = m.get("year") or ""
        except Exception as exc:
            log.warning("Plex metadata unavailable for %s: %s", entry["key"], type(exc).__name__)
    return favorites[:50]


def jellyfin_preferences():
    """Jellyfin user-scoped played state and PlayCount, grouped by series."""
    values = {}
    page_size = 250
    for start in range(0, 5000, page_size):
        url = JELLYFIN_URL + "/Users/" + JELLYFIN_USER_ID + "/Items?" + urlencode({
            "Recursive": "true", "IncludeItemTypes": "Movie,Episode",
            "Fields": "Genres,Overview", "StartIndex": start, "Limit": page_size,
        })
        data = get_json(url, {"X-Emby-Token": JELLYFIN_API_KEY})
        rows = data.get("Items") or []
        for row in rows:
            ud = row.get("UserData") or {}
            # Jellyfin PlayCount can increase on playback start. Do not infer a
            # completed watch from PlayCount alone; prefer Played or >=90%.
            fraction = 0.
            try:
                fraction = float(ud.get("PlaybackPositionTicks") or 0) / float(row.get("RunTimeTicks") or 0)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            if not (ud.get("Played") or fraction >= .9):
                continue
            plays = max(int(ud.get("PlayCount") or 0), 1)
            if row.get("Type") == "Movie":
                key = "movie:" + str(row.get("Id", ""))
                title = row.get("Name", "")
                kind = "movie"
                ep_key = str(row.get("Id", ""))
            elif row.get("Type") == "Episode" and row.get("SeriesId"):
                key = "show:" + str(row["SeriesId"])
                title = row.get("SeriesName", "")
                kind = "show"
                ep_key = str(row.get("Id", ""))
            else:
                continue
            item = values.setdefault(key, {"key": key.split(":", 1)[1], "title": title, "kind": kind,
                                           "episodes": Counter(), "genres": row.get("Genres") or [],
                                           "overview": row.get("Overview") or ""})
            item["episodes"][ep_key] = max(item["episodes"][ep_key], plays)
        if len(rows) < page_size:
            break
    favorites = []
    for item in values.values():
        counts = item.pop("episodes")
        unique = len(counts)
        repeats = sum(max(0, n - 1) for n in counts.values())
        item["weight"] = (3 + min(24, repeats * 5) if item["kind"] == "movie"
                          else 3 + min(18, unique * .35) + min(24, repeats * 5))
        favorites.append(item)
    favorites.sort(key=lambda f: -f["weight"])
    for item in favorites[:50]:
        if item["kind"] != "show":
            continue
        try:
            info = get_json(JELLYFIN_URL + "/Users/" + JELLYFIN_USER_ID + "/Items/" + item["key"],
                            {"X-Emby-Token": JELLYFIN_API_KEY})
            item["genres"] = info.get("Genres") or item["genres"]
            item["overview"] = info.get("Overview") or item["overview"]
            item["year"] = info.get("ProductionYear") or ""
        except Exception as exc:
            log.warning("Jellyfin series metadata unavailable: %s", type(exc).__name__)
    return favorites[:50]


def terms(item):
    txt = str(item.get("title") or "") + " " + str(item.get("overview") or "")[:750]
    return {w for w in re.findall(r"[a-zA-Z]{3,}", txt.lower()) if w not in STOPWORDS}


def genres(item):
    return {str(g).strip().lower() for g in (item.get("genres") or []) if isinstance(g, str)}


def description(item):
    return "Title: " + str(item.get("title") or "")[:150] + "\nGenres: " + ", ".join(sorted(genres(item))) + "\nPlot: " + str(item.get("overview") or "")[:800]


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.
    return sum(x * y for x, y in zip(a, b))


def normalize(values):
    length = math.sqrt(sum(x * x for x in values))
    return [x / length for x in values] if length else []


def metadata_score(candidate, favorite):
    cg, fg = genres(candidate), genres(favorite)
    overlap = len(cg & fg) / max(1, len(cg | fg)) if cg and fg else 0.
    ct, ft = terms(candidate), terms(favorite)
    keyword = len(ct & ft) / max(1, len(ct | ft)) if ct and ft else 0.
    return overlap * .75 + keyword * .25


class Recommender:
    def __init__(self, data_dir=DATA_DIR, start_worker=True):
        os.makedirs(data_dir, exist_ok=True)
        self.db = os.path.join(data_dir, "embeddings.sqlite3")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS embedding (key TEXT PRIMARY KEY, vector TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS feedback (kind TEXT NOT NULL, item_id TEXT NOT NULL,"
                         " action TEXT NOT NULL, updated_at INTEGER NOT NULL,"
                         " PRIMARY KEY(kind, item_id))")
        self.lock = threading.RLock()
        self.embed_lock = threading.Lock()
        self.favorites = []
        self.last_synced = 0
        self.last_error = ""
        if start_worker:
            threading.Thread(target=self._refresh_forever, daemon=True).start()

    def refresh(self):
        favorites = plex_preferences() if SOURCE == "plex" else jellyfin_preferences()
        with self.lock:
            self.favorites = favorites
            self.last_synced = int(time.time())
            self.last_error = ""
        return len(favorites)

    def _refresh_forever(self):
        while True:
            try:
                self.refresh()
            except Exception as exc:
                self.last_error = type(exc).__name__ + ": " + str(exc)[:160]
                log.warning("History refresh failed: %s", self.last_error)
            time.sleep(REFRESH_MINUTES * 60)

    def _key(self, text):
        return hashlib.sha256((EMBED_MODEL + "\0" + text).encode()).hexdigest()

    def cached_vectors(self, texts):
        with sqlite3.connect(self.db) as conn:
            result = {}
            for txt in set(texts):
                row = conn.execute("SELECT vector FROM embedding WHERE key=?", (self._key(txt),)).fetchone()
                if row:
                    result[txt] = json.loads(row[0])
        return result

    def _embed_missing(self, texts):
        try:
            known = self.cached_vectors(texts)
            missing = [t for t in dict.fromkeys(texts) if t not in known]
            for start in range(0, len(missing), 8):
                batch = missing[start:start + 8]
                payload = json.dumps({"model": EMBED_MODEL, "input": batch}).encode()
                req = Request(OLLAMA_URL + "/api/embed", payload,
                              {"Content-Type": "application/json"}, method="POST")
                with urlopen(req, timeout=180) as resp:
                    vectors = json.load(resp)["embeddings"]
                if len(vectors) != len(batch):
                    raise ValueError("Ollama returned an unexpected embedding count")
                with sqlite3.connect(self.db) as conn:
                    conn.executemany("INSERT OR REPLACE INTO embedding(key, vector) VALUES(?, ?)", [
                        (self._key(text), json.dumps(normalize(vector)))
                        for text, vector in zip(batch, vectors)
                    ])
        except Exception as exc:
            log.warning("Embedding update failed; metadata scoring remains available: %s", type(exc).__name__)
        finally:
            self.embed_lock.release()

    def feedback(self, kind, item_id, action):
        """Store only a title ID and explicit preference, never a token or history."""
        if kind not in ("movie", "show") or not str(item_id).isdigit() or not 0 < int(item_id) < 1000000000:
            raise ValueError("invalid movie/show identifier")
        if action not in ("more", "less", "hide", "clear"):
            raise ValueError("invalid feedback action")
        with sqlite3.connect(self.db, timeout=5) as conn:
            if action == "clear":
                conn.execute("DELETE FROM feedback WHERE kind=? AND item_id=?", (kind, str(int(item_id))))
            else:
                conn.execute("INSERT OR REPLACE INTO feedback(kind,item_id,action,updated_at)"
                             " VALUES(?,?,?,?)", (kind, str(int(item_id)), action, int(time.time())))
        return {"ok": True, "action": action}

    def _feedback(self):
        with sqlite3.connect(self.db, timeout=5) as conn:
            return {(kind, item_id): action for kind, item_id, action in
                    conn.execute("SELECT kind,item_id,action FROM feedback")}

    def rank(self, candidates, diversity=35):
        """Rank candidates with taste, feedback and a deterministic diversity penalty.

        The ranking stays responsive on CPU: embeddings are generated and
        persisted asynchronously, and missing embeddings fall back to metadata.
        """
        with self.lock:
            favorites = list(self.favorites)
        if not favorites:
            return {"personalized": False, "items": candidates}
        try:
            diversity = max(0, min(100, int(diversity)))
        except (TypeError, ValueError):
            diversity = 35
        favorites = favorites[:30]
        candidates = [x for x in candidates if isinstance(x, dict) and x.get("title")][:120]
        labels = self._feedback()
        texts = [description(x) for x in favorites + candidates]
        vectors = self.cached_vectors(texts)
        if len(vectors) < len(set(texts)) and self.embed_lock.acquire(blocking=False):
            threading.Thread(target=self._embed_missing, args=(texts,), daemon=True).start()
        weight_total = sum(math.sqrt(f["weight"]) for f in favorites) or 1.
        scored = []
        for index, candidate in enumerate(candidates):
            kind = candidate.get("kind")
            identifier = candidate.get("tmdb") if kind == "movie" else candidate.get("tvdb")
            if kind not in ("movie", "show") or not str(identifier or "").isdigit():
                continue
            marker = (kind, str(int(identifier)))
            vote = labels.get(marker, "")
            if vote == "hide":
                continue
            compared = [(metadata_score(candidate, f), f) for f in favorites]
            highest = max(compared, key=lambda x: x[0] * math.sqrt(x[1]["weight"]))
            metadata = highest[0] * math.sqrt(highest[1]["weight"]) / 6
            weighted = sum(sim * math.sqrt(f["weight"]) for sim, f in compared) / weight_total
            score = .6 * metadata + .4 * weighted
            cv = vectors.get(description(candidate))
            if cv:
                semantic = [(cosine(cv, vectors[description(f)]), f["weight"])
                            for f in favorites if description(f) in vectors]
                if semantic:
                    score += .65 * max(v * min(1.7, math.sqrt(w) / 3) for v, w in semantic)
                    score += .35 * (sum(v * math.sqrt(w) for v, w in semantic) /
                                    sum(math.sqrt(w) for _, w in semantic))
            score += .6 if vote == "more" else -.6 if vote == "less" else 0.
            favorite = highest[1]
            reason = ("More like titles you've enjoyed" if vote == "more"
                      else "Similar to " + str(favorite.get("title") or "your watched titles")[:75])
            ranked = dict(candidate)
            ranked["ai_reason"] = reason
            scored.append((score, index, ranked))
        # Maximal marginal relevance: high exploration spreads genres/topics,
        # low exploration favors highest match to favorites.
        picked, remaining = [], list(scored)
        max_similarity = {row[1]: 0. for row in remaining}
        while remaining:
            current = max(remaining, key=lambda row: (
                row[0] - (diversity / 100) * .42 * max_similarity[row[1]], -row[1]))
            remaining.remove(current)
            picked.append(current)
            if diversity:
                for row in remaining:
                    max_similarity[row[1]] = max(max_similarity[row[1]],
                                                  metadata_score(row[2], current[2]))
        items = [row[2] for row in picked]
        # A Hidden Gems row is conservative: non-trending, non-popular
        # candidates with a reasonably positive community score.
        gems = [row[2] for row in picked if not set(row[2].get("source_rows") or []) &
                {"trending", "popular"} and float(row[2].get("rating") or 0) >= 6.5]
        return {"personalized": True, "items": items,
                "hidden_gems": gems[:12]}


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self.respond(404, {"error": "not found"})
            with engine.lock:
                return self.respond(200, {"ok": bool(engine.last_synced), "source": SOURCE,
                                          "history_items": len(engine.favorites),
                                          "last_synced": engine.last_synced,
                                          "error": engine.last_error})

        def do_POST(self):
            if self.path not in ("/v1/rank", "/v1/feedback"):
                return self.respond(404, {"error": "not found"})
            token = self.headers.get("Authorization") or ""
            if not hmac.compare_digest(token, "Bearer " + API_KEY):
                return self.respond(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if not 0 < length <= 256000:
                    return self.respond(413, {"error": "invalid body size"})
                body = json.loads(self.rfile.read(length))
                if self.path == "/v1/feedback":
                    return self.respond(200, engine.feedback(body.get("kind"), body.get("id"), body.get("action")))
                items = body.get("items")
                if not isinstance(items, list) or len(items) > 120:
                    return self.respond(400, {"error": "items must be a list of up to 120 items"})
                return self.respond(200, engine.rank(items, body.get("diversity", 35)))
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                return self.respond(400, {"error": "invalid JSON"})

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)
    return Handler


def validate_config():
    if not API_KEY or len(API_KEY) < 24:
        raise ValueError("Set API_KEY to a random secret of at least 24 characters")
    if SOURCE == "plex":
        if not (PLEX_URL.startswith(("http://", "https://")) and PLEX_TOKEN and PLEX_ACCOUNT_ID.isdecimal()):
            raise ValueError("Plex requires PLEX_URL, PLEX_TOKEN and a numeric PLEX_ACCOUNT_ID")
    elif SOURCE == "jellyfin":
        if not (JELLYFIN_URL.startswith(("http://", "https://")) and JELLYFIN_API_KEY and JELLYFIN_USER_ID):
            raise ValueError("Jellyfin requires JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_USER_ID")
    else:
        raise ValueError("MEDIA_SOURCE must be plex or jellyfin")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    validate_config()
    engine = Recommender()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(engine))
    log.info("Orama AI ranking service listening on port %s", PORT)
    server.serve_forever()
