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
        if params and "tvdbId" in params:
            want = params["tvdbId"]
            return Resp(body=[x for x in sonarr_series if str(x.get("tvdbId")) == str(want)])
        return Resp(body=sonarr_series)
    if url.endswith("/series/lookup"):
        term = (params or {}).get("term", "")
        if ":" in term:
            tid = int(term.split(":")[1])
            return Resp(body=[{"title": f"show{tid}", "tvdbId": tid, "seasons": [], **LOOKUP_EXTRA.get(tid, {})}])
        return Resp(body=[])
    if url.endswith("/series") and method == "POST":
        sonarr_series.append({"id": len(sonarr_series) + 100, "tvdbId": json["tvdbId"], "tags": json["tags"], "title": json["title"]})
        return Resp(201, json)
    if url.endswith("/movie") and method == "GET":
        if params and "tmdbId" in params:
            want = params["tmdbId"]
            return Resp(body=[x for x in radarr_movies if str(x.get("tmdbId")) == str(want)])
        return Resp(body=radarr_movies)
    if url.endswith("/movie/lookup"):
        return Resp(body=[])
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
    if url.endswith(":7878/api/v3/movie"):
        movies = [{"id": 31, "tmdbId": 27205, "imdbId": "tt1375666", "title": "Inception"}]
        if params and "tmdbId" in params:
            want = params["tmdbId"]
            return Resp(body=[x for x in movies if str(x.get("tmdbId")) == str(want)])
        return Resp(body=movies)
    if url.endswith(":8989/api/v3/series"):
        series = [{"id": 41, "tvdbId": 121361, "imdbId": "tt0944947", "title": "Game of Thrones"}]
        if params and "tvdbId" in params:
            want = params["tvdbId"]
            return Resp(body=[x for x in series if str(x.get("tvdbId")) == str(want)])
        return Resp(body=series)
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
        movies = [{"id": 31, "tmdbId": 27205, "imdbId": "tt1375666", "title": "Inception"}]
        if params and "tmdbId" in params:
            want = params["tmdbId"]
            return Resp(body=[x for x in movies if str(x.get("tmdbId")) == str(want)])
        return Resp(body=movies)
    if url.endswith(":8989/api/v3/series"):
        series = [{"id": 41, "tvdbId": 121361, "title": "Game of Thrones"}]
        if params and "tvdbId" in params:
            want = params["tvdbId"]
            return Resp(body=[x for x in series if str(x.get("tvdbId")) == str(want)])
        return Resp(body=series)
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

# --- Discover rows: Radarr's discover (no key) + TMDB trending shows (with a key)
prev_fake = requests.Session.request
tmdb_calls = []
def disc_fake(self, method, url, params=None, json=None, headers=None, timeout=None):
    if url.endswith("/importlist/movie"):
        assert params == {"includeRecommendations": "true", "includeTrending": "true", "includePopular": "true"}, params
        return Resp(body=[
            {"title": "Sinners", "year": 2025, "tmdbId": 1233413, "isTrending": True, "isPopular": True, "remotePoster": "https://img/sinners.jpg", "ratings": {"tmdb": {"value": 7.6}}},
            {"title": "Inception", "year": 2010, "tmdbId": 27205, "isExisting": True, "isTrending": True},
            {"title": "Excluded", "year": 2020, "tmdbId": 5, "isExcluded": True, "isPopular": True},
            {"title": "Arrival", "year": 2016, "tmdbId": 329865, "isRecommendation": True, "ratings": {"tmdb": {"value": 7.6}}},
            {"title": "Interstellar", "year": 2014, "tmdbId": 157336, "isRecommendation": True, "ratings": {"tmdb": {"value": 8.4}}},
            {"title": "From a list", "year": 2019, "tmdbId": 9, "lists": [1]}])
    if "api.themoviedb.org" in url:
        tmdb_calls.append((url, dict(params or {}), dict(headers or {})))
        if url.endswith("/genre/tv/list"):
            return Resp(body={"genres": [{"id": 18, "name": "Drama"}, {"id": 9648, "name": "Mystery"}]})
        if url.endswith("/trending/tv/week"):
            return Resp(body={"results": [
                {"id": 100088, "name": "The Last of Us", "first_air_date": "2023-01-15", "poster_path": "/p.jpg", "backdrop_path": "/b.jpg", "vote_average": 8.55, "genre_ids": [18, 9648]},
                {"id": 1396, "name": "Breaking Bad", "first_air_date": "2008-01-20"},
                {"id": 1, "name": "No tvdb"}]})
        m = re.search(r"/tv/(\d+)/external_ids$", url)
        if m:
            return Resp(body={"tvdb_id": {"100088": 392256, "1396": 81189}.get(m.group(1))})
    return prev_fake(self, method, url, params=params, json=json, headers=headers, timeout=timeout)
requests.Session.request = disc_fake
requests.get = lambda url, **kw: disc_fake(None, "GET", url, **kw)
s._library_cache = {}
s.__dict__.pop("_discover_cache", None)
d1 = call("/api/tv/discover")
secs = {x["id"]: x for x in d1["sections"]}
assert [m["title"] for m in secs["trending"]["items"]] == ["Sinners"], secs     # no TMDB key: movies only
assert secs["trending"]["items"][0]["poster"] == "https://img/sinners.jpg" and secs["trending"]["items"][0]["status"] == "none"
assert [m["title"] for m in secs["recommended"]["items"]] == ["Interstellar", "Arrival"]              # best rated first
assert "popular" not in secs                                                                         # Sinners is already in trending
assert not tmdb_calls
call("/api/settings", {"tmdb_api_key": "v3key"})
assert call("/api/settings")["tmdb_api_key_set"] and "v3key" not in json.dumps(call("/api/settings"))
s.__dict__.pop("_discover_cache", None)
d2 = call("/api/tv/discover")
tr = [x for x in d2["sections"] if x["id"] == "trending"][0]["items"]
assert [(m["kind"], m["title"]) for m in tr] == [("movie", "Sinners"), ("show", "The Last of Us")], tr   # Breaking Bad is in Sonarr
lou = tr[1]
assert lou["tvdb"] == 392256 and lou["year"] == 2023 and lou["poster"].endswith("/w342/p.jpg") and lou["rating"] == 8.6
assert lou["genres"] == ["Drama", "Mystery"]
assert tmdb_calls[0][1].get("api_key") == "v3key"
assert call("/api/tv/discover") == d2          # cached
s.settings.update({"tmdb_api_key": "eyJabc"})
s.__dict__.pop("_discover_cache", None); tmdb_calls.clear()
call("/api/tv/discover")
assert tmdb_calls[0][2].get("Authorization") == "Bearer eyJabc" and "api_key" not in tmdb_calls[0][1]
requests.Session.request = prev_fake
requests.get = lambda url, **kw: fake(None, "GET", url, **kw)
print("discover tests OK")

# The ranking route is optional and uses the same credentials as the TV API.
with ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(s, "pw")) as ranking_server:
    threading.Thread(target=ranking_server.serve_forever, daemon=True).start()
    rank_url = f"http://127.0.0.1:{ranking_server.server_port}/api/tv/recommend"
    rank_body = {
        "taste": {"genres": ["Mystery"], "token": "private"},
        "seed": {"title": "Severance", "kind": "show", "genres": ["Drama"]},
        "candidates": [
            {"id": "dark", "kind": "show", "title": "Dark", "year": 2017, "genres": ["Mystery"]},
            {"id": "arrival", "kind": "movie", "title": "Arrival", "year": 2016, "genres": ["Drama"]},
        ],
    }

    def post_ranking(body=rank_body, headers=auth, url=rank_url):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    from io import BytesIO
    from unittest.mock import patch

    with patch.dict(os.environ, {"RECOMMEND_URL": "", "RECOMMEND_MODEL": ""}):
        assert post_ranking()[0] == 503, "ranking is disabled without provider config"

    def provider_reply(content):
        res = requests.Response()
        res.status_code = 200
        res.raw = BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())
        res._content_consumed = False
        return res

    with patch.dict(os.environ, {
        "RECOMMEND_URL": "http://model.local/v1/chat/completions",
        "RECOMMEND_MODEL": "test-model",
        "RECOMMEND_API_KEY": "model-secret",
    }):
        with patch.object(requests, "post", return_value=provider_reply(json.dumps({"picks": [
            {"id": "dark", "reason": "A slow-burn mystery."},
            {"id": "invented", "reason": "Not a candidate"},
            {"id": "dark", "reason": "Duplicate"},
            {"id": "arrival", "reason": "a" * 160},
        ]}))) as provider:
            assert post_ranking(headers={})[0] == 401
            assert post_ranking(url=base + "/api/tv/recommend")[0] == 503
            assert post_ranking({**rank_body, "candidates": rank_body["candidates"] * 13})[0] == 400
            assert post_ranking({**rank_body, "ignored": "x" * 25000})[0] == 413
            assert not provider.called
            assert post_ranking() == (200, {"picks": [
                {"id": "dark", "reason": "A slow-burn mystery."},
                {"id": "arrival", "reason": "a" * 120},
            ]})
            provider_payload = provider.call_args.kwargs["json"]
            assert "private" not in json.dumps(provider_payload)
            assert provider.call_args.kwargs["headers"]["Authorization"] == "Bearer model-secret"
        with patch.object(requests, "post", return_value=provider_reply("bad json")):
            assert post_ranking()[0] == 502
        with patch.object(requests, "post", side_effect=requests.Timeout("provider detail")):
            code, err = post_ranking()
            assert code == 502 and "provider detail" not in err["error"]
    ranking_server.shutdown()
print("recommendation tests OK")

# --- Acquisition writes and restart recovery ---
def acquisition_tests(directory):
    syncer = app.Syncer(directory)
    state = {"space": 100 * 1024**3, "queued": False, "timeout": False, "commands": [], "writes": [], "disable_at_disk_check": False}

    def acquisition_fake(self, method, url, params=None, json=None, headers=None, timeout=None):
        if url.endswith("/rootfolder") and ":7878" in url:
            if state["disable_at_disk_check"]:
                syncer.settings.update({"auto_fetch": False})
            return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": state["space"]}])
        if url.endswith("/queue"):
            records = [{"movieId": 201, "movie": {"tmdbId": 7001}}] if state["queued"] else []
            return Resp(body={"records": records, "totalRecords": len(records)})
        if url.endswith("/command"):
            if method == "GET":
                return Resp(body=state.get("active_commands", []))
            state["commands"].append(json)
            return Resp(201, {"id": 55, "status": "queued"})
        if url.endswith("/movie") and method == "POST":
            state["writes"].append(json["tmdbId"])
            if state["timeout"]:
                radarr_movies.append({"id": 900, "tmdbId": json["tmdbId"], "title": "Recovered"})
                raise requests.Timeout("reply lost after the write")
        return fake(self, method, url, params=params, json=json, headers=headers, timeout=timeout)

    syncer.settings.update({"tag_by_user": True})
    radarr_movies.append({"id": 201, "tmdbId": 7001, "title": "Missing", "monitored": True})
    sonarr_series.append({"id": 301, "tvdbId": 9100, "title": "Show", "monitored": True})
    with patch.object(requests.Session, "request", acquisition_fake):
        assert syncer.tv_auto_fetch(7002, "disabled")["status"] == "disabled"
        syncer.settings.update({"auto_fetch": True, "auto_fetch_free_gib": 50, "dry_run": True})
        assert syncer.tv_auto_fetch(7002, "dry")["status"] == "dry-run"
        assert syncer.tv_search_again("movie", 7001)["status"] == "dry-run"
        before_tags = dict(tags)
        assert syncer.tv_request("movie", 7002, "should-not-create-a-tag")["status"] == "dry-run"
        assert tags == before_tags
        syncer.settings.update({"dry_run": False})
        assert syncer.tv_search_again("movie", 9999)["status"] == "not-tracked"
        state["queued"] = True
        assert syncer.tv_search_again("movie", 7001)["status"] == "active"
        state["queued"] = False
        assert syncer.tv_search_again("movie", 7001)["status"] == "searching"
        assert syncer.tv_search_again("movie", 7001)["status"] == "active"
        assert syncer.tv_search_again("show", 9100)["status"] == "searching"
        assert state["commands"] == [{"name": "MoviesSearch", "movieIds": [201]},
                                     {"name": "MissingEpisodeSearch", "seriesId": 301, "monitored": True}]
        assert not state["writes"]

        assert syncer.tv_auto_fetch(7001, "existing")["status"] == "already-there"
        state["space"] = None
        assert syncer.tv_auto_fetch(7002, "unknown-space")["status"] == "space-unavailable"
        state["space"] = 20 * 1024**3
        assert syncer.tv_auto_fetch(7002, "low-space")["status"] == "low-space"
        state["space"] = 100 * 1024**3
        state["disable_at_disk_check"] = True
        assert syncer.tv_auto_fetch(7002, "disabled-during-check")["status"] == "unavailable"
        assert not state["writes"]
        state["disable_at_disk_check"] = False
        syncer.settings.update({"auto_fetch": True})
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda tid: syncer.tv_auto_fetch(*tid)["status"],
                                    [(7002, "winner"), (7003, "second")]))
        assert sorted(results) == ["added", "quota-reached"], results
        winning_id = state["writes"][0]
        trigger = "winner" if winning_id == 7002 else "second"
        assert app.Syncer(directory).tv_auto_fetch(winning_id, trigger)["status"] == "added"
        assert len(state["writes"]) == 1
        assert all(row["tmdb"] != winning_id for row in syncer.tv_queue()["items"]), "An empty search should leave the queue"

        with tempfile.TemporaryDirectory() as retry_dir:
            restart = app.Syncer(retry_dir)
            restart.settings.update({"auto_fetch": True})
            state["timeout"] = True
            assert restart.tv_auto_fetch(7004, "lost-reply")["status"] == "pending"
            recovered = app.Syncer(retry_dir)
            assert recovered.tv_auto_status(7005, "lost-reply")["status"] == "trigger-conflict"
            assert recovered.tv_auto_status(7004, "lost-reply")["status"] == "added"
            assert recovered.tv_auto_fetch(7004, "lost-reply")["status"] == "added"
            assert recovered.tv_auto_fetch(7005, "another")["status"] == "quota-reached"
            assert state["writes"].count(7004) == 1
            restart.state.db.close()
            recovered.state.db.close()
    syncer.state.db.close()

acquisition_tests(tempfile.mkdtemp())
print("acquisition tests OK")

pages = []
def paging_fake(self, method, url, params=None, **kw):
    page = params["page"]
    pages.append(page)
    if page == 1:
        return Resp(body={"records": [{"movieId": i, "status": "queued"} for i in range(1, 201)], "totalRecords": 201})
    return Resp(body={"records": [{"movieId": 201, "status": "failed", "movie": {"tmdbId": 77}}], "totalRecords": 201})

with patch.object(requests.Session, "request", paging_fake):
    arr = app.Arr("Radarr", app.Config(s.settings.all()).radarr)
    assert next(x for x in s._queue_of(arr, "movie") if x["tmdb"] == 77)["status"] == "failed"
    assert pages == [1, 2]

for route in ("/api/tv/search-again", "/api/tv/auto-fetch"):
    assert raw(route, "POST")[0] == 403

call("/api/plex/logout", {})
assert call("/api/status")["account"] is None and not s.settings.all()["plex_token"]

# ===========================================================================
# Hardening Regression Tests
# ===========================================================================
from unittest.mock import patch
import base64 as b64
import time

# 1. Path traversal / Auth bypass & Security Headers
with ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(s, "sec-pw")) as srv_auth:
    threading.Thread(target=srv_auth.serve_forever, daemon=True).start()
    b_auth = f"http://127.0.0.1:{srv_auth.server_port}"

    # Path traversal attempting to bypass auth via /health/..
    c, _ = raw("/health/../api/settings", server=b_auth)
    assert c == 401, f"Expected 401 on /health/../api/settings, got {c}"

    # Genuine /health endpoint remains unauthenticated
    c, _ = raw("/health", server=b_auth)
    assert c == 200, f"Expected 200 on /health, got {c}"

    # Authenticated access succeeds
    auth_hdr = {"Authorization": "Basic " + b64.b64encode(b"admin:sec-pw").decode()}
    c, _ = raw("/api/settings", headers=auth_hdr, server=b_auth)
    assert c == 200, f"Expected 200 with auth, got {c}"

    # Security headers on HTML and JSON responses
    c, h_html = raw("/", headers=auth_hdr, server=b_auth)
    assert c == 200, f"Expected 200 on /, got {c}"
    assert h_html.get("X-Content-Type-Options") == "nosniff"
    assert h_html.get("X-Frame-Options") == "SAMEORIGIN"
    assert "Content-Security-Policy" in h_html

    c, h_json = raw("/api/settings", headers=auth_hdr, server=b_auth)
    assert h_json.get("X-Content-Type-Options") == "nosniff"
    assert h_json.get("X-Frame-Options") == "SAMEORIGIN"
    srv_auth.shutdown()

# 2. Secret Redaction
test_url = "https://api.themoviedb.org/3/trending?api_key=secret_tmdb_key_12345&language=en"
redacted = app.redact_secrets(test_url)
assert "secret_tmdb_key_12345" not in redacted
assert "api_key=[REDACTED]" in redacted

bearer_text = "Failed with Authorization: Bearer my_super_secret_token_abc123"
redacted_bearer = app.redact_secrets(bearer_text)
assert "my_super_secret_token_abc123" not in redacted_bearer
assert "Bearer [REDACTED]" in redacted_bearer

# Known secret redaction
custom_redacted = app.redact_secrets("Error connecting to radarr with key k_custom_999", secrets=["k_custom_999"])
assert "k_custom_999" not in custom_redacted
assert "[REDACTED]" in custom_redacted

# 3. Payload size limit (MAX_BODY)
def send_custom_post(server_url, path, data, headers=None):
    req = urllib.request.Request(server_url + path, method="POST", data=data,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""

# Payload > 1MB returns 413
huge_payload = json.dumps({"test": "x" * (1_048_576 + 10)}).encode()
c_huge, b_huge = send_custom_post(base, "/api/settings", huge_payload)
assert c_huge == 413, f"Expected 413 for oversized body, got {c_huge}"

# Bad JSON returns 400
c_bad_json, _ = send_custom_post(base, "/api/settings", b"not-json{{{")
assert c_bad_json == 400, f"Expected 400 for bad JSON, got {c_bad_json}"

# 4. Settings invalid types and resilience
call("/api/settings", {"interval_min": "invalid-int-value"})
assert s.settings.all()["interval_min"] >= 1

# Corrupted settings file recovery
with tempfile.TemporaryDirectory() as td:
    corrupt_file = os.path.join(td, "settings.json")
    with open(corrupt_file, "w") as f:
        f.write("{corrupt json!")
    sett = app.Settings(corrupt_file)
    assert sett.all() == {} or isinstance(sett.all(), dict)

# 5. SQLite reliability and recovery
with tempfile.TemporaryDirectory() as td:
    state_inst = app.State(os.path.join(td, "state.db"))
    mode = state_inst.db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() in ("wal", "memory"), f"Expected WAL mode, got {mode}"

    state_inst.db.execute("INSERT OR REPLACE INTO guid_cache (rating_key, guids) VALUES ('test', '{bad-json')")
    assert state_inst.cached_guids("test") is None
    state_inst.db.close()

# 6. Auto-fetch string tmdb in POST & stale pending recovery
with tempfile.TemporaryDirectory() as td:
    af_syncer = app.Syncer(td)
    af_syncer.settings.update({"auto_fetch": True, "auto_fetch_free_gib": 50})
    with ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(af_syncer, "pw")) as srv_af:
        threading.Thread(target=srv_af.serve_forever, daemon=True).start()
        b_af = f"http://127.0.0.1:{srv_af.server_port}"
        auth_af = {"Authorization": "Basic " + b64.b64encode(b"admin:pw").decode()}

        with patch.object(af_syncer, "tv_auto_fetch", return_value={"status": "added"}):
            c_af, b_resp = send_custom_post(b_af, "/api/tv/auto-fetch",
                                            json.dumps({"tmdb": "12345", "triggerId": "trig_1"}).encode(),
                                            headers=auth_af)
            assert c_af == 200, f"Expected 200 for string tmdb, got {c_af}: {b_resp}"

        c_bad_trig, _ = send_custom_post(b_af, "/api/tv/auto-fetch",
                                         json.dumps({"tmdb": 12345, "triggerId": "bad/trigger/!@"}).encode(),
                                         headers=auth_af)
        assert c_bad_trig == 400
        srv_af.shutdown()

    now_ts = int(time.time())
    af_syncer.state.db.execute("INSERT INTO auto_fetch VALUES (?, ?, ?, 'pending')",
                              ("stale-trig", 8888, now_ts - 200))
    assert af_syncer.state.reserve_auto("new-trig", 9999) == "reserved"
    af_syncer.state.db.close()

# 7. Endpoint Bounds and Input Validation
for bad_path in [
    "/api/tv/collection?tmdb=-1",
    "/api/tv/collection?tmdb=abc",
    "/api/tv/upcoming?days=-1",
    "/api/tv/upcoming?days=abc",
    "/api/tv/lookup?kind=movie&id=-5",
    "/api/tv/lookup?kind=invalid_kind&id=1",
    "/api/tv/subtitles?kind=invalid&tmdb=1",
    "/api/tv/subtitles?kind=movie&tmdb=-1",
]:
    c_bad, _ = raw(bad_path)
    assert c_bad == 400, f"Expected 400 for {bad_path}, got {c_bad}"

# 8. Thread-Safety / Concurrency Locks
assert isinstance(s.tv_write_lock, type(threading.RLock()))
with s.tv_write_lock:
    acquired = s.tv_write_lock.acquire(timeout=1.0)
    assert acquired
    s.tv_write_lock.release()

with s.action_lock:
    s.action = {"state": "done", "user": "test", "deleted": ["A", "B"], "untagged": [], "errors": [], "total": 2}
act_copy = s.get_action()
assert act_copy == s.action
assert act_copy["deleted"] is not s.action["deleted"]

# 9. Upstream malformed response handling
def bz_test_get(url, params=None, headers=None, timeout=None):
    if "/api/providers/movies" in url:
        return Resp(body={"data": [{"language": "en", "score": "not_a_num", "provider": "p1"}]})
    return Resp(body=[])

with patch.object(app.requests, "get", side_effect=bz_test_get), \
     patch.object(requests.Session, "request", return_value=Resp(body=[{"id": 31, "tmdbId": 27205}])):
    res = s.tv_subtitle_search("movie", tmdb=27205)
    assert len(res["results"]) == 1
    assert res["results"][0]["score"] == 0.0

# 10. UI XSS Escaping Verification
with open(os.path.join(app.HERE, "ui.html"), "r", encoding="utf-8") as f:
    ui_content = f.read()
assert "${esc(u.role)}" in ui_content
assert "${esc(u.status)}" in ui_content
assert "${esc(u.items ?? \"—\")}" in ui_content
assert "${esc(placeholder)}" in ui_content

# 11. Comprehensive Error-Path Tests for auto-fetch, request tracking, tv_request and tv_search_again
def error_path_tests():
    with tempfile.TemporaryDirectory() as td:
        err_syncer = app.Syncer(td)
        try:
            err_syncer.settings.update({
                "auto_fetch": True,
                "auto_fetch_free_gib": 50,
                "dry_run": False,
                "radarr_url": "http://radarr:7878",
                "radarr_api_key": "k_rad",
                "radarr_search": True,
                "sonarr_url": "http://sonarr:8989",
                "sonarr_api_key": "k_son",
                "sonarr_search": True,
            })

            with ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(err_syncer, "testpw")) as err_srv:
                threading.Thread(target=err_srv.serve_forever, daemon=True).start()
                err_base = f"http://127.0.0.1:{err_srv.server_port}"
                err_auth = {"Authorization": "Basic " + b64.b64encode(b"admin:testpw").decode()}

                try:
                    # 1. tv_request and tv_search_again with Sonarr/Radarr returning errors
                    # 1a. Services disabled -> 409
                    err_syncer.settings.update({
                        "radarr_url": "",
                        "radarr_api_key": "",
                        "sonarr_url": "",
                        "sonarr_api_key": "",
                    })
                    c, b = send_custom_post(err_base, "/api/tv/request", json.dumps({"kind": "movie", "tmdb": 1001}).encode(), headers=err_auth)
                    assert c == 409 and b"isn't set up" in b, f"Got {c}: {b}"
                    c, b = send_custom_post(err_base, "/api/tv/request", json.dumps({"kind": "show", "tvdb": 2001}).encode(), headers=err_auth)
                    assert c == 409 and b"isn't set up" in b, f"Got {c}: {b}"
                    c, b = send_custom_post(err_base, "/api/tv/search-again", json.dumps({"kind": "movie", "tmdb": 1001}).encode(), headers=err_auth)
                    assert c == 409 and b"isn't set up" in b, f"Got {c}: {b}"

                    err_syncer.settings.update({
                        "radarr_url": "http://radarr:7878",
                        "radarr_api_key": "k_rad",
                        "sonarr_url": "http://sonarr:8989",
                        "sonarr_api_key": "k_son",
                    })

                    # 1b. Item lookup not found -> 409
                    def mock_not_found(self, method, url, **kw):
                        if "/movie/lookup/tmdb" in url or "/series/lookup" in url:
                            return Resp(body=[])
                        if url.endswith("/movie") or url.endswith("/series"):
                            return Resp(body=[])
                        return Resp(200, {})

                    with patch.object(requests.Session, "request", mock_not_found):
                        c, b = send_custom_post(err_base, "/api/tv/request", json.dumps({"kind": "movie", "tmdb": 99999}).encode(), headers=err_auth)
                        assert c == 409 and b"couldn't find that movie" in b, f"Got {c}: {b}"
                        c, b = send_custom_post(err_base, "/api/tv/request", json.dumps({"kind": "show", "tvdb": 99999}).encode(), headers=err_auth)
                        assert c == 409 and b"couldn't find that show" in b, f"Got {c}: {b}"

                    # 1c. Upstream errors (500 / timeout) during tv_request -> 502 with redacted error
                    def mock_500(self, method, url, **kw):
                        if url.endswith("/movie") or url.endswith("/series"):
                            if method == "GET":
                                return Resp(body=[])
                            if method == "POST":
                                return Resp(500, content=b"Internal error with secret key k_rad")
                        if "/movie/lookup/tmdb" in url:
                            return Resp(body=[{"title": "Test Movie", "tmdbId": 1001}])
                        if url.endswith("/qualityprofile"):
                            return Resp(body=[{"id": 1, "name": "HD"}])
                        if url.endswith("/rootfolder"):
                            return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 100 * 1024**3}])
                        return Resp(500, content=b"Server Error")

                    with patch.object(requests.Session, "request", mock_500):
                        c, b = send_custom_post(err_base, "/api/tv/request", json.dumps({"kind": "movie", "tmdb": 1001}).encode(), headers=err_auth)
                        assert c == 502, f"Got {c}: {b}"
                        assert b"k_rad" not in b, "Secret leaked in error response"
                        assert b"Radarr:" in b

                    # 1d. tv_search_again error handling and bugfix check
                    def mock_search_err(self, method, url, **kw):
                        if url.endswith("/movie"):
                            return Resp(body=[{"id": 50, "tmdbId": 1001, "monitored": True, "hasFile": False}])
                        if url.endswith("/queue"):
                            return Resp(body={"records": [], "totalRecords": 0})
                        if url.endswith("/command"):
                            if method == "GET":
                                return Resp(body=[])
                            if method == "POST":
                                return Resp(500, content=b"Command failure with secret k_rad")
                        return Resp(200, {})

                    with patch.object(requests.Session, "request", mock_search_err):
                        c, b = send_custom_post(err_base, "/api/tv/search-again", json.dumps({"kind": "movie", "tmdb": 1001}).encode(), headers=err_auth)
                        assert c == 502, f"Got {c}: {b}"
                        assert b"k_rad" not in b, "Secret leaked in error response"
                        # Key bug check: ('movie', 1001) must NOT be cached in tv_searches!
                        assert ("movie", 1001) not in err_syncer.tv_searches, "Failed search command was erroneously cached in tv_searches"
                finally:
                    err_srv.shutdown()

            # 2. Auto-fetch failure cases
            # 2a. Radarr timeout during add -> stays pending, then reconciles to added
            radarr_db = []
            def mock_af_timeout_then_added(self, method, url, json=None, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 100 * 1024**3}])
                if url.endswith("/qualityprofile"):
                    return Resp(body=[{"id": 1, "name": "HD"}])
                if url.endswith("/tag"):
                    return Resp(body={"id": 1, "label": "plex-orama-auto-fetch"} if method == "POST" else [{"id": 1, "label": "plex-orama-auto-fetch"}])
                if "/movie/lookup/tmdb" in url:
                    return Resp(body=[{"title": "Pending Movie", "tmdbId": 8001}])
                if url.endswith("/queue"):
                    return Resp(body={"records": [], "totalRecords": 0})
                if url.endswith("/movie"):
                    if method == "GET":
                        p = kw.get("params")
                        if p and "tmdbId" in p:
                            return Resp(body=[x for x in radarr_db if str(x.get("tmdbId")) == str(p["tmdbId"])])
                        return Resp(body=radarr_db)
                    if method == "POST":
                        radarr_db.append({"id": 801, "tmdbId": 8001, "title": "Pending Movie"})
                        raise requests.Timeout("Gateway Timeout")
                return Resp(200, {})

            with patch.object(requests.Session, "request", mock_af_timeout_then_added):
                res = err_syncer.tv_auto_fetch(8001, "trig_timeout_add")
                assert res["status"] == "pending", f"Expected pending, got {res}"
                attempts = err_syncer.state.auto_attempts("trig_timeout_add")
                assert len(attempts) == 1 and attempts[0][3] == "pending"

                st = err_syncer.tv_auto_status(8001, "trig_timeout_add")
                assert st["status"] == "added", f"Expected added, got {st}"
                assert err_syncer.state.auto_attempts("trig_timeout_add")[0][3] == "added"
                assert err_syncer.state.is_handled("radarr", 8001)

            # 2b. Radarr 5xx during add -> stays pending, then reconciles to unconfirmed after 120s
            err_syncer.state.db.execute("UPDATE auto_fetch SET ts = ? WHERE trigger = 'trig_timeout_add'",
                                       (int(time.time()) - (8 * 86400),))
            def mock_af_500_then_unconfirmed(self, method, url, json=None, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 100 * 1024**3}])
                if url.endswith("/qualityprofile"):
                    return Resp(body=[{"id": 1, "name": "HD"}])
                if url.endswith("/tag"):
                    return Resp(body={"id": 1, "label": "plex-orama-auto-fetch"} if method == "POST" else [{"id": 1, "label": "plex-orama-auto-fetch"}])
                if "/movie/lookup/tmdb" in url:
                    return Resp(body=[{"title": "Failing 500 Movie", "tmdbId": 8002}])
                if url.endswith("/queue"):
                    return Resp(body={"records": [], "totalRecords": 0})
                if url.endswith("/movie"):
                    if method == "GET":
                        return Resp(body=[])
                    if method == "POST":
                        return Resp(500, content=b"Server Error")
                return Resp(200, {})

            with patch.object(requests.Session, "request", mock_af_500_then_unconfirmed):
                res = err_syncer.tv_auto_fetch(8002, "trig_500_add")
                assert res["status"] == "pending", f"Expected pending, got {res}"
                # While < 120s, reconcile stays pending
                st_early = err_syncer.tv_auto_status(8002, "trig_500_add")
                assert st_early["status"] == "pending"

                # Age attempt past 120s threshold
                err_syncer.state.db.execute("UPDATE auto_fetch SET ts = ? WHERE trigger = 'trig_500_add'",
                                           (int(time.time()) - 130,))
                st_late = err_syncer.tv_auto_status(8002, "trig_500_add")
                assert st_late["status"] == "unconfirmed", f"Expected unconfirmed, got {st_late}"
                assert err_syncer.state.auto_attempts("trig_500_add")[0][3] == "unconfirmed"

            # 2c. 4xx during add -> unavailable
            def mock_af_400(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 100 * 1024**3}])
                if url.endswith("/qualityprofile"):
                    return Resp(body=[{"id": 1, "name": "HD"}])
                if url.endswith("/tag"):
                    return Resp(body={"id": 1, "label": "plex-orama-auto-fetch"} if method == "POST" else [{"id": 1, "label": "plex-orama-auto-fetch"}])
                if "/movie/lookup/tmdb" in url:
                    return Resp(body=[{"title": "Bad 400 Movie", "tmdbId": 8003}])
                if url.endswith("/queue"):
                    return Resp(body={"records": [], "totalRecords": 0})
                if url.endswith("/movie"):
                    if method == "GET":
                        return Resp(body=[])
                    if method == "POST":
                        return Resp(400, content=b"Invalid Root Folder")
                return Resp(200, {})

            with patch.object(requests.Session, "request", mock_af_400):
                res = err_syncer.tv_auto_fetch(8003, "trig_400")
                assert res["status"] == "unavailable", f"Expected unavailable, got {res}"
                assert err_syncer.state.auto_attempts("trig_400")[0][3] == "unavailable"

            # Also 404 during add -> unavailable
            def mock_af_404(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 100 * 1024**3}])
                if url.endswith("/qualityprofile"):
                    return Resp(body=[{"id": 1, "name": "HD"}])
                if url.endswith("/tag"):
                    return Resp(body={"id": 1, "label": "plex-orama-auto-fetch"} if method == "POST" else [{"id": 1, "label": "plex-orama-auto-fetch"}])
                if "/movie/lookup/tmdb" in url:
                    return Resp(body=[{"title": "404 Movie", "tmdbId": 8004}])
                if url.endswith("/queue"):
                    return Resp(body={"records": [], "totalRecords": 0})
                if url.endswith("/movie"):
                    if method == "GET":
                        return Resp(body=[])
                    if method == "POST":
                        return Resp(404, content=b"Not found")
                return Resp(200, {})

            with patch.object(requests.Session, "request", mock_af_404):
                res = err_syncer.tv_auto_fetch(8004, "trig_404")
                assert res["status"] == "unavailable", f"Expected unavailable, got {res}"

            # 2d. Quota after an added fetch within 7 days
            def mock_af_generic(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 100 * 1024**3}])
                if url.endswith("/movie") or url.endswith("/queue"):
                    return Resp(body=[] if url.endswith("/movie") else {"records": [], "totalRecords": 0})
                return Resp(200, {})

            # Re-activate 8001 as recently added (e.g. 1 hour ago)
            err_syncer.state.db.execute("UPDATE auto_fetch SET ts = ? WHERE trigger = 'trig_timeout_add'",
                                       (int(time.time()) - 3600,))
            with patch.object(requests.Session, "request", mock_af_generic):
                # 8001 is added within 7 days -> returns quota-reached
                res = err_syncer.tv_auto_fetch(8005, "trig_quota_test")
                assert res["status"] == "quota-reached", f"Expected quota-reached, got {res}"

                # If added > 7 days ago, quota is not blocked
                err_syncer.state.db.execute("UPDATE auto_fetch SET ts = ? WHERE trigger = 'trig_timeout_add'",
                                           (int(time.time()) - (8 * 86400),))
                assert err_syncer.state.reserve_auto("trig_allowed_after_7d", 8006) == "reserved"
                err_syncer.state.finish_auto("trig_allowed_after_7d", "unconfirmed")

            # 2e. Trigger-conflict
            err_syncer.state.reserve_auto("trig_conflict_test", 8007)
            with patch.object(requests.Session, "request", mock_af_generic):
                res = err_syncer.tv_auto_fetch(8008, "trig_conflict_test")
                assert res["status"] == "trigger-conflict", f"Expected trigger-conflict, got {res}"
                res = err_syncer.tv_auto_status(8008, "trig_conflict_test")
                assert res["status"] == "trigger-conflict", f"Expected trigger-conflict, got {res}"
            err_syncer.state.finish_auto("trig_conflict_test", "unconfirmed")

            # 2f. Low-space and space-unavailable
            def mock_low_space(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 20 * 1024**3}])
                return Resp(body=[] if url.endswith("/movie") else {"records": [], "totalRecords": 0})

            with patch.object(requests.Session, "request", mock_low_space):
                res = err_syncer.tv_auto_fetch(8010, "trig_low_space")
                assert res["status"] == "low-space", f"Expected low-space, got {res}"

            def mock_inaccessible_space(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": False, "freeSpace": 100 * 1024**3}])
                return Resp(body=[] if url.endswith("/movie") else {"records": [], "totalRecords": 0})

            with patch.object(requests.Session, "request", mock_inaccessible_space):
                res = err_syncer.tv_auto_fetch(8011, "trig_inaccessible")
                assert res["status"] == "space-unavailable", f"Expected space-unavailable, got {res}"

            def mock_none_space(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": None}])
                return Resp(body=[] if url.endswith("/movie") else {"records": [], "totalRecords": 0})

            with patch.object(requests.Session, "request", mock_none_space):
                res = err_syncer.tv_auto_fetch(8012, "trig_none_space")
                assert res["status"] == "space-unavailable", f"Expected space-unavailable, got {res}"

            def mock_empty_folders(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    return Resp(body=[])
                return Resp(body=[] if url.endswith("/movie") else {"records": [], "totalRecords": 0})

            with patch.object(requests.Session, "request", mock_empty_folders):
                res = err_syncer.tv_auto_fetch(8013, "trig_empty_folders")
                assert res["status"] == "space-unavailable", f"Expected space-unavailable, got {res}"

            # 2g. Settings changed mid-flight
            def mock_midflight_change(self, method, url, **kw):
                if url.endswith("/rootfolder"):
                    err_syncer.settings.update({"auto_fetch_free_gib": 99})
                    return Resp(body=[{"path": "/movies", "accessible": True, "freeSpace": 200 * 1024**3}])
                return Resp(body=[] if url.endswith("/movie") else {"records": [], "totalRecords": 0})

            with patch.object(requests.Session, "request", mock_midflight_change):
                res = err_syncer.tv_auto_fetch(8014, "trig_midflight")
                assert res["status"] == "unavailable", f"Expected unavailable, got {res}"
        finally:
            err_syncer.state.db.close()

error_path_tests()

# 12. TV error log tests: validation, auth, rate limit, bounding, redaction, escaping, and auto-fetch history
def tv_log_tests():
    with tempfile.TemporaryDirectory() as td:
        log_syncer = app.Syncer(td)
        try:
            log_syncer.settings.update({
                "radarr_url": "http://radarr:7878",
                "radarr_api_key": "rad_secret_12345",
                "tmdb_api_key": "tmdb_secret_9999",
                "auto_fetch": True,
                "auto_fetch_free_gib": 50,
            })
            pw = "log_test_pw"
            with ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(log_syncer, pw)) as log_srv:
                threading.Thread(target=log_srv.serve_forever, daemon=True).start()
                base_url = f"http://127.0.0.1:{log_srv.server_port}"
                auth_hdr = {"Authorization": "Basic " + b64.b64encode(f"admin:{pw}".encode()).decode()}

                def post_log(data, headers=None):
                    h = dict(auth_hdr)
                    if headers:
                        h.update(headers)
                    body = json.dumps(data).encode()
                    req = urllib.request.Request(base_url + "/api/tv/log", method="POST", data=body,
                                                 headers={"Content-Type": "application/json", **h})
                    try:
                        with urllib.request.urlopen(req) as r:
                            return r.status, r.read()
                    except urllib.error.HTTPError as e:
                        try:
                            return e.code, e.read()
                        except Exception:
                            return e.code, b""

                def get_errors(headers=None):
                    h = dict(auth_hdr)
                    if headers is not None:
                        h = headers
                    req = urllib.request.Request(base_url + "/api/tv-errors", headers=h)
                    try:
                        with urllib.request.urlopen(req) as r:
                            return r.status, json.loads(r.read() or b"{}")
                    except urllib.error.HTTPError as e:
                        try:
                            return e.code, json.loads(e.read() or b"{}")
                        except Exception:
                            return e.code, {}

                try:
                    # 1. Auth tests
                    # POST without auth -> 401
                    c, _ = post_log({"level": "error", "message": "unauthed"}, headers={"Authorization": ""})
                    assert c == 401, f"Expected 401 without auth, got {c}"
                    # GET /api/tv-errors without auth -> 401
                    c, _ = get_errors(headers={})
                    assert c == 401, f"Expected 401 without auth on /api/tv-errors, got {c}"

                    # 2. Validation tests
                    # Invalid levels -> 400
                    for bad_lvl in ("info", "debug", "fatal", "", None, 123):
                        c, b = post_log({"level": bad_lvl, "message": "msg"})
                        assert c == 400, f"Expected 400 for level {bad_lvl}, got {c}: {b}"

                    # Valid error & warn levels -> 204
                    c, _ = post_log({"level": "warn", "message": "warning message"})
                    assert c == 204, f"Expected 204, got {c}"
                    c, _ = post_log({"level": "error", "message": "error message"})
                    assert c == 204, f"Expected 204, got {c}"

                    # Truncation of oversized fields
                    long_msg = "m" * 600
                    long_where = "w" * 100
                    long_ver = "v" * 30
                    long_dev = "d" * 50
                    c, _ = post_log({
                        "level": "error",
                        "message": long_msg,
                        "where": long_where,
                        "version": long_ver,
                        "device": long_dev,
                    }, headers={"X-Forwarded-For": "10.0.0.1"})
                    assert c == 204

                    c, err_data = get_errors()
                    assert c == 200
                    latest = err_data["logs"][0]
                    assert len(latest["message"]) == 500
                    assert len(latest["where"]) == 80
                    assert len(latest["version"]) == 20
                    assert len(latest["device"]) == 40
                    assert latest["ip"] == "10.0.0.1"

                    # 3. Secret Redaction
                    c, _ = post_log({
                        "level": "error",
                        "message": "Failed calling rad_secret_12345 and tmdb_secret_9999 with Bearer eyJmytoken123",
                        "where": "test_redact",
                    }, headers={"X-Forwarded-For": "10.0.0.2"})
                    assert c == 204

                    c, err_data = get_errors()
                    redacted_entry = err_data["logs"][0]
                    assert "rad_secret_12345" not in redacted_entry["message"]
                    assert "tmdb_secret_9999" not in redacted_entry["message"]
                    assert "eyJmytoken123" not in redacted_entry["message"]
                    assert "[REDACTED]" in redacted_entry["message"]
                    assert "Bearer [REDACTED]" in redacted_entry["message"]

                    # 4. Rate Limiting (30/min per IP, excess drops with 204)
                    rate_ip = "192.168.100.1"
                    with log_syncer.tv_log_lock:
                        log_syncer.tv_logs.clear()
                        log_syncer.tv_log_rates.clear()

                    for i in range(30):
                        c, _ = post_log({"level": "warn", "message": f"msg {i}"}, headers={"X-Forwarded-For": rate_ip})
                        assert c == 204, f"Request {i} failed: {c}"

                    assert len(log_syncer.tv_logs) == 30

                    # 31st request from same IP is dropped with 204
                    c, _ = post_log({"level": "warn", "message": "msg 31 dropped"}, headers={"X-Forwarded-For": rate_ip})
                    assert c == 204
                    assert len(log_syncer.tv_logs) == 30, "Excess log should not be stored"

                    # Request from different IP succeeds and is stored
                    diff_ip = "192.168.100.2"
                    c, _ = post_log({"level": "error", "message": "other ip"}, headers={"X-Forwarded-For": diff_ip})
                    assert c == 204
                    assert len(log_syncer.tv_logs) == 31
                    assert log_syncer.tv_logs[-1]["ip"] == diff_ip

                    # 5. Bounded Deque (max 200 items)
                    with log_syncer.tv_log_lock:
                        log_syncer.tv_logs.clear()
                        log_syncer.tv_log_rates.clear()

                    for i in range(250):
                        log_syncer.record_tv_log(f"10.1.{i//250}.{i%250}", "error", f"entry {i}", where="bound_test")

                    assert len(log_syncer.tv_logs) == 200, f"Expected 200 items in deque, got {len(log_syncer.tv_logs)}"
                    messages = [entry["message"] for entry in log_syncer.get_tv_logs()]
                    assert messages[0] == "entry 249"  # newest first
                    assert messages[-1] == "entry 50"  # oldest remaining

                    # 6. Escaping in UI HTML
                    with open(os.path.join(app.HERE, "ui.html"), "r", encoding="utf-8") as f:
                        ui_text = f.read()

                    assert "renderTvErrors" in ui_text
                    assert 'id="tvErrBox"' in ui_text
                    assert 'id="autoFetchBox"' in ui_text
                    assert "${esc(x.message)}" in ui_text
                    assert "${esc(x.where || \"—\")}" in ui_text
                    assert "${esc(x.device || \"—\")}" in ui_text
                    assert "${esc(x.level)}" in ui_text
                    assert "${esc(x.ip || \"—\")}" in ui_text
                    assert "${esc(x.status)}" in ui_text
                    assert "${esc(x.trigger)}" in ui_text
                    assert "${esc(x.tmdb)}" in ui_text

                    # 7. Auto-fetch decisions surfaced in GET /api/tv-errors
                    log_syncer.state.record_auto("trig_low", 9001, "low-space")
                    log_syncer.state.record_auto("trig_quota", 9002, "quota-reached")
                    log_syncer.state.record_auto("trig_unavailable", 9003, "space-unavailable")

                    c, err_resp = get_errors()
                    assert c == 200
                    af_rows = err_resp.get("auto_fetches", [])
                    assert len(af_rows) >= 3
                    af_by_trig = {r["trigger"]: r for r in af_rows}
                    assert af_by_trig["trig_low"]["status"] == "low-space"
                    assert af_by_trig["trig_quota"]["status"] == "quota-reached"
                    assert af_by_trig["trig_unavailable"]["status"] == "space-unavailable"
                    assert all("ts" in r and "time" in r and "tmdb" in r for r in af_rows)

                    # 8. Perf entries in TV log
                    c, _ = post_log({
                        "level": "perf",
                        "message": "start 1180ms",
                        "where": "app_init",
                        "version": "1.0",
                        "device": "Samsung TV",
                    }, headers={"X-Forwarded-For": "10.0.0.99"})
                    assert c == 204, f"Expected 204 for perf level, got {c}"

                    c, err_data = get_errors()
                    assert c == 200
                    assert "perf" in err_data, "Missing 'perf' key in /api/tv-errors"
                    assert len(err_data["perf"]) >= 1
                    p_entry = err_data["perf"][0]
                    assert p_entry["level"] == "perf"
                    assert p_entry["message"] == "start 1180ms"
                    assert p_entry["where"] == "app_init"
                    assert p_entry["device"] == "Samsung TV"
                    assert p_entry["version"] == "1.0"
                    assert p_entry["ip"] == "10.0.0.99"
                    assert "ts" in p_entry

                    # Perf entries do not evict errors (separate bounded deques of 200)
                    with log_syncer.tv_log_lock:
                        log_syncer.tv_logs.clear()
                        log_syncer.tv_perf.clear()
                        log_syncer.tv_log_rates.clear()

                    for i in range(200):
                        log_syncer.record_tv_log(f"10.2.{i//200}.{i%200}", "error", f"err {i}")
                    assert len(log_syncer.tv_logs) == 200

                    for i in range(250):
                        log_syncer.record_tv_log(f"10.3.{i//200}.{i%200}", "perf", f"play {i}ms")
                    # Errors still 200 (not evicted by perf)
                    assert len(log_syncer.tv_logs) == 200
                    # Perf bounded to 200
                    assert len(log_syncer.tv_perf) == 200
                    perf_messages = [x["message"] for x in log_syncer.get_tv_perf()]
                    assert perf_messages[0] == "play 249ms"  # newest first
                    assert perf_messages[-1] == "play 50ms"  # oldest remaining

                    # Shares per-IP rate limit with error/warn
                    with log_syncer.tv_log_lock:
                        log_syncer.tv_logs.clear()
                        log_syncer.tv_perf.clear()
                        log_syncer.tv_log_rates.clear()
                    shared_ip = "192.168.200.5"
                    for i in range(15):
                        post_log({"level": "error", "message": f"err {i}"}, headers={"X-Forwarded-For": shared_ip})
                    for i in range(15):
                        post_log({"level": "perf", "message": f"home {i}ms"}, headers={"X-Forwarded-For": shared_ip})
                    assert len(log_syncer.tv_logs) == 15
                    assert len(log_syncer.tv_perf) == 15
                    # 31st request from same IP is dropped by rate limit
                    c, _ = post_log({"level": "perf", "message": "dropped perf"}, headers={"X-Forwarded-For": shared_ip})
                    assert c == 204
                    assert len(log_syncer.tv_perf) == 15, "Rate-limited perf entry should not be stored"

                    # UI HTML checks for TV performance section
                    assert 'id="tvPerfBox"' in ui_text
                    assert "TV performance" in ui_text
                finally:
                    log_srv.shutdown()
        finally:
            log_syncer.state.db.close()

tv_log_tests()

def single_title_lookup_tests():
    cfg = app.Config(s.settings.all())
    r_arr = app.Arr("Radarr", cfg.radarr)
    s_arr = app.Arr("Sonarr", cfg.sonarr)

    # 1. Movie lookup by tmdbId: requests ?tmdbId=..., never unfiltered /movie
    radarr_movies[:] = [{"id": 501, "tmdbId": 12345, "title": "Single Movie", "imdbId": "tt12345"}]
    calls.clear()
    found = r_arr.find(12345)
    assert found and found["id"] == 501
    movie_calls = [c for c in calls if c[0] == "GET" and c[1].endswith("/movie")]
    assert len(movie_calls) == 1, movie_calls
    assert movie_calls[0][2] == {"tmdbId": 12345}, movie_calls[0]

    # 2. Series lookup by tvdbId: requests ?tvdbId=..., never unfiltered /series
    sonarr_series[:] = [{"id": 601, "tvdbId": 54321, "title": "Single Show", "imdbId": "tt54321"}]
    calls.clear()
    found = s_arr.find(54321)
    assert found and found["id"] == 601
    series_calls = [c for c in calls if c[0] == "GET" and c[1].endswith("/series")]
    assert len(series_calls) == 1, series_calls
    assert series_calls[0][2] == {"tvdbId": 54321}, series_calls[0]

    # 3. Lookup when ID not found and no IMDb given: no unfiltered list call
    calls.clear()
    not_found = r_arr.find(99999)
    assert not_found is None
    movie_calls = [c for c in calls if c[0] == "GET" and c[1].endswith("/movie")]
    assert len(movie_calls) == 1 and movie_calls[0][2] == {"tmdbId": 99999}

    # 4. Lookup when ID not found BUT IMDb is given: falls back to unfiltered list
    calls.clear()
    found_imdb = r_arr.find(99999, imdb="tt12345")
    assert found_imdb and found_imdb["id"] == 501
    movie_calls = [c for c in calls if c[0] == "GET" and c[1].endswith("/movie")]
    assert len(movie_calls) == 2
    assert movie_calls[0][2] == {"tmdbId": 99999}
    assert not movie_calls[1][2], movie_calls[1]

    # 5. Lookup with only IMDb given (no ext_id): requests unfiltered list directly
    calls.clear()
    found_imdb_only = r_arr.find(None, imdb="tt12345")
    assert found_imdb_only and found_imdb_only["id"] == 501
    movie_calls = [c for c in calls if c[0] == "GET" and c[1].endswith("/movie")]
    assert len(movie_calls) == 1 and not movie_calls[0][2]

    print("single-title lookup tests OK")

single_title_lookup_tests()

def shared_library_tests():
    # 1. Clear cache and calls
    s._invalidate_library()
    calls.clear()

    # Restore plex token so sync cycle runs
    s.settings.update({"plex_token": "tok-from-pin"})
    r_res = s.run_once()
    assert "skipped" not in r_res, r_res
    initial_movie_full = len([c for c in calls if c[0] == "GET" and c[1].endswith("/movie") and not c[2]])
    initial_series_full = len([c for c in calls if c[0] == "GET" and c[1].endswith("/series") and not c[2]])
    assert initial_movie_full == 1, f"Expected 1 full /movie fetch in sync cycle, got {initial_movie_full}"
    assert initial_series_full == 1, f"Expected 1 full /series fetch in sync cycle, got {initial_series_full}"

    # Now perform TV lookups (e.g. for movie and show)
    # Since _library() was seeded by the sync cycle, TV lookups must reuse the cached library
    s.tv_lookup("movie", ext_id=27205)
    s.tv_lookup("show", ext_id=81189)

    total_movie_full = len([c for c in calls if c[0] == "GET" and c[1].endswith("/movie") and not c[2]])
    total_series_full = len([c for c in calls if c[0] == "GET" and c[1].endswith("/series") and not c[2]])

    assert total_movie_full == 1, f"Expected still 1 full /movie fetch across sync + lookup, got {total_movie_full}"
    assert total_series_full == 1, f"Expected still 1 full /series fetch across sync + lookup, got {total_series_full}"

    # 2. Test invalidation after delete
    was_dry = s.settings.all()["dry_run"]
    s.settings.update({"dry_run": False})
    # Add a mock movie to delete
    radarr_movies[:] = [x for x in radarr_movies if x.get("tmdbId") != 27205] + [{"id": 31, "tmdbId": 27205, "title": "Inception"}]
    s.tv_delete("movie", 27205, "", "Inception")
    s.settings.update({"dry_run": was_dry})

    # Cache for movie should be invalidated
    calls.clear()
    s.tv_lookup("movie", ext_id=27205)
    after_del_movie_full = len([c for c in calls if c[0] == "GET" and c[1].endswith("/movie") and not c[2]])
    assert after_del_movie_full == 1, f"Expected re-fetch after delete invalidation, got {after_del_movie_full}"

    print("shared library tests OK")

shared_library_tests()

def gzip_tests():
    import http.client, gzip
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)

    # 1. GET / with Accept-Encoding: gzip (ui.html is ~27KB > 1KB)
    conn.request("GET", "/", headers={"Accept-Encoding": "gzip"})
    r = conn.getresponse()
    headers = dict(r.getheaders())
    body = r.read()
    assert r.status == 200
    assert headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in headers.get("Vary", "")
    assert headers.get("Content-Length") == str(len(body))
    decompressed = gzip.decompress(body)
    assert decompressed.startswith(b"<!doctype html>")
    assert len(body) < len(decompressed)

    # 2. GET / without Accept-Encoding
    conn.request("GET", "/")
    r = conn.getresponse()
    headers = dict(r.getheaders())
    body = r.read()
    assert r.status == 200
    assert "Content-Encoding" not in headers
    assert headers.get("Content-Length") == str(len(body))
    assert body.startswith(b"<!doctype html>")

    # 3. Small body (e.g. GET /health ~ 13 bytes) with Accept-Encoding: gzip -> not gzipped (< 1KB)
    conn.request("GET", "/health", headers={"Accept-Encoding": "gzip"})
    r = conn.getresponse()
    headers = dict(r.getheaders())
    body = r.read()
    assert r.status == 200
    assert "Content-Encoding" not in headers
    assert body == b'{"ok": true}'

    # 4. HEAD request on / with Accept-Encoding: gzip -> never gzipped, no body
    conn.request("HEAD", "/", headers={"Accept-Encoding": "gzip"})
    r = conn.getresponse()
    headers = dict(r.getheaders())
    body = r.read()
    assert r.status == 200
    assert "Content-Encoding" not in headers
    assert len(body) == 0

    # 5. 204 response (OPTIONS) with Accept-Encoding: gzip -> never gzipped
    conn.request("OPTIONS", "/api/tv/sync", headers={"Accept-Encoding": "gzip"})
    r = conn.getresponse()
    headers = dict(r.getheaders())
    body = r.read()
    assert r.status == 204
    assert "Content-Encoding" not in headers
    assert headers.get("Content-Length") == "0"
    assert len(body) == 0

    conn.close()
    print("gzip tests OK")

gzip_tests()

def guid_grouping_tests():
    assert app.external_num("81189") == 81189
    assert app.external_num("81189?lang=en") == 81189
    assert app.external_num("81189/1") == 81189
    assert app.external_num("movie/27205") == 27205
    assert app.external_num("  42 ") == 42
    assert app.external_num("nope") is None
    assert app.external_num(True) is None
    assert app.external_num(False) is None
    assert app.external_num(0) is None
    assert app.external_num(-3) is None
    assert app.external_num(None) is None
    assert app.external_num(12.5) is None

    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            plex = app.Plex("tok", sy.client_id, sy.state)
            plex._get_json = lambda url, **params: {"MediaContainer": {"Metadata": [{"Guid": [
                {"id": "tvdb://81189?lang=en"},
                {"id": "imdb://tt0903747"},
                {"id": "tmdb://movie/1396"},
                "nope",
                {"id": None},
                {"id": "tvdb://not-a-number"},
            ]}]}}
            assert plex.guids("rk1") == {"tvdb": "81189", "imdb": "tt0903747", "tmdb": "1396"}

            class P:
                def guids(self, rk):
                    return {
                        "good": {"tvdb": "81189"},
                        "dirty": {"tvdb": "305288?lang=en"},
                        "bad": {"tvdb": "nope"},
                    }[rk]
            sy.plex = P()
            summary = {"errors": []}
            grouped = sy._group_by({
                "good": {"title": "Breaking Bad", "users": {"a"}},
                "dirty": {"title": "Stranger Things", "users": {"b"}},
                "bad": {"title": "Mystery", "users": {"c"}},
            }, "tvdb", summary)
            assert set(grouped) == {81189, 305288}, grouped
            assert grouped[305288]["users"] == {"b"}
            assert summary["errors"] == []
            assert "Mystery" not in {v["title"] for v in grouped.values()}
        finally:
            sy.state.db.close()
    print("guid grouping tests OK")

guid_grouping_tests()

def watchlist_paging_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            plex = app.Plex("tok", sy.client_id, sy.state)
            calls = []

            def get_json(url, **params):
                assert "watchlist/all" in url
                start = int(params.get("X-Plex-Container-Start", 0))
                calls.append(start)
                if start == 0:
                    meta = [{"title": "skip-me", "type": "show"}, "junk"]
                    meta += [{"ratingKey": i, "title": f"T{i}", "type": "movie"} for i in range(1, 99)]
                    # 2 junk + 98 titles = 100. totalSize omitted, and one row has no ratingKey.
                    assert len(meta) == 100
                    return {"MediaContainer": {"totalSize": None, "Metadata": meta}}
                if start == 100:
                    return {"MediaContainer": {"Metadata": [
                        {"ratingKey": "last", "title": "Last", "type": "SHOW"},
                    ]}}
                raise AssertionError(f"unexpected page {start}")

            plex._get_json = get_json
            items = list(plex.own_watchlist())
            assert calls == [0, 100], calls
            assert [k for k, _, _ in items][0] == "1"
            assert items[-1][0] == "last" and items[-1][2] == "show"
            assert len(items) == 99  # 98 numbered + last; the row without ratingKey is skipped
            assert all(isinstance(t, str) for _, t, _ in items)

            spins = {"n": 0}

            def spin(url, **params):
                spins["n"] += 1
                return {"MediaContainer": {"totalSize": 10**9, "Metadata": []}}

            plex._get_json = spin
            assert list(plex.own_watchlist()) == []
            assert spins["n"] == 1

            repeats = {"n": 0}

            def repeat(url, **params):
                repeats["n"] += 1
                return {"MediaContainer": {"totalSize": 10**9, "Metadata": [
                    {"ratingKey": "same", "title": "Same", "type": "movie"},
                ] * 100}}

            plex._get_json = repeat
            assert [k for k, _, _ in plex.own_watchlist()] == ["same"] * 100
            assert repeats["n"] == 2  # second page repeats the first id and stops

            cursors = []

            def graphql(query, variables=None):
                variables = variables or {}
                cursors.append(variables.get("after"))
                if variables.get("after") == "c1":
                    return {"user": {"watchlist": {"nodes": [
                        {"id": "a", "title": "A", "type": "SHOW"},
                        {"title": "no-id", "type": "MOVIE"},
                        "x",
                    ], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}}
                return {"user": {"watchlist": {"nodes": [
                    {"id": "b", "title": "B", "type": "movie"},
                ], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}}

            plex._graphql = graphql
            got = list(plex.friend_watchlist("u"))
            assert [i[0] for i in got] == ["b", "a"], got
            assert cursors == [None, "c1"]

            plex._graphql = lambda *a, **k: {"user": None}
            try:
                list(plex.friend_watchlist("u"))
                raise AssertionError("null user should fail the friend's list")
            except RuntimeError:
                pass

            plex._graphql = lambda *a, **k: {"allFriendsV2": [
                {"user": {"id": "ok", "username": "mia"}},
                {"user": {"id": "", "username": "blank"}},
                "junk",
                {"user": {"username": "noid"}},
            ]}
            assert plex.friends() == [{"id": "ok", "username": "mia"}]

            sy.build()
            class P:
                def owner_name(self):
                    raise RuntimeError("plex-account-down")
                def own_watchlist(self):
                    raise AssertionError("owner list should not be read without a name")
                def friends(self):
                    return [{"id": "f1", "username": "mia"}]
                def server_usernames(self):
                    return {"mia"}
                def friend_watchlist(self, uid):
                    yield "9", "Show", "show"
            sy.plex = P()
            items = sy.collect()
            assert items["9"]["title"] == "Show" and "mia" in items["9"]["users"]
            assert sy.users[0]["status"] == "error"
            assert any(u["name"] == "mia" and u["status"] == "ok" for u in sy.users)

            class P2(P):
                def owner_name(self):
                    return "owner"
                def own_watchlist(self):
                    yield "1", "Owned", "movie"
                def server_usernames(self):
                    raise RuntimeError("users xml broke")
            sy.plex = P2()
            items = sy.collect()
            assert items["1"]["title"] == "Owned"
            assert "9" not in items  # friends skipped rather than opened to everyone
            assert all(u["role"] != "friend" for u in sy.users)
        finally:
            sy.state.db.close()
    print("watchlist paging tests OK")

watchlist_paging_tests()

def interval_limit_tests():
    with tempfile.TemporaryDirectory() as td:
        st = app.State(os.path.join(td, "state.db"))
        try:
            sett = app.Settings(st)
            sett.update({"interval_min": 10**400, "auto_fetch_free_gib": 10**12})
            assert sett.all()["interval_min"] == 10080
            # Only the sync interval is capped. A huge free-space threshold must not
            # be rewritten into a small one, or auto-fetch would start downloading.
            assert sett.all()["auto_fetch_free_gib"] == 10**12
            saved = json.loads(st.get("settings"))
            assert saved["interval_min"] == 10080
            # A value already stored before the cap still can't reach Event.wait.
            st.set("settings", json.dumps({"interval_min": 10**400}))
            assert sett.all()["interval_min"] == 10080
        finally:
            st.db.close()
    print("interval limit tests OK")

interval_limit_tests()

def auto_fetch_order_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({
                "auto_fetch": True,
                "dry_run": False,
                "radarr_url": "http://localhost:7878",
                "radarr_api_key": "k",
                "radarr_search": True,
            })
            now = int(time.time())
            sy.state.db.execute(
                "INSERT INTO auto_fetch VALUES ('other-pending', 111, ?, 'pending')", (now,))
            sy.state.record_auto("mine", 222, "added")
            real = sy.state.auto_attempts

            def other_first(trigger):
                rows = list(real(trigger))
                return sorted(rows, key=lambda r: 0 if r[0] == "other-pending" else 1)

            sy.state.auto_attempts = other_first
            assert other_first("mine")[0][0] == "other-pending"
            assert sy.tv_auto_fetch(222, "mine")["status"] == "added"
            # A trigger we have never seen still yields to someone else's fresh pending row.
            assert sy.tv_auto_fetch(333, "new-one")["status"] == "quota-reached"
        finally:
            sy.state.db.close()
    print("auto-fetch order tests OK")

auto_fetch_order_tests()

def exclude_thread_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({"plex_token": "secret-plex-zzz"})
            sy.action = {"user": "a", "state": "running", "deleted": [], "untagged": [], "errors": [], "total": 1}
            sy.build = lambda: (_ for _ in ()).throw(RuntimeError("boom secret-plex-zzz"))
            sy._do_exclude("a", {"delete": [{"service": "sonarr", "id": 1, "title": "Show"}], "keep": []}, True)
            assert sy.action["state"] == "done"
            assert sy.action["errors"] and "secret-plex-zzz" not in sy.action["errors"][0]
            assert "boom" in sy.action["errors"][0]
        finally:
            sy.state.db.close()
    print("exclude thread tests OK")

exclude_thread_tests()

def exclude_race_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.action = {"state": "running", "user": "x", "deleted": [], "untagged": [], "errors": [], "total": 0}
            assert sy.exclude_user("newperson", False) is None
            assert "newperson" not in sy.settings.all()["user_block"].lower()
        finally:
            sy.state.db.close()
    s.action = {"state": "running", "user": "x", "deleted": [], "untagged": [], "errors": [], "total": 0}
    try:
        try:
            call("/api/users/somebody/exclude", {"delete": False})
            raise AssertionError("expected 409")
        except urllib.error.HTTPError as e:
            assert e.code == 409, e.code
        assert "somebody" not in s.settings.all()["user_block"].lower()
    finally:
        s.action = {"state": "done", "user": "x", "deleted": [], "untagged": [], "errors": [], "total": 0}
    print("exclude race tests OK")

exclude_race_tests()

def cache_lock_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({
                "dry_run": False,
                "radarr_url": "http://localhost:7878",
                "radarr_api_key": "k",
                "plex_token": "t",
            })
            sy._queue_cache = (time.time(), {"items": [1]})
            entered = []
            real_lock = sy.cache_lock

            class Wrap:
                def __enter__(self):
                    entered.append("lock")
                    return real_lock.__enter__()
                def __exit__(self, *args):
                    entered.append("unlock")
                    return real_lock.__exit__(*args)
                def locked(self):
                    return real_lock.locked()

            sy.cache_lock = Wrap()
            with patch.object(app.Arr, "find", return_value={"id": 3, "tmdbId": 9, "monitored": True, "hasFile": False}), \
                 patch.object(app.Arr, "queued", return_value=False), \
                 patch.object(app.Arr, "active_search_ids", return_value=set()), \
                 patch.object(app.Arr, "post", return_value={"id": 1}):
                assert sy.tv_search_again("movie", 9)["status"] == "searching"
            assert entered == ["lock", "unlock"], entered
            assert "_queue_cache" not in sy.__dict__

            events = []

            class Spy(dict):
                def clear(self):
                    events.append("clear")
                    dict.clear(self)

            class Wrap2(Wrap):
                def __enter__(self):
                    events.append("lock")
                    return real_lock.__enter__()
                def __exit__(self, *args):
                    events.append("unlock")
                    return real_lock.__exit__(*args)

            sy.cache_lock = Wrap2()
            sy._collection_cache = Spy({1: (0, {"collection": None})})
            sy._queue_cache = (1, {"items": []})
            with patch.object(app.Arr, "find", return_value={"id": 4, "tmdbId": 9, "title": "M"}), \
                 patch.object(app.Arr, "delete", return_value=None):
                res = sy.tv_delete("movie", 9, "", "M")
            assert res["deleted"] is True
            assert events[events.index("clear") - 1] == "lock", events
            assert sy._queue_cache is None
            assert sy._collection_cache == {}
        finally:
            sy.state.db.close()
    print("cache lock tests OK")

cache_lock_tests()

def collection_cache_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({"radarr_url": "http://localhost:7878", "radarr_api_key": "k"})
            now = time.time()
            cache = {i: (now - 1000, {"collection": None}) for i in range(30)}
            cache.update({10000 + i: (now, {"collection": i}) for i in range(210)})
            sy._collection_cache = cache

            def fake_req(self, method, url, params=None, **kw):
                if "/lookup/" in url:
                    return Resp(body={})
                return Resp(body=[])

            with patch.object(requests.Session, "request", fake_req):
                out = sy.tv_collection(424242)
            assert out == {"collection": None}
            stored = sy._collection_cache
            assert 424242 in stored
            assert all(k >= 10000 or k == 424242 for k in stored), sorted(stored)[:5]
            assert len(stored) <= 200, len(stored)
        finally:
            sy.state.db.close()
    print("collection cache tests OK")

collection_cache_tests()

def collection_lookup_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({"radarr_url": "http://localhost:7878", "radarr_api_key": "k"})

            def fake_req(self, method, url, params=None, **kw):
                if "/lookup/" in url:
                    return Resp(body=[{"collection": {"tmdbId": 9, "title": "Saga"}}])
                if url.endswith("/collection"):
                    assert params["tmdbId"] == 9
                    return Resp(body=[{"title": "Saga", "movies": [
                        {"tmdbId": 1, "title": "One", "year": 2001, "isExisting": False}]}])
                if url.endswith("/movie"):
                    return Resp(body=[{"collection": ["not-a-dict"]}])
                return Resp(body=[])

            with patch.object(requests.Session, "request", fake_req):
                out = sy.tv_collection(77)
            assert out["collection"]["title"] == "Saga", out
            assert [m["title"] for m in out["collection"]["movies"]] == ["One"]
        finally:
            sy.state.db.close()
    print("collection lookup tests OK")

collection_lookup_tests()

def subtitle_shape_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({
                "radarr_url": "http://localhost:7878", "radarr_api_key": "k",
                "sonarr_url": "http://localhost:8989", "sonarr_api_key": "k",
                "bazarr_url": "http://localhost:6767", "bazarr_api_key": "bz",
            })
            release = "Movie.2020.1080p.BluRay"

            def fake_get(url, params=None, headers=None, timeout=None):
                return Resp(body={"data": [{
                    "provider": "opensubtitles", "language": "en", "score": 10,
                    "release_info": release, "subtitle": "P",
                }]})

            def fake_req(self, method, url, params=None, **kw):
                if url.endswith("/movie"):
                    return Resp(body=[{"id": 7, "tmdbId": 5, "title": "M"}])
                if url.endswith("/series"):
                    return Resp(body=[{"id": 8, "tvdbId": 9, "title": "S"}])
                if url.endswith("/episode"):
                    return Resp(body={"id": 3, "seasonNumber": 1, "episodeNumber": 2})
                return Resp(body=[])

            with patch.object(requests.Session, "request", fake_req), \
                 patch.object(app.requests, "get", fake_get):
                movie = sy.tv_subtitle_search("movie", tmdb=5)
                assert movie["results"][0]["release"] == release, movie
                ep = sy.tv_subtitle_search("episode", tvdb=9, season=1, episode=2)
                assert ep["results"][0]["release"] == release
        finally:
            sy.state.db.close()
    print("subtitle shape tests OK")

subtitle_shape_tests()

def delete_rollback_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({"radarr_url": "http://localhost:7878", "radarr_api_key": "k", "dry_run": False})
            order = []

            def spy_delete(self, obj):
                order.append(("delete", sy.state.is_handled("radarr", 9)))
                raise app.ArrError(500, "down")

            with patch.object(app.Arr, "find", return_value={"id": 4, "tmdbId": 9, "title": "M"}), \
                 patch.object(app.Arr, "delete", spy_delete):
                try:
                    sy.tv_delete("movie", 9, "", "M")
                    raise AssertionError("expected ArrError")
                except app.ArrError:
                    pass
            assert order == [("delete", True)]
            assert not sy.state.is_handled("radarr", 9)

            with patch.object(app.Arr, "find", return_value={"id": 4, "tmdbId": "9", "title": "M"}), \
                 patch.object(app.Arr, "delete", return_value=None):
                res = sy.tv_delete("movie", 9, "", "M")
            assert res["deleted"] is True
            assert sy.state.deleted_users("radarr", 9) == set()
        finally:
            sy.state.db.close()
    print("delete rollback tests OK")

delete_rollback_tests()

def lookup_shape_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            sy.settings.update({
                "sonarr_url": "http://localhost:8989", "sonarr_api_key": "k",
                "radarr_url": "http://localhost:7878", "radarr_api_key": "k",
                "dry_run": False,
            })
            show = {"title": "Odd Show", "tvdbId": 55, "genres": ["Drama"],
                    "originalLanguage": {"name": "English"}}
            with patch.object(app.Arr, "find", return_value=None), \
                 patch.object(app.Arr, "get", return_value=show), \
                 patch.object(app.Arr, "quality_profile_id", return_value=1), \
                 patch.object(app.Arr, "root_folder", return_value="/tv"), \
                 patch.object(app.Arr, "tag_ids", return_value=[]), \
                 patch.object(app.Arr, "post", return_value={"id": 1}):
                out = sy.tv_request("show", 55, "ada")
            assert out["status"] == "added" and out["title"] == "Odd Show", out

            with patch.object(app.Arr, "find", return_value=None), \
                 patch.object(app.Arr, "get", return_value=["nope"]):
                try:
                    sy.tv_request("movie", 1, "ada")
                    raise AssertionError("expected LookupError")
                except LookupError as e:
                    assert "couldn't find" in str(e)

            summary = {"errors": [], "added_series": []}
            sy.cfg = app.Config(sy.settings.all())
            sy.sonarr = app.Arr("Sonarr", sy.cfg.sonarr)
            with patch.object(sy, "_group_by", return_value={56: {"title": "Odd Show", "users": {"ada"}}}), \
                 patch.object(sy, "_library", return_value={}), \
                 patch.object(sy, "_sonarr_ctx", return_value={
                     "profile": 1, "root": "/tv", "anime_root": None,
                     "anime_profile": 1, "lang_profile": None}), \
                 patch.object(app.Arr, "get", return_value=show), \
                 patch.object(app.Arr, "post", return_value={"id": 1}), \
                 patch.object(app.Arr, "tag_ids", return_value=[]):
                sy._sync_sonarr({}, summary)
            assert summary["added_series"] == ["Odd Show"], summary
            assert summary["errors"] == []
        finally:
            sy.state.db.close()
    print("lookup shape tests OK")

lookup_shape_tests()

def profile_shape_tests():
    cfg = type("C", (), {"url": "http://localhost:7878", "api_key": "k",
                         "quality_profile": "", "root_folder": ""})()
    arr = app.Arr("Radarr", cfg)
    with patch.object(arr, "get", return_value=[{"id": 1}, {"id": 2, "name": "HD"}]):
        assert arr.quality_profile_id() == 1
        assert arr.quality_profile_id("hd") == 2
    with patch.object(arr, "get", return_value={"id": 3, "name": "Any"}):
        assert arr.quality_profile_id() == 3
    with patch.object(arr, "get", return_value=[{"id": 9}, {"path": "/tv"}, {"path": None}, "nope"]):
        assert arr.root_folder() == "/tv"
    print("profile shape tests OK")

profile_shape_tests()

def queue_shape_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            class Q:
                def queue_records(self, **params):
                    return [
                        {"movieId": 5, "status": "downloading", "size": "1000", "sizeleft": "250",
                         "movie": {"title": "M", "tmdbId": 5},
                         "episode": {"seasonNumber": "1", "episodeNumber": "4"}},
                        {"movieId": 6, "status": "queued", "size": "nope", "sizeleft": True,
                         "movie": {"title": "N", "tmdbId": 6},
                         "episode": {"seasonNumber": 1, "episodeNumber": "nope"}},
                    ]
            out = {x["tmdb"]: x for x in sy._queue_of(Q(), "movie")}
            assert out[5]["size"] == 1000 and out[5]["progress"] == 75.0 and out[5]["detail"] == "S01E04", out[5]
            assert out[6]["size"] == 0 and out[6]["progress"] == 0 and out[6]["detail"] == ""
        finally:
            sy.state.db.close()
    print("queue shape tests OK")

queue_shape_tests()

def anime_shape_tests():
    assert app.is_anime({"genres": ["Animation"], "originalLanguage": "Japanese"})
    assert app.is_anime({"genres": "Anime"})
    assert not app.is_anime({"genres": "Drama"})
    assert not app.is_anime({"genres": 5, "originalLanguage": {"name": "Japanese"}})
    assert not app.is_anime(None)
    print("anime shape tests OK")

anime_shape_tests()

def sync_secret_tests():
    with tempfile.TemporaryDirectory() as td:
        sy = app.Syncer(td)
        try:
            secret = "plex-secret-xyz"
            sy.settings.update({
                "plex_token": secret,
                "sonarr_url": "http://localhost:8989",
                "sonarr_api_key": "k",
            })

            def boom():
                raise RuntimeError("failed " + secret)

            sy._last_ping = time.time()
            with patch.object(sy, "collect", boom):
                summary = sy._run_once()
            blob = " ".join(summary["errors"])
            assert secret not in blob, blob
            assert "[REDACTED]" in blob, blob
            assert secret not in json.dumps(sy.last_run)
        finally:
            sy.state.db.close()
    print("sync secret tests OK")

sync_secret_tests()

print("recent:", [(x["title"], x["users"], x["result"]) for x in s.state.recent()])
print("ALL OK")
