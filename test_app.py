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
        "201": ["tvdb://305288"], "202": ["tvdb://81189"], "203": ["tvdb://999"]}
sonarr_series, radarr_movies = [{"id": 1, "tvdbId": 999, "tags": []}], []
tags = {}
IMG = "https://image.tmdb.org/t/p/original/"
IMPORTLIST = [
    {"tmdbId": 438631, "title": "Dune", "year": 2021, "genres": ["Science Fiction", "Adventure"],
     "ratings": {"tmdb": {"value": 7.8}}, "overview": "Spice.", "isRecommendation": True,
     "images": [{"coverType": "poster", "remoteUrl": IMG + "dune.jpg"}, {"coverType": "fanart", "remoteUrl": IMG + "dune-bg.jpg"}]},
    {"tmdbId": 27205, "title": "Inception", "isExisting": True},          # already in Radarr
    {"tmdbId": 555, "title": "Excluded One", "isExcluded": True},
    {"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "genres": ["Science Fiction"],
     "ratings": {"imdb": {"value": 8.6}}, "images": []},
]


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
    if url.endswith("/importlist/movie"):
        assert params.get("includeRecommendations") == params.get("includeTrending") == params.get("includePopular") == "true"
        return Resp(body=IMPORTLIST)
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
        return Resp(body={"MediaContainer": {"totalSize": 2, "Metadata": [
            {"ratingKey": "101", "title": "Breaking Bad", "type": "show"},
            {"ratingKey": "102", "title": "Inception", "type": "movie"}]}})
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
        return Resp(body=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}])
    if url.endswith("/rootfolder"):
        return Resp(body=[{"path": "/tv/"}] if ":8989" in url else [{"path": "/movies"}])
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
        return Resp(body=[{"title": f"show{tid}", "tvdbId": tid, "seasons": []}])
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
assert t1["ok"] and t1["version"] == "4.0.15" and t1["roots"] == ["/tv/"], t1
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

# --- TV clients (FlexPlex): suggestions + on-the-fly requests
s.settings.update({"dry_run": False})
info = call("/api/tv")
assert info["product"] == "watchlist-sync" and info["radarr"] and not info["dry_run"], info
sug = call("/api/tv/suggestions")
titles = [m["title"] for m in sug["movies"]]
assert titles == ["Dune", "Dune: Part Two"], titles          # existing + excluded filtered out
dune = sug["movies"][0]
assert dune["poster"] == "https://image.tmdb.org/t/p/w342/dune.jpg", dune
assert dune["backdrop"] == "https://image.tmdb.org/t/p/w1280/dune-bg.jpg" and dune["rating"] == 7.8
assert sug["movies"][1]["rating"] == 8.6 and sug["movies"][1]["poster"] == ""
calls.clear()
res = call("/api/tv/request", {"tmdbId": 438631, "title": "Dune"})
assert res["ok"] and res["status"] == "added", res
post = [c for c in calls if c[0] == "POST" and c[1].endswith("/movie")][0][3]
assert post["tmdbId"] == 438631 and post["addOptions"]["searchForMovie"] is True
assert tags["plex-aggelos"] in post["tags"], post
assert any("/api/system/tasks" in c[1] and c[2]["taskid"] == "update_movies" for c in calls)  # Bazarr nudged
again = call("/api/tv/request", {"tmdbId": 438631})
assert again["status"] == "already-requested", again
assert [m["title"] for m in call("/api/tv/suggestions")["movies"]] == ["Dune: Part Two"]  # requested one gone
assert s.state.is_handled("radarr", 438631)
assert not s.run_once()["added_movies"]                      # the watchlist sync won't re-add it
s.settings.update({"dry_run": True}); calls.clear()
dry = call("/api/tv/request", {"tmdbId": 693134})
assert dry["status"] == "dry-run" and not [c for c in calls if c[0] == "POST" and c[1].endswith("/movie")], dry
assert not s.state.is_handled("radarr", 693134)
s.settings.update({"dry_run": False})
try:
    call("/api/tv/request", {"title": "no id"}); raise AssertionError("expected 400")
except urllib.error.HTTPError as e:
    assert e.code == 400

# CORS only on /api/tv, preflight works, and UI_PASSWORD still applies
def raw(path, method="GET", headers=None, server=base):
    req = urllib.request.Request(server + path, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)
code, h = raw("/api/tv/request", "OPTIONS")
assert code == 204 and h["Access-Control-Allow-Origin"] == "*" and "Authorization" in h["Access-Control-Allow-Headers"]
assert raw("/api/tv")[1].get("Access-Control-Allow-Origin") == "*"
assert "Access-Control-Allow-Origin" not in raw("/api/status")[1]
srv2 = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(s, "pw"))
threading.Thread(target=srv2.serve_forever, daemon=True).start()
b2 = f"http://127.0.0.1:{srv2.server_port}"
code, h = raw("/api/tv", server=b2)
assert code == 401 and h.get("Access-Control-Allow-Origin") == "*" and "WWW-Authenticate" not in h, (code, h)
assert raw("/api/status", server=b2)[1].get("WWW-Authenticate")    # the web UI still prompts
import base64 as b64
auth = {"Authorization": "Basic " + b64.b64encode(b"tv:pw").decode()}
assert raw("/api/tv", headers=auth, server=b2)[0] == 200
assert raw("/api/tv/request", "OPTIONS", server=b2)[0] == 204      # preflight needs no password
srv2.shutdown()
orig = requests.Session.request
def down(self, method, url, **kw):
    if "importlist" in url:
        raise requests.ConnectTimeout("timed out")
    return orig(self, method, url, **kw)
requests.Session.request = down
s._suggest = None
try:
    call("/api/tv/suggestions"); raise AssertionError("expected 502")
except urllib.error.HTTPError as e:
    err = json.loads(e.read())["error"]
    assert e.code == 502 and err == "watchlist-sync can't reach Radarr at http://localhost:7878", err
requests.Session.request = orig
print("tv tests OK")

call("/api/plex/logout", {})
assert call("/api/status")["account"] is None and not s.settings.all()["plex_token"]
print("recent:", [(x["title"], x["users"], x["result"]) for x in s.state.recent()])
print("ALL OK")
