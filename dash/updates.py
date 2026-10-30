"""
Container updates for mediadash.

Checking: for every container, ask the registry (through the Docker engine,
nothing is downloaded) for the current digest of the image's tag, and compare
it with the digest of the image the container runs. A pinned tag
(e.g. plexinc/pms-docker:1.43.1) is compared with that same tag, so pins are
respected. Images built on the NAS (compose `build:`) have no registry and are
shown as "built here".

Updating: through docker compose, with the project, working folder and compose
files Docker recorded on the container, so every setting from your compose
file carries over:
    docker compose pull <service>
    docker compose up -d --no-deps <service>
Containers that share the updated container's network (qBittorrent on
Gluetun's) are recreated right after, or they'd lose their connection. Old
image layers nobody uses any more are removed afterwards.

One update runs at a time; the rest wait in a queue.
"""
import asyncio
import hmac
import json
import os
import time

import httpx

P = "com.docker.compose."
LOG_LINES = 40

STATE = {
    "items": {},        # container name -> item (see _item)
    "checked": 0,
    "checking": False,
    "check_error": None,
    "queue": [],        # container names waiting
    "running": None,    # container name being updated
    "log": [],          # [time, text] across all updates, newest last
}

_cfg: dict = {}
_lock = asyncio.Lock()
_wake = asyncio.Event()


def configure(cfg):
    global _cfg
    _cfg = cfg


def password():
    return os.environ.get("MEDIADASH_PASSWORD") or str(_cfg.get("update_password") or "")


def check_password(given):
    pw = password()
    return bool(pw) and hmac.compare_digest(str(given or ""), pw)


def _log(text):
    STATE["log"].append([time.time(), text])
    del STATE["log"][:-LOG_LINES]


def _docker():
    sock = _cfg.get("docker_socket", "/var/run/docker.sock")
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=sock), base_url="http://docker", timeout=60)


def _ref(image):
    """'lscr.io/linuxserver/sonarr' -> ('lscr.io/linuxserver/sonarr', 'latest')."""
    if "@" in image:
        return image, None                 # pinned to a digest: never changes
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, "latest"


def _item(c, old=None):
    labels = c.get("Labels") or {}
    it = dict(old or {})
    it.update({
        "name": c["Names"][0].lstrip("/"),
        "id": c["Id"],
        "image": c.get("Image", ""),
        "image_id": c.get("ImageID", ""),
        "running": c.get("State") == "running",
        "project": labels.get(P + "project", ""),
        "service": labels.get(P + "service", ""),
        "workdir": labels.get(P + "project.working_dir", ""),
        "files": [f for f in (labels.get(P + "project.config_files") or "").split(",") if f],
        "network_mode": (c.get("HostConfig") or {}).get("NetworkMode", ""),
    })
    it.setdefault("state", "unknown")   # current | available | local | unknown | error
    it.setdefault("message", "")
    it.setdefault("job", None)          # None | queued | updating | updated | failed
    return it


async def _remote_digest(d, image):
    repo, tag = _ref(image)
    if tag is None:
        return None
    r = await d.get(f"/distribution/{repo}:{tag}/json")
    if r.status_code in (401, 403):
        raise RuntimeError("the registry wants a login")
    if r.status_code == 404:
        raise RuntimeError("tag not found on the registry")
    r.raise_for_status()
    return (r.json().get("Descriptor") or {}).get("digest")


async def check(only=None):
    """Refresh what every container runs and whether its image has an update."""
    if STATE["checking"]:
        return
    STATE["checking"] = True
    STATE["check_error"] = None
    try:
        async with _docker() as d:
            lst = (await d.get("/containers/json", params={"all": "true"})).json()
            items = {}
            for c in lst:
                name = c["Names"][0].lstrip("/")
                try:
                    # The list shows an image ID once the tag has moved on; the
                    # container's own config keeps the name it was created from.
                    c["Image"] = (await d.get(f"/containers/{c['Id']}/json")).json()["Config"]["Image"]
                except Exception:
                    pass
                items[name] = _item(c, STATE["items"].get(name))
            # Drop containers that no longer exist; keep job results.
            STATE["items"] = items
            images = {}
            for it in items.values():
                if only and it["name"] not in only:
                    continue
                images.setdefault((it["image"], it["image_id"]), []).append(it)

            async def one(key, group):
                image, image_id = key
                try:
                    info = (await d.get(f"/images/{image_id}/json")).json()
                    digests = [x.split("@", 1)[1] for x in info.get("RepoDigests") or [] if "@" in x]
                    tagged = await d.get(f"/images/{image}/json") if "@" not in image else None
                    tag_id = tagged.json().get("Id") if tagged is not None and tagged.status_code == 200 else None
                    if "@" in image:
                        state, msg = "current", "Pinned to an exact image"
                    elif tag_id and tag_id != image_id:
                        # A newer image was already downloaded (e.g. by the UGREEN app)
                        # but the container still runs the old one.
                        state, msg = "available", "New image already downloaded, needs a restart"
                    elif not digests:
                        state, msg = "local", "No registry fingerprint to compare"
                    else:
                        remote = await _remote_digest(d, image)
                        if remote and remote not in digests:
                            state, msg = "available", "A newer image is available"
                        else:
                            state, msg = "current", "Up to date"
                except Exception as e:
                    state, msg = "error", f"Couldn't check: {e}"[:160]
                for it in group:
                    it["state"], it["message"] = state, msg

            # Services your compose file builds from a folder (mediadash,
            # watchlist-sync...) have no registry to check.
            built = set()
            projects = {}
            for it in items.values():
                if it["project"] and it["files"]:
                    projects[(it["project"], it["workdir"], tuple(it["files"]))] = it
            for (proj, _, _), it in projects.items():
                try:
                    out = await _run(*_compose_args(it), "config", "--format", "json", step="Reading compose file", keep=0)
                    conf = json.loads("\n".join(out))
                    built |= {(proj, name) for name, svc in (conf.get("services") or {}).items() if svc.get("build")}
                except Exception:
                    pass
            for key, group in list(images.items()):
                if all((it["project"], it["service"]) in built for it in group):
                    for it in group:
                        it["state"], it["message"] = "local", "Built here, no registry to check"
                    del images[key]

            sem = asyncio.Semaphore(4)

            async def limited(k, g):
                async with sem:
                    await one(k, g)
            await asyncio.gather(*[limited(k, g) for k, g in images.items()])
        STATE["checked"] = time.time()
    except Exception as e:
        STATE["check_error"] = f"Couldn't reach Docker: {e}"[:200]
    finally:
        STATE["checking"] = False


def enqueue(names):
    added = []
    for n in names:
        it = STATE["items"].get(n)
        if not it or n in STATE["queue"] or STATE["running"] == n:
            continue
        if it["state"] == "local":
            continue
        it["job"], it["message"] = "queued", "Waiting…"
        STATE["queue"].append(n)
        added.append(n)
    _wake.set()
    return added


async def _run(*args, step="", keep=15):
    sock = _cfg.get("docker_socket", "/var/run/docker.sock")
    env = dict(os.environ, DOCKER_HOST=f"unix://{sock}", COMPOSE_ANSI="never", COMPOSE_PROGRESS="plain")
    proc = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    lines = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if line:
            lines.append(line)
            if keep:
                del lines[:-keep]
    code = await proc.wait()
    if code != 0:
        tail = " | ".join(lines[-3:]) or f"exit code {code}"
        raise RuntimeError(f"{step} failed: {tail}"[:300])
    return lines


def _compose_args(it):
    if not (it["project"] and it["service"] and it["workdir"] and it["files"]):
        raise RuntimeError("Not started with docker compose, so mediadash can't recreate it safely")
    missing = [f for f in it["files"] if not os.path.exists(f)]
    if missing:
        raise RuntimeError(f"Can't see {missing[0]}: mount {it['workdir']} into mediadash (see README)")
    args = ["compose", "-p", it["project"], "--project-directory", it["workdir"]]
    for f in it["files"]:
        args += ["-f", f]
    return args


async def _update(name):
    it = STATE["items"].get(name)
    if not it:
        return
    it["job"], it["message"] = "updating", "Downloading the new image…"
    _log(f"{name}: updating")
    followers = []
    try:
        base = _compose_args(it)
        # Who shares this container's network? They must follow it.
        async with _docker() as d:
            lst = (await d.get("/containers/json", params={"all": "true"})).json()
        followers = [_item(c) for c in lst
                     if (c.get("HostConfig") or {}).get("NetworkMode") in (f"container:{it['id']}", f"container:{name}")]
        await _run(*base, "pull", it["service"], step="Download")
        it["message"] = "Restarting with the new image…"
        if name == os.environ.get("SELF_CONTAINER", "orama-server"):
            # Updating ourselves: restarting this container would cut the update
            # off halfway, so a throwaway helper container does the restart.
            sock = _cfg.get("docker_socket", "/var/run/docker.sock")
            vols = ["-v", f"{sock}:/var/run/docker.sock", "-v", f"{it['workdir']}:{it['workdir']}"]
            for f in it["files"]:
                d = os.path.dirname(f)
                if not d.startswith(it["workdir"]):
                    vols += ["-v", f"{d}:{d}"]
            await _run("run", "--rm", "-d", "--name", "orama-server-updater", *vols, "-w", it["workdir"],
                       "docker:27-cli", *base, "up", "-d", "--no-deps", it["service"], step="Restart")
            it["job"], it["message"] = "updated", "Restarting with the new version…"
            _log(f"{name}: restarting itself through a helper container")
            return
        await _run(*base, "up", "-d", "--no-deps", it["service"], step="Restart")
        for f in followers:
            if f["project"] and f["service"]:
                it["message"] = f"Reconnecting {f['name']}…"
                await _run(*_compose_args(f), "up", "-d", "--no-deps", "--force-recreate", f["service"],
                           step=f"Reconnecting {f['name']}")
                _log(f"{f['name']}: recreated to follow {name}'s network")
        async with _docker() as d:
            await d.post("/images/prune", params={"filters": '{"dangling":["true"]}'})
        it["job"], it["message"] = "updated", "Updated " + time.strftime("%H:%M")
        _log(f"{name}: updated")
    except Exception as e:
        it["job"], it["message"] = "failed", str(e)[:300]
        _log(f"{name}: {e}"[:300])
    await check(only={name} | {f["name"] for f in followers})
    # check() rebuilt the item: keep the job result on it.
    new = STATE["items"].get(name)
    if new is not None:
        new["job"], new["message"] = it["job"], it["message"]


async def worker():
    while True:
        await _wake.wait()
        _wake.clear()
        while STATE["queue"]:
            name = STATE["queue"].pop(0)
            async with _lock:
                STATE["running"] = name
                try:
                    await _update(name)
                finally:
                    STATE["running"] = None


async def check_loop():
    await asyncio.sleep(20)
    hours = float(_cfg.get("update_check_hours", 6) or 6)
    while True:
        if not STATE["running"]:
            await check()
        await asyncio.sleep(hours * 3600)


def public():
    items = sorted(STATE["items"].values(), key=lambda x: x["name"].lower())
    return {
        "enabled": bool(password()),
        "checked": STATE["checked"],
        "checking": STATE["checking"],
        "check_error": STATE["check_error"],
        "running": STATE["running"],
        "queue": list(STATE["queue"]),
        "log": STATE["log"][-10:],
        "items": [dict({k: it[k] for k in ("name", "image", "running", "service", "state", "message", "job")},
                       managed=bool(it["project"] and it["service"] and it["files"])) for it in items],
    }
