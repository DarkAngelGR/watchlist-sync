#!/usr/bin/env python3
"""Orama installer: keeps the Orama app on a Samsung TV up to date.
(Part of Orama Server; still runs on its own too. Inside Orama Server it reads
INSTALLER_PORT / INSTALLER_DATA_DIR / INSTALLER_PASSWORD first.)

Watches the GitHub releases of Orama, and on request (from its web page, from
Orama itself, or automatically) downloads the newest .wgt and has
Apps2Samsung sign it with your TV's certificate and install it.

Standard library only. The signing and installing are done by Apps2Samsung's
command-line tool (apps2samsung-cli), which comes with the Docker image.
"""
import base64
import hmac
import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PRODUCT = "flexplex-installer"
VERSION = "1.0.1"


def env(name, default=""):
    return os.environ.get(name, default).strip()


CFG = {
    "tv_ip": env("TV_IP"),
    "tv_port": int(env("TV_SDB_PORT", "26101") or 26101),
    "repo": env("GITHUB_REPO", "DarkAngelGR/orama"),
    "github_api": env("GITHUB_API", "https://api.github.com").rstrip("/"),
    "github_token": env("GITHUB_TOKEN"),
    "check_hours": float(env("CHECK_HOURS", "6") or 6),
    "auto_install": env("AUTO_INSTALL", "false").lower() in ("1", "true", "yes", "on"),
    "port": int(env("INSTALLER_PORT") or env("PORT", "8790") or 8790),
    "password": env("INSTALLER_PASSWORD") or env("PASSWORD"),
    "data": env("INSTALLER_DATA_DIR") or env("DATA_DIR", "/data"),
    "a2s": env("A2S_BIN", "apps2samsung-cli"),
    # Lets Apps2Samsung continue past its pre-install warnings and, if the TV
    # only takes the update after removing the old copy (different
    # certificate), do that: Orama's settings and sign-in go with it.
    "allow_reinstall": env("ALLOW_REINSTALL", "false").lower() in ("1", "true", "yes", "on"),
}

STATE_FILE = os.path.join(CFG["data"], "state.json")
WORK = os.path.join(CFG["data"], "work")
A2S_DATA = os.path.join(CFG["data"], "apps2samsung")
LOG_MAX = 200


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_version(v):
    """'v0.8.10' -> (0, 8, 10). Non-numeric parts count as 0."""
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def newer(a, b):
    """Is version a newer than version b? An unknown b counts as older."""
    if not a:
        return False
    if not b:
        return True
    return parse_version(a) > parse_version(b)


class State:
    """Small persisted state: what was installed, what's latest, a short log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {"installed": "", "installed_at": "", "seen_on_tv": "", "seen_at": "",
                     "latest": None, "checked_at": "", "check_error": "",
                     "last": None, "log": []}
        self.busy = False
        try:
            with open(STATE_FILE) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(CFG["data"], exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, STATE_FILE)

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)
            self.save()

    def log(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        print(line, flush=True)
        with self.lock:
            self.data["log"] = (self.data.get("log") or [])[-(LOG_MAX - 1):] + [line]
            self.save()

    def installed(self):
        """Best guess of the version on the TV: what Orama last reported, or
        what this installer last put there, whichever is newer."""
        a, b = self.data.get("seen_on_tv"), self.data.get("installed")
        return a if newer(a, b) or not b else b

    def public(self):
        with self.lock:
            d = dict(self.data)
        latest = d.get("latest") or None
        installed = self.installed()
        return {
            "product": PRODUCT, "version": VERSION,
            "repo": CFG["repo"], "tv_ip": CFG["tv_ip"], "auto_install": CFG["auto_install"],
            "installed": installed, "installed_at": d.get("installed_at", ""),
            "latest": latest, "checked_at": d.get("checked_at", ""), "check_error": d.get("check_error", ""),
            "update_available": bool(latest and newer(latest.get("version"), installed)),
            "busy": self.busy, "last": d.get("last"), "log": (d.get("log") or [])[-30:],
        }


STATE = State()


# --- GitHub --------------------------------------------------------------------

class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """GitHub sends release downloads on to a storage host, which rejects the
    GitHub token; only send it to the host it was meant for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_opener = urllib.request.build_opener(_DropAuthOnRedirect)


def github(url, accept="application/vnd.github+json", timeout=20):
    headers = {"Accept": accept, "User-Agent": f"{PRODUCT}/{VERSION}"}
    if CFG["github_token"]:
        headers["Authorization"] = "Bearer " + CFG["github_token"]
    return _opener.open(urllib.request.Request(url, headers=headers), timeout=timeout)


def check_latest():
    """Newest published release with a .wgt attached, or raise."""
    url = f"{CFG['github_api']}/repos/{CFG['repo']}/releases/latest"
    try:
        with github(url) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            if not CFG["github_token"]:
                raise RuntimeError(f"no releases found in {CFG['repo']}; if the repo is private, set GITHUB_TOKEN")
            raise RuntimeError(f"no releases yet in {CFG['repo']} (or GITHUB_TOKEN can't read it)")
        if e.code == 401:
            raise RuntimeError("GitHub rejected GITHUB_TOKEN (expired or mistyped?)")
        if e.code == 403:
            raise RuntimeError("GitHub rate limit reached; try again later (or set GITHUB_TOKEN)")
        raise RuntimeError(f"GitHub answered {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"can't reach GitHub ({getattr(e, 'reason', e)})")
    asset = next((a for a in rel.get("assets", []) if a.get("name", "").endswith(".wgt")), None)
    if not asset:
        raise RuntimeError(f"release {rel.get('tag_name')} has no .wgt attached")
    latest = {
        "version": (rel.get("tag_name") or "").lstrip("vV"),
        "tag": rel.get("tag_name"),
        "name": rel.get("name") or rel.get("tag_name"),
        "notes": (rel.get("body") or "").strip()[:2000],
        "published": rel.get("published_at", ""),
        "url": rel.get("html_url", ""),
        # The API URL works for private repos (with the token) and public ones.
        "asset": asset.get("url") or asset.get("browser_download_url"),
        "asset_name": asset.get("name"),
        "size": asset.get("size", 0),
    }
    STATE.update(latest=latest, checked_at=now_iso(), check_error="")
    return latest


def safe_check():
    try:
        return check_latest()
    except Exception as e:  # noqa: BLE001 - reported to the user as-is
        STATE.update(checked_at=now_iso(), check_error=str(e))
        STATE.log(f"update check failed: {e}")
        return None


# --- Apps2Samsung ------------------------------------------------------------------
# The signing and installing are done by apps2samsung-cli (github.com/Apps2Samsung/
# Apps2Samsung), which reuses the certificate the Apps2Samsung desktop app made
# for your TV: copy its Certificate folder into DATA_DIR/apps2samsung/.

class InstallError(Exception):
    pass


def a2s(args, timeout=900):
    """Run apps2samsung-cli with --json; returns (exit code, JSON result, stderr)."""
    cmd = [CFG["a2s"]] + args + ["--json", "--no-browser", "--data-dir", A2S_DATA]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GITHUB_TOKEN=CFG["github_token"]))
    except FileNotFoundError:
        raise InstallError("apps2samsung-cli isn't installed (use the Docker image)")
    except subprocess.TimeoutExpired:
        raise InstallError(f"apps2samsung-cli {args[0]} timed out")
    out = (p.stdout or "").strip()
    result = {}
    try:  # one (indented) JSON document on stdout; progress goes to stderr
        result = json.loads(out[out.index("{"):]) if "{" in out else {}
    except ValueError:
        pass
    return p.returncode, result, (p.stderr or "")


def field(d, name, default=None):
    """The CLI's JSON may use camelCase or PascalCase."""
    if not isinstance(d, dict):
        return default
    return d.get(name, d.get(name[:1].upper() + name[1:], default))


def certificate_covers_tv():
    """Is there a certificate here for this TV? Making one needs a Samsung
    sign-in in a browser, which a server can't do, so it must be copied in."""
    code, res, err = a2s(["cert", "--tv", CFG["tv_ip"]], timeout=120)
    duid = field(res, "tvDuid")
    profiles = field(res, "profiles") or []
    if code != 0 or not duid:
        msg = field(res, "error") or err.strip().splitlines()[-1:] or ["no answer"]
        raise InstallError(f"can't reach the TV at {CFG['tv_ip']} ({msg if isinstance(msg, str) else msg[0]}). "
                           "Is it on, with Developer Mode set to this server's IP address?")
    for p in profiles:
        if field(p, "usable") and duid.lower() in [d.lower() for d in field(p, "duids") or []]:
            return True
    raise InstallError(f"no Apps2Samsung certificate here covers this TV (DUID {duid}). Copy the Certificate "
                       "folder from the Apps2Samsung app on your computer into data/apps2samsung/ "
                       "(see installer/README.md)")


def do_install(latest):
    version = latest["version"]
    if not CFG["tv_ip"]:
        raise InstallError("TV_IP isn't set")
    STATE.log(f"installing Orama {version}")
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    wgt = os.path.join(WORK, f"Orama-{version}.wgt")
    STATE.log(f"downloading {latest['asset_name']}")
    try:
        with github(latest["asset"], accept="application/octet-stream", timeout=120) as r, open(wgt, "wb") as f:
            shutil.copyfileobj(r, f)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise InstallError(f"download failed ({getattr(e, 'reason', e)})")
    try:
        with zipfile.ZipFile(wgt) as z:
            if "config.xml" not in z.namelist():
                raise InstallError("the release package has no config.xml")
    except zipfile.BadZipFile:
        raise InstallError("the release package isn't a valid .wgt")

    STATE.log(f"checking the TV at {CFG['tv_ip']} and the certificate")
    certificate_covers_tv()
    STATE.log("signing and installing (Apps2Samsung)")
    args = ["install", wgt, "--tv", CFG["tv_ip"]]
    if CFG["allow_reinstall"]:
        args.append("--yes")
    code, res, err = a2s(args)
    if code == 0 and field(res, "ok"):
        STATE.update(installed=version, installed_at=now_iso())
        STATE.log(f"Orama {version} installed" + (" and started" if field(res, "launched") else ""))
        return
    msg = field(res, "error") or (err.strip().splitlines() or [f"exit {code}"])[-1]
    checks = field(field(res, "details") or {}, "checks") or []
    if checks:
        msg += ": " + "; ".join(str(field(c, "message") or field(c, "defaultMessageWithDetail") or field(c, "kind")) for c in checks)
    if code == 3 and not CFG["allow_reinstall"]:
        msg = re.sub(r"\s*Rerun with --yes[^.]*\.?", "", msg) + " Set ALLOW_REINSTALL=true to let it go ahead."
    raise InstallError(msg)


def install_job(version=None):
    """Background job. Only one at a time."""
    try:
        latest = check_latest()  # always fresh: the asset URL must match the release
        if version and latest["version"] != version:
            raise InstallError(f"{version} isn't the latest release ({latest['version']} is)")
        do_install(latest)
        STATE.update(last={"ok": True, "version": latest["version"], "at": now_iso(),
                           "message": f"Orama {latest['version']} installed"})
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        STATE.log("install failed: " + msg)
        STATE.update(last={"ok": False, "version": version or "", "at": now_iso(), "message": msg})
    finally:
        STATE.busy = False


def start_install(version=None):
    with STATE.lock:
        if STATE.busy:
            return False
        STATE.busy = True
    threading.Thread(target=install_job, args=(version,), daemon=True).start()
    return True


def loop():
    """Check GitHub every CHECK_HOURS; install by itself when AUTO_INSTALL is on."""
    while True:
        latest = safe_check()
        if latest and CFG["auto_install"] and newer(latest["version"], STATE.installed()):
            STATE.log(f"Orama {latest['version']} is out; installing it (AUTO_INSTALL)")
            start_install(latest["version"])
        time.sleep(max(0.25, CFG["check_hours"]) * 3600)


# --- web -------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orama installer</title>
<style>
:root { --bg:#0B0D12; --raised:#181B23; --text:#FFF7E6; --dim:rgba(255,247,230,.62); --coral:#FF4C42; --ok:#5BD18B; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:16px/1.5 -apple-system, system-ui, sans-serif; }
main { max-width:560px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:26px; margin:8px 0 20px; }
.card { background:var(--raised); border-radius:14px; padding:16px; margin-bottom:14px; }
.row { display:flex; justify-content:space-between; gap:12px; padding:6px 0; }
.row span:first-child { color:var(--dim); }
button { width:100%; border:0; border-radius:12px; padding:15px; font:600 17px inherit; margin-top:10px;
  background:var(--coral); color:#fff; }
button.secondary { background:rgba(255,247,230,.12); color:var(--text); }
button:disabled { opacity:.5; }
.msg { margin-top:10px; min-height:1.5em; }
.ok { color:var(--ok); } .bad { color:var(--coral); }
pre { white-space:pre-wrap; font:12px/1.5 ui-monospace, monospace; color:var(--dim); margin:0; max-height:280px; overflow:auto; }
.notes { white-space:pre-wrap; color:var(--dim); font-size:14px; margin-top:8px; }
</style></head><body><main>
<h1>Orama installer</h1>
<div class="card">
  <div class="row"><span>On the TV</span><span id="installed">…</span></div>
  <div class="row"><span>Latest release</span><span id="latest">…</span></div>
  <div class="row"><span>TV</span><span id="tv">…</span></div>
  <div class="notes" id="notes"></div>
  <button id="install" disabled>Install</button>
  <button id="check" class="secondary">Check for updates</button>
  <div class="msg" id="msg"></div>
</div>
<div class="card"><pre id="log"></pre></div>
</main>
<script>
const $ = id => document.getElementById(id);
let busy = false;
async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('error ' + r.status));
  return j;
}
function show(s) {
  busy = s.busy;
  $('installed').textContent = s.installed || 'unknown';
  $('latest').textContent = s.latest ? s.latest.version : (s.check_error || '…');
  $('tv').textContent = s.tv_ip || 'TV_IP not set';
  $('notes').textContent = s.latest && s.update_available ? s.latest.notes : '';
  const b = $('install');
  b.disabled = s.busy || !s.latest;
  b.textContent = s.busy ? 'Installing…' : s.latest ? (s.update_available ? 'Install ' + s.latest.version : 'Reinstall ' + s.latest.version) : 'Install';
  const m = $('msg');
  if (s.busy) { m.className = 'msg'; m.textContent = 'Working… the app on the TV will close and reopen.'; }
  else if (s.last) { m.className = 'msg ' + (s.last.ok ? 'ok' : 'bad'); m.textContent = s.last.message; }
  else if (s.check_error) { m.className = 'msg bad'; m.textContent = s.check_error; }
  else m.textContent = '';
  $('log').textContent = (s.log || []).slice().reverse().join('\\n');
}
async function refresh() { try { show(await api('api/status')); } catch (e) { $('msg').textContent = e.message; } }
$('install').onclick = async () => {
  try { await api('api/install', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); } catch (e) { $('msg').textContent = e.message; }
  refresh();
};
$('check').onclick = async () => {
  $('msg').textContent = 'Checking…';
  try { show(await api('api/check', { method: 'POST' })); } catch (e) { $('msg').textContent = e.message; }
};
refresh();
setInterval(refresh, 3000);
</script></body></html>
"""


# --- request origin --------------------------------------------------------------
# Browsers let any website send requests to addresses on your home network, so
# commands are only accepted from this page, the TV app (file://) or another
# device on your network, and only for local host names (a public domain
# pointing at this server is how "DNS rebinding" works). ALLOWED_HOSTS adds names.
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".ts.net")


def _extra_hosts():
    return {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}


def _is_ip(host):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def host_allowed(host_header):
    host = (urlparse("//" + (host_header or "")).hostname or "").lower()
    return bool(host) and (bool(_is_ip(host)) or host == "localhost" or "." not in host
                           or host.endswith(LOCAL_SUFFIXES) or host in _extra_hosts())


def origin_allowed(origin, host_header):
    if not origin or origin == "null":
        return True
    u = urlparse(origin)
    if u.scheme not in ("http", "https"):
        return True                      # the TV app runs from file://
    host = (u.hostname or "").lower()
    if host == (urlparse("//" + (host_header or "")).hostname or "").lower():
        return True
    ip = _is_ip(host)
    if ip:
        return ip.is_private or ip.is_loopback or ip.is_link_local
    return host == "localhost" or "." not in host or host.endswith(LOCAL_SUFFIXES) or host in _extra_hosts()


class Handler(BaseHTTPRequestHandler):
    server_version = f"{PRODUCT}/{VERSION}"

    def log_message(self, fmt, *args):  # quiet access log
        pass

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin_allowed(origin, self.headers.get("Host")):  # the TV app runs from file://
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if not ctype.endswith("json") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        pw = CFG["password"]
        if not pw:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(h[6:]).decode().partition(":")
                if hmac.compare_digest(given.encode(), pw.encode()):
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
        # The browser page gets a login prompt; API calls (Orama) a plain 401.
        self.send_response(401)
        if not urlparse(self.path).path.startswith("/api/"):
            self.send_header("WWW-Authenticate", 'Basic realm="Orama installer"')
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(b'{"error": "password required"}')
        return False

    def _guard(self, command=False):
        if not host_allowed(self.headers.get("Host")):
            self._send(403, {"error": "unknown host name: open this by its IP address, or add the name to ALLOWED_HOSTS"})
            return False
        if command and not origin_allowed(self.headers.get("Origin"), self.headers.get("Host")):
            self._send(403, {"error": "request from another website refused"})
            return False
        return True

    def do_OPTIONS(self):
        if not self._guard(command=True):
            return
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            return self._send(200, {"ok": True})
        if not self._guard():
            return
        if not self._authed():
            return
        if url.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html")
        if url.path == "/api/status":
            reported = (parse_qs(url.query).get("installed") or [""])[0]
            if re.fullmatch(r"\d+(\.\d+){0,3}", reported or ""):
                if reported != STATE.data.get("seen_on_tv"):
                    STATE.update(seen_on_tv=reported, seen_at=now_iso())
            return self._send(200, STATE.public())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if not self._guard(command=True):
            return
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(min(length, 10000)) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad JSON"})
        if url.path == "/api/check":
            safe_check()
            return self._send(200, STATE.public())
        if url.path == "/api/install":
            version = body.get("version") if isinstance(body, dict) else None
            if version is not None and not re.fullmatch(r"\d+(\.\d+){0,3}", str(version)):
                return self._send(400, {"error": "bad version"})
            if not start_install(version):
                return self._send(409, {"error": "an install is already running"})
            return self._send(202, {"started": True})
        return self._send(404, {"error": "not found"})


def main():
    os.makedirs(CFG["data"], exist_ok=True)
    STATE.busy = False
    if not CFG["tv_ip"]:
        STATE.log("TV_IP isn't set: updates can be checked but not installed")
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), Handler)
    STATE.log(f"Orama installer listening on port {CFG['port']} (repo {CFG['repo']}, TV {CFG['tv_ip'] or '?'})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
