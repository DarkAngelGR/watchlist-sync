"""
mediadash — a small live dashboard for the mediastack.

Polls every service's API server-side (so API keys never reach the browser),
reads host CPU / RAM / disk / network / temps, reads container status and
per-container CPU / RAM from the Docker socket, and serves one page.
"""
import asyncio
import os
import socket
import time
from pathlib import Path

import httpx
import psutil
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

import updates

CONFIG_PATH = Path(os.environ.get("MEDIADASH_CONFIG", "/config/config.yaml"))
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="mediadash", docs_url=None, redoc_url=None)

STATE = {"services": {}, "system": {}, "containers": {}, "updated": 0}
CFG: dict = {}


# ───────────────────────── helpers ─────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("title", "Orama Server")
    cfg.setdefault("refresh_seconds", 5)
    cfg.setdefault("docker_socket", "/var/run/docker.sock")
    cfg.setdefault("disks", [])
    cfg.setdefault("services", [])
    for s in cfg["services"]:
        s.setdefault("id", s["name"].lower().replace(" ", "-"))
        s.setdefault("type", "generic")
        s.setdefault("group", "Other")
        if s.get("url"):
            s["url"] = s["url"].rstrip("/")
        s.setdefault("link", s.get("url", ""))
    return cfg


def fmt_bytes(n, per_sec=False):
    if n is None:
        return "—"
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if abs(n) < 1024 or unit == "PB":
            s = f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            return s + ("/s" if per_sec else "")
        n /= 1024


def fmt_eta(seconds):
    if seconds is None or seconds < 0 or seconds >= 8640000:
        return "∞"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def hms_to_seconds(t):
    """Sonarr/Radarr '01:02:03' or '1.02:03:04' → seconds."""
    if not t:
        return None
    try:
        days = 0
        if "." in t.split(":")[0]:
            d, t = t.split(".", 1)
            days = int(d)
        parts = [int(float(p)) for p in t.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts
        return days * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return None


def result(status="ok", headline="", stats=None, items=None, note=None):
    return {
        "status": status,           # ok | warn | error | idle
        "headline": headline,
        "stats": stats or [],       # [{label, value}]
        "items": items or [],       # [{title, sub, progress (0-100|None), tag}]
        "note": note,
    }


_slow_cache: dict = {}


async def cached(key, ttl, coro_fn):
    """Cache rarely-changing lookups (library sizes, etc.)."""
    hit = _slow_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await coro_fn()
    _slow_cache[key] = (time.time(), val)
    return val


# ───────────────────────── collectors ─────────────────────────

async def c_plex(s, http):
    h = {"X-Plex-Token": s.get("token", ""), "Accept": "application/json"}
    r = await http.get(f"{s['url']}/status/sessions", headers=h)
    r.raise_for_status()
    mc = r.json().get("MediaContainer", {})
    sessions = mc.get("Metadata", []) or []
    items, transcodes = [], 0
    for m in sessions:
        if m.get("type") == "episode":
            title = f"{m.get('grandparentTitle','')} · S{m.get('parentIndex',0):02d}E{m.get('index',0):02d}"
            sub_title = m.get("title", "")
        elif m.get("type") == "track":
            title = f"{m.get('grandparentTitle','')} — {m.get('title','')}"
            sub_title = ""
        else:
            title = m.get("title", "?")
            sub_title = str(m.get("year", "")) if m.get("year") else ""
        ts = m.get("TranscodeSession")
        if ts and ts.get("videoDecision") == "transcode":
            transcodes += 1
            hw = " (HW)" if ts.get("transcodeHwEncoding") or ts.get("transcodeHwRequested") else ""
            mode = f"Transcode{hw}"
        elif ts and (ts.get("videoDecision") == "copy" or ts.get("audioDecision") == "transcode"):
            mode = "Direct Stream"
        else:
            mode = "Direct Play"
        player = m.get("Player", {})
        user = m.get("User", {}).get("title", "?")
        state = player.get("state", "")
        dur, off = m.get("duration") or 0, m.get("viewOffset") or 0
        bits = [user, player.get("title") or player.get("product", "")]
        if sub_title:
            bits.insert(0, sub_title)
        items.append({
            "title": title,
            "sub": " · ".join(b for b in bits if b),
            "progress": round(off / dur * 100, 1) if dur else None,
            "tag": "Paused" if state == "paused" else mode,
        })
    n = len(sessions)
    if n == 0:
        return result("idle", "Nobody watching")
    head = f"{n} stream{'s' if n != 1 else ''}"
    if transcodes:
        head += f" · {transcodes} transcoding"
    return result("ok", head, items=items)


async def c_jellyfin(s, http):
    h = {"Authorization": f'MediaBrowser Token="{s.get("api_key","")}"'}
    r = await http.get(f"{s['url']}/Sessions", params={"activeWithinSeconds": 960}, headers=h)
    r.raise_for_status()
    playing = [x for x in r.json() if x.get("NowPlayingItem")]
    items, transcodes = [], 0
    for x in playing:
        it, ps = x["NowPlayingItem"], x.get("PlayState", {})
        if it.get("Type") == "Episode":
            title = f"{it.get('SeriesName','')} · S{it.get('ParentIndexNumber',0):02d}E{it.get('IndexNumber',0):02d}"
            extra = it.get("Name", "")
        else:
            title = it.get("Name", "?")
            extra = str(it.get("ProductionYear", "")) if it.get("ProductionYear") else ""
        method = ps.get("PlayMethod", "DirectPlay")
        if method == "Transcode":
            transcodes += 1
            hw = (x.get("TranscodingInfo") or {}).get("HardwareAccelerationType")
            tag = f"Transcode{' (HW)' if hw and hw != 'none' else ''}"
        else:
            tag = {"DirectPlay": "Direct Play", "DirectStream": "Direct Stream"}.get(method, method)
        rt, pos = it.get("RunTimeTicks") or 0, ps.get("PositionTicks") or 0
        bits = [extra, x.get("UserName", "?"), x.get("DeviceName") or x.get("Client", "")]
        items.append({
            "title": title,
            "sub": " · ".join(b for b in bits if b),
            "progress": round(pos / rt * 100, 1) if rt else None,
            "tag": "Paused" if ps.get("IsPaused") else tag,
        })
    n = len(playing)
    if n == 0:
        return result("idle", "Nobody watching")
    head = f"{n} stream{'s' if n != 1 else ''}"
    if transcodes:
        head += f" · {transcodes} transcoding"
    return result("ok", head, items=items)


async def _arr_common(s, http, api="v3"):
    h = {"X-Api-Key": s.get("api_key", "")}
    base = f"{s['url']}/api/{api}"
    health_r = await http.get(f"{base}/health", headers=h)
    health_r.raise_for_status()
    health = [x for x in health_r.json() if x.get("type") in ("warning", "error")]
    return h, base, health


def _queue_item(rec, title):
    size, left = rec.get("size") or 0, rec.get("sizeleft") or 0
    prog = round((size - left) / size * 100, 1) if size else None
    state = rec.get("trackedDownloadState") or rec.get("status") or ""
    tag = {
        "downloading": "Downloading", "importPending": "Import pending",
        "importing": "Importing", "failedPending": "Failed",
        "importBlocked": "Import blocked",
    }.get(state, state.capitalize() if state else "")
    if rec.get("status") in ("paused", "queued") and state == "downloading":
        tag = rec["status"].capitalize()
    eta = hms_to_seconds(rec.get("timeleft"))
    sub = [rec.get("downloadClient", ""), f"{fmt_bytes(size)}"]
    if eta and tag == "Downloading":
        sub.append(f"ETA {fmt_eta(eta)}")
    return {"title": title, "sub": " · ".join(x for x in sub if x), "progress": prog, "tag": tag,
            "warn": rec.get("trackedDownloadStatus") in ("warning", "error")}


async def c_sonarr(s, http):
    h, base, health = await _arr_common(s, http)
    q = (await http.get(f"{base}/queue", headers=h,
                        params={"pageSize": 50, "includeSeries": "true", "includeEpisode": "true"})).json()
    recs = q.get("records", [])
    items = []
    for rec in recs:
        se, ep = rec.get("series") or {}, rec.get("episode") or {}
        t = se.get("title") or rec.get("title", "?")
        if ep:
            t += f" · S{ep.get('seasonNumber',0):02d}E{ep.get('episodeNumber',0):02d}"
        items.append(_queue_item(rec, t))
    missing = (await http.get(f"{base}/wanted/missing", headers=h, params={"pageSize": 1})).json().get("totalRecords", 0)

    async def series_count():
        r = await http.get(f"{base}/series", headers=h)
        return len(r.json())
    shows = await cached(f"{s['id']}-series", 600, series_count)

    total = q.get("totalRecords", len(recs))
    stats = [{"label": "Queue", "value": total}, {"label": "Missing", "value": missing},
             {"label": "Series", "value": shows}]
    status = "warn" if health or any(i.get("warn") for i in items) else ("ok" if total else "idle")
    head = f"{total} in queue" if total else "Queue empty"
    note = health[0]["message"] if health else None
    return result(status, head, stats, items, note)


async def c_radarr(s, http):
    h, base, health = await _arr_common(s, http)
    q = (await http.get(f"{base}/queue", headers=h,
                        params={"pageSize": 50, "includeMovie": "true"})).json()
    recs = q.get("records", [])
    items = []
    for rec in recs:
        mv = rec.get("movie") or {}
        t = mv.get("title") or rec.get("title", "?")
        if mv.get("year"):
            t += f" ({mv['year']})"
        items.append(_queue_item(rec, t))

    async def movie_counts():
        r = await http.get(f"{base}/movie", headers=h)
        movies = r.json()
        miss = sum(1 for m in movies if m.get("monitored") and not m.get("hasFile") and m.get("isAvailable"))
        return len(movies), miss
    total_movies, missing = await cached(f"{s['id']}-movies", 300, movie_counts)

    total = q.get("totalRecords", len(recs))
    stats = [{"label": "Queue", "value": total}, {"label": "Missing", "value": missing},
             {"label": "Movies", "value": total_movies}]
    status = "warn" if health or any(i.get("warn") for i in items) else ("ok" if total else "idle")
    head = f"{total} in queue" if total else "Queue empty"
    note = health[0]["message"] if health else None
    return result(status, head, stats, items, note)


async def c_prowlarr(s, http):
    h, base, health = await _arr_common(s, http, api="v1")
    idx = (await http.get(f"{base}/indexer", headers=h)).json()
    st = (await http.get(f"{base}/indexerstatus", headers=h)).json()
    enabled = [i for i in idx if i.get("enable")]
    names = {i["id"]: i.get("name", "?") for i in idx}
    failing = [x for x in st if x.get("disabledTill")]
    items = [{"title": names.get(f.get("indexerId"), "?"), "sub": "Disabled after failures",
              "progress": None, "tag": "Failing", "warn": True} for f in failing]
    stats = [{"label": "Enabled", "value": len(enabled)}, {"label": "Failing", "value": len(failing)}]
    status = "warn" if failing or health else "ok"
    head = f"{len(enabled)} indexers" + (f" · {len(failing)} failing" if failing else " · all healthy")
    return result(status, head, stats, items, health[0]["message"] if health else None)


async def c_bazarr(s, http):
    h = {"X-API-KEY": s.get("api_key", "")}
    r = await http.get(f"{s['url']}/api/badges", headers=h)
    r.raise_for_status()
    b = r.json()
    ep, mv = b.get("episodes", 0), b.get("movies", 0)
    prov, issues = b.get("providers", 0), b.get("status", 0)
    stats = [{"label": "Wanted ep.", "value": ep}, {"label": "Wanted movies", "value": mv},
             {"label": "Throttled", "value": prov}]
    status = "warn" if prov or issues else ("ok" if ep or mv else "idle")
    head = f"{ep + mv} subtitles wanted" if (ep or mv) else "Nothing wanted"
    note = f"{prov} provider(s) throttled" if prov else (f"{issues} health issue(s)" if issues else None)
    return result(status, head, stats, note=note)


async def c_sabnzbd(s, http):
    r = await http.get(f"{s['url']}/api", params={"mode": "queue", "output": "json",
                                                 "apikey": s.get("api_key", ""), "limit": 10})
    r.raise_for_status()
    q = r.json().get("queue", {})
    slots = q.get("slots", [])
    items = []
    for sl in slots:
        try:
            prog = float(sl.get("percentage", 0))
        except ValueError:
            prog = None
        sub = [sl.get("cat", ""), f"{float(sl.get('mb', 0)) / 1024:.1f} GB"]
        if sl.get("timeleft") and sl.get("status") == "Downloading":
            sub.append(f"ETA {sl['timeleft']}")
        items.append({"title": sl.get("filename", "?"), "sub": " · ".join(x for x in sub if x and x != "*"),
                      "progress": prog, "tag": sl.get("status", "")})
    speed = float(q.get("kbpersec", 0) or 0) * 1024
    paused = q.get("paused")
    n = int(q.get("noofslots_total", q.get("noofslots", len(slots))) or 0)
    stats = [{"label": "Speed", "value": fmt_bytes(speed, True)},
             {"label": "Left", "value": f"{float(q.get('mbleft', 0) or 0) / 1024:.1f} GB"},
             {"label": "Free disk", "value": f"{q.get('diskspace1', '?')} GB"}]
    if paused:
        return result("warn", f"Paused · {n} queued", stats, items)
    if n == 0:
        return result("idle", "Idle", stats)
    return result("ok", f"{fmt_bytes(speed, True)} · {n} in queue · {q.get('timeleft','')}", stats, items)


_qbit_clients: dict = {}


async def c_qbittorrent(s, http):
    # qBittorrent needs a session cookie, so it keeps its own client.
    cl = _qbit_clients.get(s["id"])
    if cl is None:
        cl = httpx.AsyncClient(timeout=6, headers={"Referer": s["url"]})
        _qbit_clients[s["id"]] = cl

    async def login():
        if s.get("username"):
            r = await cl.post(f"{s['url']}/api/v2/auth/login",
                              data={"username": s["username"], "password": s.get("password", "")})
            if r.text.strip() != "Ok.":
                raise RuntimeError("qBittorrent login failed (check username/password)")

    async def get(path, **params):
        r = await cl.get(f"{s['url']}{path}", params=params)
        if r.status_code == 403:
            await login()
            r = await cl.get(f"{s['url']}{path}", params=params)
        r.raise_for_status()
        return r.json()

    info = await get("/api/v2/transfer/info")
    active = await get("/api/v2/torrents/info", filter="active", sort="dlspeed", reverse="true")
    downloading = await get("/api/v2/torrents/info", filter="downloading")
    items = []
    for t in active[:8]:
        st = t.get("state", "")
        if t.get("dlspeed", 0) > 0 or st in ("downloading", "forcedDL", "metaDL"):
            sub = [t.get("category", ""), f"↓ {fmt_bytes(t.get('dlspeed', 0), True)}", f"ETA {fmt_eta(t.get('eta'))}"]
            tag = "Downloading"
        else:
            sub = [t.get("category", ""), f"↑ {fmt_bytes(t.get('upspeed', 0), True)}", f"ratio {t.get('ratio', 0):.2f}"]
            tag = "Seeding"
        items.append({"title": t.get("name", "?"), "sub": " · ".join(x for x in sub if x),
                      "progress": round(t.get("progress", 0) * 100, 1), "tag": tag})
    dl, ul = info.get("dl_info_speed", 0), info.get("up_info_speed", 0)
    conn = info.get("connection_status", "")
    stats = [{"label": "Down", "value": fmt_bytes(dl, True)}, {"label": "Up", "value": fmt_bytes(ul, True)},
             {"label": "Downloading", "value": len(downloading)}]
    status = "warn" if conn == "disconnected" else ("ok" if active else "idle")
    head = f"↓ {fmt_bytes(dl, True)} · ↑ {fmt_bytes(ul, True)}"
    note = "No connection (VPN down?)" if conn == "disconnected" else ("Firewalled — port not open" if conn == "firewalled" else None)
    return result(status, head, stats, items, note)


async def c_tdarr(s, http):
    h = {"x-api-key": s["api_key"]} if s.get("api_key") else {}
    r = await http.post(f"{s['url']}/api/v2/cruddb", headers=h, json={
        "data": {"collection": "StatisticsJSONDB", "mode": "getById", "docID": "statistics"}})
    r.raise_for_status()
    st = r.json() or {}
    queue = int(st.get("table1Count", 0) or 0) + int(st.get("table4Count", 0) or 0)
    done = int(st.get("table2Count", 0) or 0) + int(st.get("table5Count", 0) or 0)
    errors = int(st.get("table3Count", 0) or 0) + int(st.get("table6Count", 0) or 0)
    saved = float(st.get("sizeDiff", 0) or 0)

    items = []
    try:
        nodes = (await http.get(f"{s['url']}/api/v2/get-nodes", headers=h)).json() or {}
        for node in nodes.values():
            for w in (node.get("workers") or {}).values():
                f = w.get("file") or w.get("_id") or "?"
                name = str(f).replace("\\", "/").split("/")[-1]
                sub = [node.get("nodeName", ""), (w.get("workerType") or "").replace("transcode", "transcode ").strip()]
                if w.get("fps"):
                    sub.append(f"{w['fps']} fps")
                if w.get("ETA"):
                    sub.append(f"ETA {w['ETA']}")
                try:
                    prog = float(w.get("percentage", 0))
                except (TypeError, ValueError):
                    prog = None
                items.append({"title": name, "sub": " · ".join(x for x in sub if x),
                              "progress": prog, "tag": w.get("status") or "Working"})
    except Exception:
        pass

    stats = [{"label": "Queued", "value": queue}, {"label": "Processed", "value": done},
             {"label": "Errors", "value": errors}, {"label": "Saved", "value": f"{saved:.1f} GB"}]
    status = "warn" if errors else ("ok" if items else "idle")
    head = f"{len(items)} worker{'s' if len(items) != 1 else ''} busy · {queue} queued" if items else f"Idle · {queue} queued"
    return result(status, head, stats, items)


async def c_seerr(s, http):
    h = {"X-Api-Key": s.get("api_key", "")}
    r = await http.get(f"{s['url']}/api/v1/request/count", headers=h)
    r.raise_for_status()
    c = r.json()
    issues = 0
    try:
        issues = (await http.get(f"{s['url']}/api/v1/issue/count", headers=h)).json().get("open", 0)
    except Exception:
        pass
    pending = c.get("pending", 0)
    stats = [{"label": "Pending", "value": pending}, {"label": "Processing", "value": c.get("processing", 0)},
             {"label": "Available", "value": c.get("available", 0)}, {"label": "Open issues", "value": issues}]
    status = "warn" if pending or issues else "ok"
    head = f"{pending} request{'s' if pending != 1 else ''} waiting approval" if pending else f"{c.get('total', 0)} requests total"
    return result(status, head, stats)


async def c_gluetun(s, http):
    h = {}
    if s.get("api_key"):
        h["X-API-Key"] = s["api_key"]
    auth = (s["username"], s.get("password", "")) if s.get("username") else None

    async def get(*paths):
        last = None
        for p in paths:
            r = await http.get(f"{s['url']}{p}", headers=h, auth=auth)
            if r.status_code == 200:
                return r.json()
            last = r
        last.raise_for_status()
        return {}

    ip = await get("/v1/publicip/ip")
    vpn = await get("/v1/vpn/status", "/v1/openvpn/status")
    try:
        port = (await get("/v1/portforward", "/v1/openvpn/portforwarded")).get("port")
    except Exception:
        port = None
    running = vpn.get("status") == "running"
    where = ", ".join(x for x in [ip.get("city"), ip.get("country")] if x)
    stats = [{"label": "Public IP", "value": ip.get("public_ip", "?")},
             {"label": "Location", "value": where or "?"},
             {"label": "Forwarded port", "value": port or "—"}]
    return result("ok" if running else "error", f"VPN {'connected' if running else vpn.get('status','down')} · {where}", stats)


async def c_generic(s, http):
    if s.get("url") and s.get("check_http", True):
        r = await http.get(s["url"])
        if r.status_code >= 500:
            return result("error", f"HTTP {r.status_code}")
        return result("ok", s.get("description", "Responding"))
    return result("ok", s.get("description", ""))


COLLECTORS = {
    "plex": c_plex, "jellyfin": c_jellyfin, "sonarr": c_sonarr, "radarr": c_radarr,
    "prowlarr": c_prowlarr, "bazarr": c_bazarr, "sabnzbd": c_sabnzbd,
    "qbittorrent": c_qbittorrent, "tdarr": c_tdarr, "seerr": c_seerr,
    "overseerr": c_seerr, "jellyseerr": c_seerr, "gluetun": c_gluetun, "generic": c_generic,
}


# ───────────────────────── docker ─────────────────────────

async def docker_loop():
    sock = CFG["docker_socket"]
    if not os.path.exists(sock):
        return
    transport = httpx.AsyncHTTPTransport(uds=sock)
    async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=15) as d:
        while True:
            try:
                lst = (await d.get("/containers/json", params={"all": "true"})).json()
                out = {}
                for c in lst:
                    name = c["Names"][0].lstrip("/")
                    out[name] = {"state": c.get("State"), "status": c.get("Status"),
                                 "image": c.get("Image"), "cpu": None, "mem": None}

                async def stats(name, cid):
                    try:
                        st = (await d.get(f"/containers/{cid}/stats", params={"stream": "false"})).json()
                        cpu_d = st["cpu_stats"]["cpu_usage"]["total_usage"] - st["precpu_stats"]["cpu_usage"]["total_usage"]
                        sys_d = st["cpu_stats"].get("system_cpu_usage", 0) - st["precpu_stats"].get("system_cpu_usage", 0)
                        ncpu = st["cpu_stats"].get("online_cpus") or len(st["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1])
                        out[name]["cpu"] = round(cpu_d / sys_d * ncpu * 100, 1) if sys_d > 0 else 0.0
                        mem = st.get("memory_stats", {})
                        cache = (mem.get("stats") or {}).get("inactive_file", (mem.get("stats") or {}).get("cache", 0))
                        out[name]["mem"] = max(0, mem.get("usage", 0) - cache)
                    except Exception:
                        pass

                await asyncio.gather(*[stats(c["Names"][0].lstrip("/"), c["Id"])
                                       for c in lst if c.get("State") == "running"])
                STATE["containers"] = out
            except Exception as e:
                STATE["containers"] = {"_error": str(e)}
            await asyncio.sleep(max(10, CFG["refresh_seconds"] * 2))


# ───────────────────────── system ─────────────────────────

_prev = {}


def read_system():
    now = time.time()
    net = psutil.net_io_counters()
    dio = psutil.disk_io_counters()
    rates = {}
    if _prev:
        dt = now - _prev["t"]
        rates = {
            "net_rx": (net.bytes_recv - _prev["rx"]) / dt, "net_tx": (net.bytes_sent - _prev["tx"]) / dt,
            "disk_r": ((dio.read_bytes - _prev["dr"]) / dt) if dio else 0,
            "disk_w": ((dio.write_bytes - _prev["dw"]) / dt) if dio else 0,
        }
    _prev.update(t=now, rx=net.bytes_recv, tx=net.bytes_sent,
                 dr=dio.read_bytes if dio else 0, dw=dio.write_bytes if dio else 0)

    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    disks = []
    for d in CFG["disks"]:
        try:
            u = psutil.disk_usage(d["path"])
            disks.append({"name": d.get("name", d["path"]), "total": u.total, "used": u.used, "percent": u.percent})
        except Exception:
            disks.append({"name": d.get("name", d["path"]), "error": "not mounted in container"})

    temp = None
    try:
        sensors = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
            if key in sensors and sensors[key]:
                temp = max(x.current for x in sensors[key])
                break
        if temp is None and sensors:
            temp = max(x.current for v in sensors.values() for x in v)
    except Exception:
        pass

    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)

    return {
        "host": CFG.get("hostname") or socket.gethostname(),
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_percent(interval=None, percpu=True),
        "cpu_count": psutil.cpu_count(),
        "load": [round(x, 2) for x in load],
        "mem_total": vm.total, "mem_used": vm.total - vm.available, "mem_percent": vm.percent,
        "swap_percent": sw.percent,
        "temp": round(temp, 1) if temp is not None else None,
        "uptime": int(now - psutil.boot_time()),
        "disks": disks,
        **{k: round(v) for k, v in rates.items()},
    }


async def system_loop():
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    while True:
        try:
            STATE["system"] = read_system()
        except Exception as e:
            STATE["system"] = {"error": str(e)}
        await asyncio.sleep(2)


# ───────────────────────── services ─────────────────────────

async def poll_service(s, http):
    fn = COLLECTORS.get(s["type"], c_generic)
    try:
        res = await asyncio.wait_for(fn(s, http), timeout=8)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        msg = "Bad API key / token" if code in (401, 403) else f"HTTP {code}"
        res = result("error", msg)
    except (httpx.ConnectError, httpx.ConnectTimeout, OSError):
        res = result("error", "Not reachable")
    except asyncio.TimeoutError:
        res = result("error", "Timed out")
    except Exception as e:
        res = result("error", f"{type(e).__name__}: {e}"[:120])
    res["checked"] = time.time()
    STATE["services"][s["id"]] = res


async def services_loop():
    async with httpx.AsyncClient(timeout=6, verify=False, follow_redirects=True) as http:
        while True:
            await asyncio.gather(*[poll_service(s, http) for s in CFG["services"]])
            STATE["updated"] = time.time()
            await asyncio.sleep(CFG["refresh_seconds"])


# ───────────────────────── app ─────────────────────────

@app.on_event("startup")
async def startup():
    global CFG
    CFG = load_config()
    asyncio.create_task(system_loop())
    asyncio.create_task(services_loop())
    asyncio.create_task(docker_loop())
    updates.configure(CFG)
    asyncio.create_task(updates.worker())
    asyncio.create_task(updates.check_loop())


@app.get("/api/status")
async def status():
    containers = STATE["containers"]
    services = []
    for s in CFG["services"]:
        res = STATE["services"].get(s["id"]) or result("idle", "Checking…")
        cname = s.get("container")
        c = containers.get(cname) if cname else None
        entry = {
            "id": s["id"], "name": s["name"], "type": s["type"], "group": s["group"],
            "link": s.get("link") or "", "description": s.get("description", ""),
            "container": c, "container_name": cname, **res,
        }
        u = updates.STATE["items"].get(cname) if cname else None
        entry["update"] = (u["job"] if u and u["job"] in ("queued", "updating") else u["state"]) if u else None
        if cname and c and c.get("state") != "running":
            entry["status"] = "down"
            entry["headline"] = f"Container {c.get('state')}"
            entry["items"] = []
        elif cname and not c and containers and "_error" not in containers:
            entry["status"] = "down"
            entry["headline"] = "Container not found"
        services.append(entry)
    return JSONResponse({
        "title": CFG["title"],
        "refresh": CFG["refresh_seconds"],
        "system": STATE["system"],
        "services": services,
        "docker_ok": bool(containers) and "_error" not in containers,
        "updated": STATE["updated"],
        "orama": orama_summary(),
    })


def orama_summary():
    """Inside Orama Server: the converter's state, straight from the sync part
    running in this same process (None when the dashboard runs on its own)."""
    import sys
    sync = sys.modules.get("app")
    syncer = getattr(sync, "SYNCER", None) if sync else None
    if not syncer:
        return None
    try:
        c = syncer.converter.status()
    except Exception:
        return None
    cur = c.get("current")
    return {"enabled": c["enabled"], "in_hours": c["in_hours"], "queued": c["counts"].get("queued", 0),
            "failed": c["counts"].get("failed", 0), "done": c["counts"].get("done", 0),
            "current": {"name": cur["name"], "step": cur.get("step"), "progress": cur.get("progress")} if cur else None}


# ───────────────────────── updates ─────────────────────────

def _denied(request: Request):
    if not updates.password():
        return JSONResponse({"error": "Updates are off: set MEDIADASH_PASSWORD (see README)"}, status_code=403)
    if not updates.check_password(request.headers.get("X-Password")):
        return JSONResponse({"error": "Wrong password"}, status_code=401)
    return None


@app.get("/api/updates")
async def updates_status():
    return JSONResponse(updates.public())


@app.post("/api/updates/check")
async def updates_check(request: Request):
    if (bad := _denied(request)):
        return bad
    asyncio.create_task(updates.check())
    return JSONResponse({"ok": True})


@app.post("/api/updates/run")
async def updates_run(request: Request):
    if (bad := _denied(request)):
        return bad
    try:
        body = await request.json()
    except Exception:
        body = {}
    names = body.get("containers")
    if names == "all":
        names = [i["name"] for i in updates.STATE["items"].values() if i["state"] == "available"]
    if not isinstance(names, list) or not names:
        return JSONResponse({"error": "Say which containers to update"}, status_code=400)
    return JSONResponse({"queued": updates.enqueue([str(n) for n in names])})


@app.get("/")
async def index():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse('<!doctype html><html lang="en"><meta charset="utf-8">\n' + page)
