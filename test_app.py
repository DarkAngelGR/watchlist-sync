import json, os, re, tempfile
import requests
import app

calls = []
pin_polls = []


class Resp:
    def __init__(self, code=200, body=None, content=None):
        self.status_code, self._b, self.headers = code, body, {}
        self.content = content or json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


USERS_XML = b'<MediaContainer><User id="1" title="Maria" username="maria"/><User id="2" title="nikos" username="nikos"/></MediaContainer>'
META = {"101": ["tvdb://81189", "tmdb://1396"], "102": ["tmdb://27205", "imdb://tt1375666"],
        "201": ["tvdb://305288"], "202": ["tvdb://81189"], "203": ["tvdb://999"],
        "104": ["tvdb://424536"], "105": ["tvdb://371028"]}
owner_watchlist = [{"ratingKey": "101", "title": "Breaking Bad", "type": "show"},
                   {"ratingKey": "102", "title": "Inception", "type": "movie"}]
LOOKUP_EXTRA = {424536: {"genres": ["Anime", "Animation", "Fantasy"], "originalLanguage": {"name": "Japanese"}},
                371028: {"genres": ["Animation", "Action"], "originalLanguage": {"name": "English"}}}
sonarr_series, radarr_movies = [{"id": 1, "tvdbId": 999, "tags": []}], []
tags = {}


def fake(self, method, url, params=None, json=None, headers=None, timeout=None):
    m = re.search(r"/api/v3/(series|movie)/(\d+)$", url)
    if m and method in ("DELETE", "PUT"):
        coll = sonarr_series if m.group(1) == "series" else radarr_movies
        calls.append((method, url, params, json))
        idx = next(i for i, x in enumerate(coll) if x["id"] == int(m.group(2)))
        if method == "DELETE":
            assert params.get("deleteFiles") == "true"
            coll.pop(idx)
        else:
            coll[idx] = json
        return Resp(200, {})
    calls.append((method, url, params, json))
    if url.endswith("/ping"):
        return Resp(body={"status": "OK"})
    if url.endswith("/api/v2/pins") and method == "POST":
        return Resp(body={"id": 4242, "code": "abcd"})
    if "/api/v2/pins/4242" in url:
        pin_polls.append(1)
        return Resp(body={"authToken": "tok-from-pin" if len(pin_polls) > 1 else None})
    if url.endswith("/system/status"):
        if "6767" in url:
            return Resp(body={"data": {"bazarr_version": "1.5.3"}})
        return Resp(body={"version": "4.0.15"})
    if "plex.tv/api/v2/user" in url:
        return Resp(body={"username": "aggelos"})
    if "api/v2/ping" in url:
        return Resp(body={})
    if "watchlist/all" in url:
        return Resp(body={"MediaContainer": {"totalSize": len(owner_watchlist), "Metadata": owner_watchlist}})
    if "community.plex.tv" in url:
        if "allFriendsV2" in json["query"]:
            return Resp(body={"data": {"allFriendsV2": [
                {"user": {"id": "u-maria", "username": "maria"}},
                {"user": {"id": "u-nikos", "username": "nikos"}},
                {"user": {"id": "u-rando", "username": "rando"}}]}})
        uid = json["variables"]["uuid"]
        if uid == "u-nikos":
            return Resp(body={"errors": [{"message": "private"}]})
        if uid == "u-rando":
            raise AssertionError("non-server friend should be skipped")
        if "after" not in json["variables"]:
            return Resp(body={"data": {"user": {"watchlist": {
                "nodes": [{"id": "201", "title": "Stranger Things", "type": "SHOW"},
                          {"id": "202", "title": "Breaking Bad", "type": "SHOW"}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}}})
        return Resp(body={"data": {"user": {"watchlist": {
            "nodes": [{"id": "203", "title": "Already There", "type": "SHOW"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if url.endswith("/api/users"):
        return Resp(content=USERS_XML)
    m = re.search(r"/library/metadata/(\d+)", url)
    if m:
        return Resp(body={"MediaContainer": {"Metadata": [{"Guid": [{"id": g} for g in META[m.group(1)]]}]}})
    if "/api/system/tasks" in url:
        return Resp(204, {})
    # *arr
    if url.endswith("/qualityprofile"):
        return Resp(body=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}, {"id": 7, "name": "Anime"}])
    if url.endswith("/rootfolder"):
        return Resp(body=[{"path": "/tv/"}, {"path": "/anime/"}] if ":8989" in url else [{"path": "/movies"}])
    if url.endswith("/languageprofile"):
        return Resp(404, {})
    if url.endswith("/tag"):
        if method == "POST":
            tags[json["label"]] = len(tags) + 1
            return Resp(body={"id": tags[json["label"]], "label": json["label"]})
        return Resp(body=[{"id": i, "label": l} for l, i in tags.items()])
    if url.endswith("/series") and method == "GET":
        return Resp(body=sonarr_series)
    if url.endswith("/series/lookup"):
        tid = int(params["term"].split(":")[1])
        return Resp(body=[{"title": f"show{tid}", "tvdbId": tid, "seasons": [], **LOOKUP_EXTRA.get(tid, {})}])
    if url.endswith("/series") and method == "POST":
        sonarr_series.append({"id": len(sonarr_series) + 100, "tvdbId": json["tvdbId"], "tags": json["tags"], "title": json["title"]})
        return Resp(201, json)
    if url.endswith("/movie") and method == "GET":
        return Resp(body=radarr_movies)
    if "/movie/lookup/tmdb" in url:
        return Resp(body={"title": "Inception", "tmdbId": params["tmdbId"]})
    if url.endswith("/movie") and method == "POST":
        radarr_movies.append({"id": len(radarr_movies) + 200, "tmdbId": json["tmdbId"], "tags": json["tags"]})
        return Resp(201, json)
    raise AssertionError(f"unexpected {method} {url}")


requests.Session.request = fake
requests.post = lambda url, **kw: fake(None, "POST", url, **kw)
requests.get = lambda url, **kw: fake(None, "GET", url, **kw)

d = tempfile.mkdtemp()
os.environ.update({
    "DATA_DIR": d, "DRY_RUN": "false",
    "SONARR_URL": "http://localhost:8989", "SONARR_API_KEY": "k", "SONARR_QUALITY_PROFILE": "hd-1080p",
    "RADARR_URL": "http://localhost:7878", "RADARR_API_KEY": "k",
    "BAZARR_URL": "http://localhost:6767", "BAZARR_API_KEY": "k",
})
s = app.Syncer(d)

# --- web API: sign-in flow + settings
import threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(s, ""))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
def call(path, body=None):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or b"{}")

assert urllib.request.urlopen(base + "/").read().startswith(b"<!doctype html>")
skipped = s.run_once()
assert "Sign in with Plex" in skipped["skipped"], skipped
assert "Sign in" in call("/api/status")["problem"]
pin = call("/api/plex/pin", {})
assert pin["id"] == 4242 and "clientID=" in pin["url"] and "code=abcd" in pin["url"]
assert call(f"/api/plex/pin/{pin['id']}") == {"done": False}
done = call(f"/api/plex/pin/{pin['id']}")
assert done["done"] and done["account"]["username"] == "aggelos"
assert s.settings.all()["plex_token"] == "tok-from-pin"
pub = call("/api/settings")
assert "plex_token" not in pub and pub["plex_token_set"] and pub["sonarr_api_key_set"]
assert "sonarr_api_key" not in json.dumps(pub).replace("sonarr_api_key_set", "")
# blank api key in form keeps the saved one; profile saved by name
call("/api/settings", {"sonarr_api_key": "", "sonarr_quality_profile": "HD-1080p", "plex_token": "evil"})
assert s.settings.all()["sonarr_api_key"] == "k" and s.settings.all()["plex_token"] == "tok-from-pin"
t1 = call("/api/test/sonarr", {"url": "", "api_key": ""})
assert t1["ok"] and t1["version"] == "4.0.15" and t1["roots"] == ["/tv/", "/anime/"], t1
assert call("/api/test/bazarr", {})["version"] == "1.5.3"

r1 = s.run_once()
print("cycle 1:", json.dumps(r1, indent=1))

posts = [c for c in calls if c[0] == "POST" and c[1].endswith(("/series", "/movie"))]
bb = next(c[3] for c in posts if c[3].get("tvdbId") == 81189)
assert bb["qualityProfileId"] == 4 and bb["rootFolderPath"] == "/tv/"
assert len(bb["tags"]) == 2, bb["tags"]  # requested by owner + maria
assert bb["addOptions"]["searchForMissingEpisodes"] is True
assert r1["shows"] == 3 and r1["movies"] == 1
assert sorted(r1["added_series"]) == ["Breaking Bad", "Stranger Things"]
assert r1["added_movies"] == ["Inception"]
assert any(c[2] == {"taskid": "update_series"} for c in calls)
assert any(c[2] == {"taskid": "update_movies"} for c in calls)

calls.clear()
r2 = s.run_once()
assert not r2["added_series"] and not r2["added_movies"] and not r2["errors"]
assert not any("/library/metadata/" in c[1] for c in calls), "guids should be cached"
assert not any("system/tasks" in c[1] for c in calls)
st = call("/api/status")
assert {u["name"]: u["status"] for u in st["users"]} == {"aggelos": "ok", "maria": "ok", "nikos": "private"}, st["users"]
# --- exclude maria: Stranger Things only she wanted -> deleted; Breaking Bad shared with owner -> kept, untagged
import time as _t
def wait_action():
    for _ in range(100):
        a = call("/api/status")["action"]
        if a and a["state"] == "done":
            return a
        _t.sleep(0.05)
    raise AssertionError("exclusion never finished")

plan = call("/api/users/maria/plan")
assert [x["title"] for x in plan["delete"]] == ["Stranger Things"], plan
assert [(x["title"], x["others"]) for x in plan["keep"]] == [("Breaking Bad", ["aggelos"])], plan
assert all(x["title"] != "Already There" for x in plan["delete"] + plan["keep"])  # never ours to delete
calls.clear()
call("/api/users/maria/exclude", {"delete": True})
act = wait_action()
assert act["deleted"] == ["Stranger Things"] and act["untagged"] == ["Breaking Bad"] and not act["errors"], act
dels = [c for c in calls if c[0] == "DELETE"]
assert len(dels) == 1 and dels[0][2]["deleteFiles"] == "true"
assert 305288 not in {x["tvdbId"] for x in sonarr_series}
bb = next(x for x in sonarr_series if x["tvdbId"] == 81189)
assert tags["plex-maria"] not in bb["tags"] and tags["plex-aggelos"] in bb["tags"], bb
assert 999 in {x["tvdbId"] for x in sonarr_series}  # pre-existing show untouched
assert "maria" in s.settings.all()["user_block"]
# next sync must not re-add what we deleted
r3 = s.run_once()
assert not r3["added_series"], r3
assert 305288 not in {x["tvdbId"] for x in sonarr_series}
assert {u["name"]: u["status"] for u in call("/api/status")["users"]}["maria"] == "excluded"

# --- include her again -> Stranger Things comes back
call("/api/users/maria/include", {})
assert "maria" not in s.settings.all()["user_block"]
r4 = s.run_once()
assert r4["added_series"] == ["Stranger Things"], r4

# --- exclude without deleting -> nothing removed
calls.clear()
call("/api/users/maria/exclude", {"delete": False})
wait_action()
assert not [c for c in calls if c[0] == "DELETE"]
call("/api/users/maria/include", {}); s.run_once()

# --- dry run: plan shown, nothing deleted
s.settings.update({"dry_run": True}); calls.clear()
call("/api/users/aggelos/exclude", {"delete": True})
act = wait_action()
assert act["deleted"] == ["Inception"], act   # Breaking Bad kept: maria wants it
assert not [c for c in calls if c[0] in ("DELETE", "PUT")]
call("/api/users/aggelos/include", {})
print("exclusion tests OK")

# --- TV clients (FlexPlex): status + "sync now" after a Watchlist add
info = call("/api/tv")
assert info["product"] == "watchlist-sync" and info["radarr"] and info["sonarr"], info
assert info["radarr_ok"] is True and info["sonarr_ok"] is True, info      # /ping answered
assert info["radarr_url"] == "http://localhost:7878" and info["signed_in"] is True, info
orig_get = requests.get
requests.get = lambda url, **kw: (_ for _ in ()).throw(requests.ConnectTimeout("timed out")) if ":7878/ping" in url else orig_get(url, **kw)
info = call("/api/tv")
assert info["radarr_ok"] is False and info["sonarr_ok"] is True, info     # unreachable Radarr is reported
requests.get = orig_get
s.wake.clear()
assert call("/api/tv/sync", {}) == {"queued": True}
assert s.wake.is_set()
for gone in ("/api/tv/suggestions",):
    try:
        call(gone); raise AssertionError("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404

# CORS only on /api/tv, preflight works, and UI_PASSWORD still applies
def raw(path, method="GET", headers=None, server=base):
    req = urllib.request.Request(server + path, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
code, h = raw("/api/tv/sync", "OPTIONS")
assert code == 204 and h["Access-Control-Allow-Origin"] == "*" and "Authorization" in h["Access-Control-Allow-Headers"]
assert raw("/api/tv")[1].get("Access-Control-Allow-Origin") == "*"
assert "Access-Control-Allow-Origin" not in raw("/api/status")[1]
# other websites can't drive it through your browser; the TV app and LAN pages can
evil = {"Origin": "https://evil.example", "Content-Type": "text/plain"}
assert raw("/api/sync", "POST", evil)[0] == 403
assert raw("/api/tv/delete", "OPTIONS", {"Origin": "https://evil.example"})[0] == 403
assert "Access-Control-Allow-Origin" not in raw("/api/tv", headers={"Origin": "https://evil.example"})[1]
assert raw("/api/settings", headers={"Host": "evil.example:8789"})[0] == 403          # DNS rebinding
assert raw("/health", headers={"Host": "evil.example"})[0] == 200
assert raw("/api/tv/sync", "POST", {"Origin": "file://"})[0] == 202                   # Orama on the TV
assert raw("/api/tv/sync", "OPTIONS", {"Origin": "null"})[1].get("Access-Control-Allow-Origin") == "null"
assert raw("/api/tv", headers={"Origin": "http://192.168.1.20:8800"})[1].get("Access-Control-Allow-Origin") == "http://192.168.1.20:8800"
assert raw("/api/settings", headers={"Host": "nas.local:8789"})[0] == 200
assert raw("/api/settings", headers={"Host": "nas.tail1234.ts.net"})[0] == 200
os.environ["ALLOWED_HOSTS"] = "sync.example.com"
assert raw("/api/settings", headers={"Host": "sync.example.com"})[0] == 200
del os.environ["ALLOWED_HOSTS"]
srv2 = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(s, "pw"))
threading.Thread(target=srv2.serve_forever, daemon=True).start()
b2 = f"http://127.0.0.1:{srv2.server_port}"
code, h = raw("/api/tv", server=b2)
assert code == 401 and h.get("Access-Control-Allow-Origin") == "*" and "WWW-Authenticate" not in h, (code, h)
assert raw("/api/status", server=b2)[1].get("WWW-Authenticate")    # the web UI still prompts
import base64 as b64
auth = {"Authorization": "Basic " + b64.b64encode(b"tv:pw").decode()}
assert raw("/api/tv", headers=auth, server=b2)[0] == 200
assert raw("/api/tv/sync", "OPTIONS", server=b2)[0] == 204         # preflight needs no password
srv2.shutdown()

# --- download progress for FlexPlex: Radarr + Sonarr queues, grouped, plus "searching"
prev = requests.Session.request
def queue_fake(self, method, url, params=None, json=None, headers=None, timeout=None):
    if url.endswith(":7878/api/v3/queue"):
        assert params["includeMovie"] == "true"
        return Resp(body={"records": [
            {"movieId": 5, "status": "downloading", "trackedDownloadState": "downloading", "size": 1000, "sizeleft": 250,
             "timeleft": "00:12:30", "movie": {"title": "Dune: Part Two", "year": 2024, "tmdbId": 693134, "imdbId": "tt15239678",
             "images": [{"coverType": "poster", "remoteUrl": "https://image.tmdb.org/p.jpg"}]}},
            {"movieId": 6, "status": "completed", "trackedDownloadState": "importPending", "size": 10, "sizeleft": 0,
             "movie": {"title": "Arrival", "year": 2016, "tmdbId": 329865}}]})
    if url.endswith(":8989/api/v3/queue"):
        return Resp(body={"records": [
            {"seriesId": 9, "status": "downloading", "size": 400, "sizeleft": 300, "timeleft": "1.02:00:00",
             "series": {"title": "Severance", "year": 2022, "tvdbId": 371980}, "episode": {"seasonNumber": 2, "episodeNumber": 1}},
            {"seriesId": 9, "status": "queued", "size": 600, "sizeleft": 600, "timeleft": None,
             "series": {"title": "Severance", "year": 2022, "tvdbId": 371980}, "episode": {"seasonNumber": 2, "episodeNumber": 2}}]})
    if url.endswith(":7878/api/v3/movie"):
        return Resp(body=[{"title": "Searching Movie", "year": 2025, "tmdbId": 4242, "hasFile": False, "monitored": True},
                          {"title": "Old Movie", "tmdbId": 1, "hasFile": False, "monitored": True}])
    if url.endswith(":8989/api/v3/series"):
        return Resp(body=[])
    return prev(self, method, url, params=params, json=json, headers=headers, timeout=timeout)
requests.Session.request = queue_fake
s.state.mark("radarr", 4242, "Searching Movie", {"aggelos"}, "added")
q = call("/api/tv/queue")
by = {x["title"]: x for x in q["items"]}
assert q["errors"] == {}, q
# downloading first (furthest along first), then importing, then searching
assert [x["title"] for x in q["items"]] == ["Dune: Part Two", "Severance", "Arrival", "Searching Movie"], [x["title"] for x in q["items"]]
d = by["Dune: Part Two"]
assert d["kind"] == "movie" and d["progress"] == 75.0 and d["status"] == "downloading" and d["timeleft"] == 750 and d["tmdb"] == 693134, d
assert d["poster"] == "https://image.tmdb.org/p.jpg", d
sv = by["Severance"]
assert sv["kind"] == "show" and sv["progress"] == 10.0 and sv["detail"] == "2 episodes" and sv["timeleft"] == 93600 and sv["status"] == "downloading", sv
assert by["Arrival"]["status"] == "importing", by["Arrival"]
assert by["Searching Movie"]["status"] == "searching" and "Old Movie" not in by, q   # only recent watchlist-sync adds
assert raw("/api/tv/queue")[1].get("Access-Control-Allow-Origin") == "*"
requests.Session.request = prev
# --- a movie's collection for FlexPlex (from Radarr)
prev2 = requests.Session.request
def coll_fake(self, method, url, params=None, json=None, headers=None, timeout=None):
    if url.endswith(":7878/api/v3/movie") and params and "tmdbId" in params:
        db = {155: {"tmdbId": 155, "title": "The Dark Knight", "hasFile": True, "monitored": True, "collection": {"title": "The Dark Knight Collection", "tmdbId": 263}},
              272: {"tmdbId": 272, "title": "Batman Begins", "hasFile": True, "monitored": True},
              49026: {"tmdbId": 49026, "title": "The Dark Knight Rises", "hasFile": False, "monitored": True}}
        m = db.get(int(params["tmdbId"]))
        return Resp(body=[m] if m else [])
    if url.endswith(":7878/api/v3/collection"):
        assert params["tmdbId"] == 263
        return Resp(body=[{"title": "The Dark Knight Collection", "tmdbId": 263, "movies": [
            {"tmdbId": 49026, "title": "The Dark Knight Rises", "year": 2012, "isExisting": True, "images": [{"coverType": "poster", "remoteUrl": "https://img/rises.jpg"}]},
            {"tmdbId": 155, "title": "The Dark Knight", "year": 2008, "isExisting": True},
            {"tmdbId": 272, "title": "Batman Begins", "year": 2005, "isExisting": True},
            {"tmdbId": 999, "title": "Joker Spin-off", "year": 2027, "isExisting": False}]}])
    if url.endswith(":7878/api/v3/movie/lookup/tmdb"):
        return Resp(body={"title": "Standalone"})
    return prev2(self, method, url, params=params, json=json, headers=headers, timeout=timeout)
requests.Session.request = coll_fake
c = call("/api/tv/collection?tmdb=155")["collection"]
assert c["title"] == "The Dark Knight Collection", c
assert [m["title"] for m in c["movies"]] == ["Batman Begins", "The Dark Knight", "The Dark Knight Rises", "Joker Spin-off"], c
byt = {m["title"]: m for m in c["movies"]}
assert byt["Batman Begins"]["hasFile"] and byt["The Dark Knight Rises"]["inRadarr"] and not byt["The Dark Knight Rises"]["hasFile"], c
assert not byt["Joker Spin-off"]["inRadarr"] and byt["The Dark Knight Rises"]["poster"] == "https://img/rises.jpg", c
assert call("/api/tv/collection?tmdb=1") == {"collection": None}
try:
    call("/api/tv/collection?tmdb=abc"); raise AssertionError("expected 400")
except urllib.error.HTTPError as e:
    assert e.code == 400
requests.Session.request = prev2
# --- deleting from a TV: Radarr/Sonarr delete with files, and it stays deleted
prev3 = requests.Session.request
deleted = []
def del_fake(self, method, url, params=None, json=None, headers=None, timeout=None):
    if method == "DELETE":
        deleted.append((url.split("/api/v3")[1], dict(params or {})))
        return Resp(body={})
    if url.endswith(":7878/api/v3/movie") and not params:
        return Resp(body=[{"id": 31, "tmdbId": 27205, "imdbId": "tt1375666", "title": "Inception"}])
    if url.endswith(":8989/api/v3/series") and not params:
        return Resp(body=[{"id": 41, "tvdbId": 121361, "imdbId": "tt0944947", "title": "Game of Thrones"}])
    return prev3(self, method, url, params=params, json=json, headers=headers, timeout=timeout)
requests.Session.request = del_fake
assert call("/api/tv/delete", {"kind": "movie", "tmdb": 27205})["dry_run"] and not deleted   # dry run deletes nothing
was_dry = s.settings.all()["dry_run"]
s.settings.update({"dry_run": False})
r = call("/api/tv/delete", {"kind": "movie", "tmdb": 27205, "title": "Inception"})
assert r["deleted"] and r["service"] == "radarr", r
assert deleted[-1] == ("/movie/31", {"deleteFiles": "true", "addImportExclusion": "false"}), deleted
r = call("/api/tv/delete", {"kind": "show", "imdb": "tt0944947"})          # no tvdb: found by IMDb id
assert r["deleted"] and deleted[-1][0] == "/series/41", (r, deleted)
assert call("/api/tv/delete", {"kind": "movie", "tmdb": 1})["found"] is False
for bad in ({"kind": "song", "tmdb": 1}, {"kind": "movie"}):
    try:
        call("/api/tv/delete", bad); raise AssertionError("expected 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400
# stays deleted while the same people have it listed; someone adding it again brings it back
s.state.mark("radarr", 27205, "Inception", {"aggelos", "maria"}, "deleted")
assert s._skip_deleted("radarr", 27205, {"aggelos", "maria"})
assert s._skip_deleted("radarr", 27205, {"maria"})                       # aggelos removed it from his watchlist
assert s.state.deleted_users("radarr", 27205) == {"maria"}
assert not s._skip_deleted("radarr", 27205, {"maria", "aggelos"})        # ...and later added it again
assert s.state.deleted_users("radarr", 27205) is None and not s.state.is_handled("radarr", 27205)
s.settings.update({"dry_run": was_dry})
requests.Session.request = prev3
# --- subtitles through Bazarr, and upcoming episodes (fakes for Radarr/Sonarr/Bazarr)
prev4 = requests.Session.request
prev_get, prev_post = requests.get, requests.post
bz = {"posted": None}
def tv4_fake(self, method, url, params=None, json=None, headers=None, timeout=None, data=None):
    if url.endswith(":7878/api/v3/movie"):
        return Resp(body=[{"id": 31, "tmdbId": 27205, "imdbId": "tt1375666", "title": "Inception"}])
    if url.endswith(":8989/api/v3/series"):
        return Resp(body=[{"id": 41, "tvdbId": 121361, "title": "Game of Thrones"}])
    if url.endswith(":8989/api/v3/episode"):
        assert params["seriesId"] == 41
        return Resp(body=[{"id": 900, "seasonNumber": 1, "episodeNumber": 2}, {"id": 901, "seasonNumber": 1, "episodeNumber": 3}])
    if url.endswith(":8989/api/v3/calendar"):
        assert params["includeSeries"] == "true"
        return Resp(body=[
            {"seasonNumber": 2, "episodeNumber": 5, "title": "Later", "airDateUtc": "2026-10-30T01:00:00Z", "hasFile": False,
             "series": {"title": "Severance", "year": 2022, "tvdbId": 371980, "images": [{"coverType": "poster", "remoteUrl": "https://img/sev.jpg"}]}},
            {"seasonNumber": 1, "episodeNumber": 1, "title": "Sooner", "airDateUtc": "2026-10-27T01:00:00Z", "hasFile": False,
             "series": {"title": "New Show", "year": 2026, "tvdbId": 1}},
            {"seasonNumber": 4, "episodeNumber": 3, "title": "Got it", "airDateUtc": "2026-10-26T01:00:00Z", "hasFile": True,
             "series": {"title": "The Bear", "tvdbId": 2}}])
    return prev4(self, method, url, params=params, json=json, headers=headers, timeout=timeout)
def bz_get(url, params=None, headers=None, timeout=None):
    assert headers["X-API-KEY"] == "bzkey"
    if url.endswith("/api/providers/movies"):
        assert params == {"radarrid": 31}
        return Resp(body={"data": [
            {"provider": "opensubtitles", "language": "el", "hearing_impaired": "False", "forced": "False", "score": 90,
             "release_info": ["Inception.2010.1080p.BluRay"], "subtitle": "PICKLED-1", "original_format": "False", "uploader": "x"},
            {"provider": "podnapisi", "language": "en", "hearing_impaired": "True", "forced": "False", "score": 95,
             "release_info": ["Inception.2010.WEB"], "subtitle": "PICKLED-2", "original_format": "False"},
            {"provider": "subs4free", "language": "el:hi", "hearing_impaired": "True", "forced": "False", "score": 70,
             "release_info": [], "subtitle": "PICKLED-3", "original_format": "False"}]})
    if url.endswith("/api/providers/episodes"):
        assert params == {"episodeid": 901}
        return Resp(body={"data": [{"provider": "addic7ed", "language": "el", "score": 80, "subtitle": "PICKLED-E",
                                    "hearing_impaired": "False", "forced": "False", "original_format": "False"}]})
    return prev_get(url, params=params, headers=headers, timeout=timeout)
def bz_post(url, data=None, headers=None, timeout=None, **kw):
    if "/api/providers/" in url:
        bz["posted"] = (url.rsplit("/", 1)[1], dict(data))
        return Resp(204, body=None, content=b"")
    return prev_post(url, data=data, headers=headers, timeout=timeout, **kw)
requests.Session.request = tv4_fake
requests.get, requests.post = bz_get, bz_post
app.requests.get, app.requests.post = bz_get, bz_post
s.settings.update({"bazarr_url": "http://localhost:6767", "bazarr_api_key": "bzkey"})
r = call("/api/tv/subtitles?kind=movie&tmdb=27205&lang=el")
assert [x["provider"] for x in r["results"]] == ["opensubtitles", "subs4free"], r     # Greek only, best first
assert all("subtitle" not in x and "_post" not in x for x in r["results"]), r          # Bazarr's pickled ids stay here
assert r["results"][1]["hi"] is True and r["results"][0]["release"] == "Inception.2010.1080p.BluRay"
ok = call("/api/tv/subtitles/download", {"token": r["token"], "index": 1})
assert ok["ok"] and bz["posted"] == ("movies", {"hi": "True", "forced": "False", "original_format": "False",
                                                "provider": "subs4free", "subtitle": "PICKLED-3", "radarrid": "31"}), bz
r = call("/api/tv/subtitles?kind=episode&tvdb=121361&season=1&episode=3")
call("/api/tv/subtitles/download", {"token": r["token"], "index": 0})
assert bz["posted"][0] == "episodes" and bz["posted"][1]["seriesid"] == "41" and bz["posted"][1]["episodeid"] == "901"
for bad in ({"token": "nope", "index": 0}, {"token": r["token"], "index": 5}):
    try:
        call("/api/tv/subtitles/download", bad); raise AssertionError("expected 409")
    except urllib.error.HTTPError as e:
        assert e.code == 409
try:
    call("/api/tv/subtitles?kind=movie&tmdb=1"); raise AssertionError("expected 409")
except urllib.error.HTTPError as e:
    assert e.code == 409 and b"isn't in Radarr" in e.read()
up = call("/api/tv/upcoming?days=7")["items"]
assert [(x["show"], x["season"], x["episode"]) for x in up] == [("New Show", 1, 1), ("Severance", 2, 5)], up   # soonest first, no downloaded ones
assert up[1]["poster"] == "https://img/sev.jpg"
requests.Session.request = prev4
requests.get, requests.post = prev_get, prev_post
app.requests.get, app.requests.post = prev_get, prev_post
print("tv tests OK")

# --- anime: own folder, own profile, series type Anime
assert app.is_anime({"genres": ["Anime", "Animation"]})
assert app.is_anime({"genres": ["Animation"], "originalLanguage": {"name": "Japanese"}})
assert not app.is_anime({"genres": ["Animation"], "originalLanguage": {"name": "English"}})
assert not app.is_anime({"genres": ["Drama"]}) and not app.is_anime({})
s.settings.update({"dry_run": False})
owner_watchlist.append({"ratingKey": "104", "title": "Frieren", "type": "show"})
calls.clear()
s.run_once()     # anime folder not set yet: added like any show
body = next(c[3] for c in calls if c[0] == "POST" and c[1].endswith("/series"))
assert body["tvdbId"] == 424536 and body["seriesType"] == "standard" and body["rootFolderPath"] == "/tv/", body
sonarr_series[:] = [x for x in sonarr_series if x["tvdbId"] != 424536]
s.state.db.execute("DELETE FROM handled WHERE ext_id IN (424536, 371028)")
call("/api/settings", {"sonarr_anime_root_folder": "/anime/", "sonarr_anime_quality_profile": "Anime"})
owner_watchlist.append({"ratingKey": "105", "title": "Arcane", "type": "show"})
calls.clear()
r5 = s.run_once()
posted = {c[3]["tvdbId"]: c[3] for c in calls if c[0] == "POST" and c[1].endswith("/series")}
fr, ar = posted[424536], posted[371028]
assert fr["seriesType"] == "anime" and fr["rootFolderPath"] == "/anime/" and fr["qualityProfileId"] == 7, fr
assert ar["seriesType"] == "standard" and ar["rootFolderPath"] == "/tv/" and ar["qualityProfileId"] == 4, ar   # western animation
assert not r5["errors"], r5
s.settings.update({"sonarr_anime_quality_profile": ""})     # no anime profile: anime keeps the normal one
assert call("/api/settings")["sonarr_anime_root_folder"] == "/anime/"
print("anime tests OK")

# --- TV lookups and requests (for Jellyfin, which has no Watchlist)
prev_fake = requests.Session.request
def look_fake(self, method, url, params=None, json=None, headers=None, timeout=None):
    if url.endswith("/movie/lookup") and method == "GET":
        calls.append((method, url, params, json))
        return Resp(body=[
            {"title": "Dune", "year": 2021, "tmdbId": 438631, "imdbId": "tt1160419", "overview": "Paul.", "genres": ["Sci-Fi"],
             "images": [{"coverType": "poster", "remoteUrl": "https://img/dune.jpg"}], "ratings": {"tmdb": {"value": 7.8}}},
            {"title": "Inception", "year": 2010, "tmdbId": 27205, "images": []},
            {"title": "No id", "tmdbId": 0}])
    if url.endswith("/series/lookup") and not params["term"].startswith("tvdb:"):
        calls.append((method, url, params, json))
        return Resp(body=[{"title": "Frieren", "year": 2023, "tvdbId": 424536, "genres": ["Anime"], "network": "NTV"}])
    return prev_fake(self, method, url, params=params, json=json, headers=headers, timeout=timeout)
requests.Session.request = look_fake
radarr_movies[:] = [x for x in radarr_movies if x.get("tmdbId") != 27205] + [{"id": 299, "tmdbId": 27205, "tags": [], "hasFile": True}]
s._library_cache = {}
r = call("/api/tv/lookup?kind=movie&q=" + "dune")
assert [x["title"] for x in r["items"]] == ["Dune", "Inception"], r
dune, inc = r["items"]
assert dune["status"] == "none" and dune["poster"] == "https://img/dune.jpg" and dune["rating"] == 7.8 and dune["imdb"] == "tt1160419"
assert inc["status"] == "have"
r = call("/api/tv/lookup?kind=show&q=frieren")
assert r["items"][0]["tvdb"] == 424536 and r["items"][0]["kind"] == "show"
try:
    call("/api/tv/lookup?kind=book&q=x"); raise AssertionError("expected 400")
except urllib.error.HTTPError as e:
    assert e.code == 400
calls.clear()
res = call("/api/tv/request", {"kind": "movie", "tmdb": 438631, "user": "Aggelos <x>"})
assert res["status"] == "added", res
posted = next(c[3] for c in calls if c[0] == "POST" and c[1].endswith("/movie"))
assert posted["tmdbId"] == 438631 and posted["rootFolderPath"] == "/movies" and posted["addOptions"]["monitor"] == "movieOnly"
assert s.state.is_handled("radarr", 438631)
assert call("/api/tv/lookup?kind=movie&id=438631")["items"][0]["status"] == "requested"
assert call("/api/tv/request", {"kind": "movie", "tmdb": 438631})["status"] == "already-there"
# a show requested from the TV gets the anime folder like a watchlist add would
sonarr_series[:] = [x for x in sonarr_series if x["tvdbId"] != 424536]
calls.clear()
assert call("/api/tv/request", {"kind": "show", "tvdb": 424536, "user": "aggelos"})["status"] == "added"
body = next(c[3] for c in calls if c[0] == "POST" and c[1].endswith("/series"))
assert body["seriesType"] == "anime" and body["rootFolderPath"] == "/anime/", body
try:
    call("/api/tv/request", {"kind": "show", "tvdb": "x"}); raise AssertionError("expected 400")
except urllib.error.HTTPError as e:
    assert e.code == 400
requests.Session.request = prev_fake
print("tv lookup/request tests OK")

call("/api/plex/logout", {})
assert call("/api/status")["account"] is None and not s.settings.all()["plex_token"]
print("recent:", [(x["title"], x["users"], x["result"]) for x in s.state.recent()])
print("ALL OK")
