"""
Orama Server converter: makes files play directly on Samsung TVs.

Two fixes, each optional (Settings > Converter):

  * DTS audio (Samsung TVs have no DTS): each DTS track without a playable
    track in the same language gets an E-AC3 copy (Dolby Digital Plus, what
    every Samsung TV and soundbar plays), as the default track. The original
    DTS track is kept, unless "keep the original" is off.
  * Dolby Vision (Samsung TVs have none):
      - profile 7 (UHD Blu-ray): turned into profile 8.1 without re-encoding
        (dovi_tool), which always plays as HDR10. Seconds per file.
      - profile 5 (many streaming downloads, purple and green on Samsung):
        re-encoded to HDR10 with the NAS's Intel graphics (libplacebo). Slow,
        and needs /dev/dri in the container. Experimental, off by default.

Work arrives from Sonarr/Radarr (Connect > Webhook, "On Import" and "On
Upgrade", to /api/hooks/arr) and from a nightly scan of the media folders.
One file at a time, at low priority, optionally only during set hours. A file
is only replaced after the new copy has been checked (same length, every
track there), by an atomic rename next to the original.
"""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("convert")

EXTS = {".mkv", ".mp4", ".m4v"}
TMP_PREFIX = ".orama-tmp-"
MIN_AGE_S = 120                      # a file this fresh may still be importing
# Audio the TV plays (or passes to the soundbar) as is.
PLAYABLE_AUDIO = {"aac", "ac3", "eac3", "truehd", "mp3", "mp2", "flac", "opus", "vorbis", "pcm_s16le", "pcm_s24le"}
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
# jellyfin-ffmpeg: has libplacebo + Vulkan for Dolby Vision profile 5.
FFMPEG_DV = os.environ.get("FFMPEG_DV", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
DOVI_TOOL = os.environ.get("DOVI_TOOL", "dovi_tool")
MKVMERGE = os.environ.get("MKVMERGE", "mkvmerge")
RENDER_NODE = os.environ.get("RENDER_NODE", "/dev/dri/renderD128")


# --------------------------------------------------------------------------- probing
def probe(path):
    out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
                          "-show_chapters", str(path)], capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError("ffprobe: " + ((out.stderr or "").strip().splitlines() or ["failed"])[-1])
    return json.loads(out.stdout)


def duration(info):
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def streams(info, kind):
    return [s for s in info.get("streams", []) if s.get("codec_type") == kind]


def lang(s):
    return ((s.get("tags") or {}).get("language") or "und").lower()


def layout_name(s):
    ch = int(s.get("channels") or 0)
    return {1: "1.0", 2: "2.0", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}.get(ch, f"{ch}ch")


def dovi(info):
    """(profile, compatibility id) of the main video's Dolby Vision, or (None, None)."""
    v = next((s for s in streams(info, "video") if not (s.get("disposition") or {}).get("attached_pic")), None)
    for sd in (v or {}).get("side_data_list") or []:
        if "DOVI" in (sd.get("side_data_type") or "").upper():
            return int(sd.get("dv_profile") or 0), int(sd.get("dv_bl_signal_compatibility_id") or 0)
    return None, None


def dts_needing_copy(info):
    """[(audio index, stream)] of DTS tracks with no TV-playable track in the same language."""
    audio = streams(info, "audio")
    playable_langs = {lang(s) for s in audio if s.get("codec_name") in PLAYABLE_AUDIO}
    return [(i, s) for i, s in enumerate(audio) if s.get("codec_name") == "dts" and lang(s) not in playable_langs]


def plan(info, opts):
    """What a file needs, in order: ['audio', 'dv7' or 'dv5']."""
    todo = []
    if opts.get("audio", True) and dts_needing_copy(info):
        todo.append("audio")
    prof, compat = dovi(info)
    if prof == 7 and opts.get("dv7"):
        todo.append("dv7")
    elif prof == 5 and opts.get("dv5"):
        todo.append("dv5")
    return todo


def describe(info, todo):
    bits = []
    if "audio" in todo:
        bits.append(", ".join(f"{s.get('profile') or 'DTS'} {layout_name(s)}" for _, s in dts_needing_copy(info))
                    + " → E-AC3")
    if "dv7" in todo:
        bits.append("Dolby Vision 7 → 8.1")
    if "dv5" in todo:
        bits.append("Dolby Vision 5 → HDR10")
    return "; ".join(bits)


# --------------------------------------------------------------------------- running tools
def run(cmd, name="", total=0, progress=None, timeout=None, stdin=None):
    """Run a command at low priority; ffmpeg's -progress lines update `progress`."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=stdin, text=True,
                         preexec_fn=lambda: os.nice(10))
    done_s, t0 = 0.0, time.time()
    for line in p.stdout:
        k, _, v = line.strip().partition("=")
        if k == "out_time_us" and v.strip().isdigit():
            done_s = int(v) / 1e6
            if progress and total:
                progress(min(99.0, done_s / total * 100))
        if timeout and time.time() - t0 > timeout:
            p.kill()
            raise RuntimeError(f"{name or cmd[0]} took too long")
    err = p.stderr.read()
    if p.wait() != 0:
        raise RuntimeError(f"{name or cmd[0]}: " + ((err or "").strip().splitlines() or ["failed"])[-1][:300])


def check_output(src_info, dst, want_streams, expect_eac3=False, expect_dovi=None, seconds=0):
    info = probe(dst)
    got = [s for s in info.get("streams", []) if s.get("codec_type") != "data"]
    if len(got) != want_streams:
        raise RuntimeError(f"track count changed ({want_streams} expected, {len(got)} written)")
    d0, d1 = (seconds or duration(src_info)), duration(info)
    if d0 and abs(d0 - d1) > 2:
        raise RuntimeError(f"length changed ({d0:.0f}s → {d1:.0f}s)")
    if expect_eac3 and not any(s.get("codec_name") == "eac3" for s in streams(info, "audio")):
        raise RuntimeError("no E-AC3 track in the result")
    if expect_dovi is not None:
        prof, _ = dovi(info)
        if prof != expect_dovi:
            raise RuntimeError(f"Dolby Vision profile is {prof}, expected {expect_dovi}")
    return info


# --------------------------------------------------------------------------- the three conversions
def audio_cmd(src, dst, info, bitrate, keep_original):
    """Copy everything; add an E-AC3 copy of each DTS track that needs one, as
    the default track, right after the video (Orama and Plex pick it)."""
    audio = streams(info, "audio")
    need = dts_needing_copy(info)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:v?"]
    # New E-AC3 tracks first, then the original audio (minus replaced DTS unless kept).
    for ai, _ in need:
        cmd += ["-map", f"0:a:{ai}"]
    replaced = {ai for ai, _ in need}
    for ai in range(len(audio)):
        if ai in replaced and not keep_original:
            continue
        cmd += ["-map", f"0:a:{ai}"]
    cmd += ["-map", "0:s?", "-map", "0:t?", "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
            "-max_muxing_queue_size", "4096"]
    for k, (ai, s) in enumerate(need):
        cmd += [f"-c:a:{k}", "eac3", f"-b:a:{k}", bitrate]
        lay = layout_name(s)
        if int(s.get("channels") or 0) > 6:     # ffmpeg's E-AC3 stops at 5.1 (keep the LFE)
            cmd += [f"-ac:a:{k}", "6"]
            lay = "5.1"
        cmd += [f"-metadata:s:a:{k}", f"title=E-AC3 {lay}", f"-metadata:s:a:{k}", f"language={lang(s)}",
                f"-disposition:a:{k}", "default" if k == 0 else "0"]
    out_audio = len(need) + sum(1 for ai in range(len(audio)) if not (ai in replaced and not keep_original))
    for k in range(len(need), out_audio):
        cmd += [f"-disposition:a:{k}", "0"]
    cmd += ["-f", "matroska", "-progress", "pipe:1", "-nostats", str(dst)]
    kept = sum(1 for s in info.get("streams", []) if s.get("codec_type") not in ("data",))
    want = kept + len(need) - (0 if keep_original else len(need))
    return cmd, want


def fps_of(info):
    v = streams(info, "video")[0]
    num, _, den = (v.get("r_frame_rate") or "24000/1001").partition("/")
    return f"{num}/{den or 1}p"


def convert_dv7(src, dst, info, workdir, progress):
    """Profile 7 → 8.1, video untouched apart from the Dolby Vision metadata:
    extract the HEVC stream, drop the enhancement layer and convert the RPU
    (dovi_tool), then put it back with every other track (mkvmerge)."""
    hevc = workdir / "video.hevc"
    conv = workdir / "video.p81.hevc"
    progress(5)
    run([FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-c", "copy",
         "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(hevc)], "extracting the video")
    progress(40)
    run([DOVI_TOOL, "-m", "2", "convert", "--discard", "-i", str(hevc), "-o", str(conv)], "dovi_tool")
    hevc.unlink(missing_ok=True)
    progress(70)
    v = streams(info, "video")[0]
    vlang = lang(v)
    run([MKVMERGE, "-q", "-o", str(dst), "--language", f"0:{vlang}", "--default-duration", f"0:{fps_of(info)}",
         str(conv), "--no-video", str(src)], "mkvmerge")
    conv.unlink(missing_ok=True)
    progress(95)


def convert_dv5(src, dst, info, progress, quality=20):
    """Profile 5 → HDR10: libplacebo applies the Dolby Vision reshaping on the
    GPU (Vulkan) and outputs PQ / BT.2020; the Intel encoder (VAAPI) writes
    10-bit HEVC. Audio, subtitles and chapters are copied."""
    filt = ("hwupload=derive_device=vulkan,"
            "libplacebo=apply_dolbyvision=1:colorspace=bt2020nc:color_primaries=bt2020:color_trc=smpte2084:"
            "range=tv:format=p010le,"
            "hwmap=derive_device=vaapi,format=vaapi")
    cmd = [FFMPEG_DV, "-nostdin", "-v", "error", "-y",
           "-init_hw_device", f"vaapi=va:{RENDER_NODE}", "-init_hw_device", "vulkan=vk@va", "-filter_hw_device", "vk",
           "-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
           "-c", "copy", "-map_metadata", "0", "-map_chapters", "0",
           "-filter:v:0", filt, "-c:v:0", "hevc_vaapi", "-profile:v:0", "main10", "-rc_mode", "CQP", "-qp", str(quality),
           "-color_primaries:v:0", "bt2020", "-color_trc:v:0", "smpte2084", "-colorspace:v:0", "bt2020nc",
           "-max_muxing_queue_size", "4096", "-f", "matroska", "-progress", "pipe:1", "-nostats", str(dst)]
    run(cmd, "Dolby Vision 5 → HDR10", duration(info), progress)


def gpu_check():
    """Can this container do the profile 5 conversion? (render node + Vulkan + libplacebo)"""
    if not os.path.exists(RENDER_NODE):
        return False, f"no {RENDER_NODE}: add the Intel graphics to the container (devices: /dev/dri)"
    if not shutil.which(FFMPEG_DV) and not os.path.exists(FFMPEG_DV):
        return False, "jellyfin-ffmpeg isn't installed in this image"
    try:
        out = subprocess.run([FFMPEG_DV, "-v", "error", "-init_hw_device", f"vaapi=va:{RENDER_NODE}",
                              "-init_hw_device", "vulkan=vk@va", "-f", "lavfi", "-i", "testsrc2=size=256x144:duration=1",
                              "-vf", "format=yuv420p10le,hwupload=derive_device=vulkan,libplacebo=format=p010le,"
                              "hwmap=derive_device=vaapi,format=vaapi", "-c:v", "hevc_vaapi", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if out.returncode != 0:
        return False, ((out.stderr or "").strip().splitlines() or ["failed"])[-1][:200]
    return True, "the graphics can do it"


# --------------------------------------------------------------------------- the service
class Converter:
    """Queue + worker + nightly scan. settings() returns the current options:
    enabled, dirs, audio, bitrate, keep_original, dv7, dv5, hours (e.g. "1-7"),
    path_map ("from=to;..."), plex_url, plex_token."""

    def __init__(self, data_dir, settings):
        self.settings = settings
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "convert.db"), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INT, mtime INT,
                               result TEXT, at INT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT,
                               todo TEXT, status TEXT, detail TEXT, added INT, started INT, finished INT,
                               source TEXT, arr TEXT)""")
            # A job that was running when the container stopped starts again.
            self.db.execute("UPDATE jobs SET status='queued', started=NULL WHERE status='running'")
        self.current = None                 # {path, todo, progress, started}
        self.wake = threading.Event()
        self.scanning = False
        self.last_scan = None
        self._stop = False

    # ---- options
    def opts(self):
        return self.settings()

    def map_path(self, p):
        """A path as Sonarr/Radarr see it → as this container sees it."""
        for pair in (self.opts().get("path_map") or "").split(";"):
            a, _, b = pair.partition("=")
            a, b = a.strip().rstrip("/"), b.strip().rstrip("/")
            if a and b and (p == a or p.startswith(a + "/")):
                return b + p[len(a):]
        return p

    def in_hours(self, now=None):
        spec = (self.opts().get("hours") or "").strip()
        if not spec:
            return True
        try:
            a, b = (int(x) for x in spec.split("-"))
        except ValueError:
            return True
        h = (now or time.localtime()).tm_hour
        return a <= h < b if a < b else (h >= a or h < b)

    # ---- queue
    def enqueue(self, path, source="scan", arr=None, todo=None):
        path = str(path)
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM jobs WHERE path=? AND status IN ('queued','running')", (path,)).fetchone():
                return False
            self.db.execute("INSERT INTO jobs (path, todo, status, detail, added, source, arr) VALUES (?,?,?,?,?,?,?)",
                            (path, json.dumps(todo) if todo else None, "queued", "", int(time.time()), source,
                             json.dumps(arr) if arr else None))
        self.wake.set()
        return True

    def jobs(self, limit=50):
        with self.lock:
            rows = self.db.execute("SELECT id, path, todo, status, detail, added, started, finished, source FROM jobs "
                                   "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, id DESC "
                                   "LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            out.append({"id": r[0], "name": os.path.basename(r[1]), "path": r[1], "todo": json.loads(r[2]) if r[2] else None,
                        "status": r[3], "detail": r[4], "added": r[5], "started": r[6], "finished": r[7], "source": r[8]})
        return out

    def status(self):
        o = self.opts()
        with self.lock:
            counts = dict(self.db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
            scanned = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        cur = dict(self.current) if self.current else None
        if cur:
            cur["name"] = os.path.basename(cur["path"])
        return {"enabled": bool(o.get("enabled")), "in_hours": self.in_hours(), "hours": o.get("hours") or "",
                "current": cur, "counts": counts, "scanned": scanned, "scanning": self.scanning,
                "last_scan": self.last_scan, "jobs": self.jobs()}

    def retry(self, job_id):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status='queued', detail='', started=NULL, finished=NULL WHERE id=? "
                            "AND status IN ('failed','skipped')", (job_id,))
        self.wake.set()

    def clear_finished(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM jobs WHERE status IN ('done','failed','skipped')")

    def _next(self):
        with self.lock:
            return self.db.execute("SELECT id, path, todo, arr FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def _finish(self, job_id, status, detail):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status=?, detail=?, finished=? WHERE id=?",
                            (status, detail[:500], int(time.time()), job_id))

    def _remember(self, path, result):
        try:
            st = os.stat(path)
        except OSError:
            return
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                            (str(path), st.st_size, int(st.st_mtime), result, int(time.time())))

    # ---- webhooks from Sonarr / Radarr
    def hook(self, payload):
        """Connect > Webhook. Returns the number of files queued."""
        ev = (payload.get("eventType") or "").lower()
        if ev == "test":
            return 0
        if ev not in ("download", "upgrade"):      # Radarr/Sonarr send "Download" for imports and upgrades
            return 0
        paths, arr = [], {}
        if payload.get("movie"):
            mf = payload.get("movieFile") or {}
            p = mf.get("path") or os.path.join((payload["movie"].get("folderPath") or ""), mf.get("relativePath") or "")
            if p:
                paths.append(p)
            arr = {"app": "radarr", "id": payload["movie"].get("id")}
        if payload.get("series"):
            for ef in payload.get("episodeFiles") or ([payload["episodeFile"]] if payload.get("episodeFile") else []):
                p = ef.get("path") or os.path.join((payload["series"].get("path") or ""), ef.get("relativePath") or "")
                if p:
                    paths.append(p)
            arr = {"app": "sonarr", "id": payload["series"].get("id")}
        n = 0
        for p in paths:
            n += 1 if self.enqueue(self.map_path(p), source=arr.get("app", "webhook"), arr=arr) else 0
        return n

    # ---- the nightly scan
    def scan(self):
        if self.scanning:
            return
        self.scanning = True
        found = 0
        try:
            o = self.opts()
            for root in [d.strip() for d in (o.get("dirs") or "").split(",") if d.strip()]:
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for f in filenames:
                        if self._stop:
                            return
                        p = os.path.join(dirpath, f)
                        if f.startswith(".") or os.path.splitext(f)[1].lower() not in EXTS:
                            continue
                        try:
                            st = os.stat(p)
                        except OSError:
                            continue
                        if time.time() - st.st_mtime < MIN_AGE_S:
                            continue
                        with self.lock:
                            row = self.db.execute("SELECT size, mtime FROM files WHERE path=?", (p,)).fetchone()
                        if row and row[0] == st.st_size and row[1] == int(st.st_mtime):
                            continue
                        try:
                            info = probe(p)
                        except Exception as e:  # noqa: BLE001
                            self._remember(p, "unreadable: " + str(e)[:100])
                            continue
                        todo = plan(info, o)
                        if todo:
                            found += 1 if self.enqueue(p, "scan", todo=todo) else 0
                        else:
                            self._remember(p, "ok")
            self.last_scan = {"at": int(time.time()), "queued": found}
            log.info("Converter scan done: %d file(s) queued", found)
        finally:
            self.scanning = False

    # ---- the worker
    def loop(self):
        last_scan_day = None
        while not self._stop:
            o = self.opts()
            today = time.strftime("%Y-%m-%d")
            if o.get("enabled") and last_scan_day != today and time.localtime().tm_hour >= int(o.get("scan_hour") or 3):
                last_scan_day = today
                threading.Thread(target=self.scan, daemon=True).start()
            job = self._next() if o.get("enabled") and self.in_hours() else None
            if not job:
                self.wake.wait(60)
                self.wake.clear()
                continue
            self.process(*job)

    def process(self, job_id, path, todo_json, arr_json):
        o = self.opts()
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (int(time.time()), job_id))
        self.current = {"path": path, "todo": None, "progress": 0, "started": int(time.time()), "step": "checking"}
        try:
            if not os.path.exists(path):
                return self._finish(job_id, "skipped", "the file isn't there any more")
            age = time.time() - os.path.getmtime(path)
            if age < MIN_AGE_S:
                time.sleep(MIN_AGE_S - age)       # let the import settle
            info = probe(path)
            todo = plan(info, o)
            if not todo:
                self._remember(path, "ok")
                return self._finish(job_id, "skipped", "nothing to change")
            self.current["todo"] = todo
            t0 = time.time()
            self.convert(path, info, todo, o)
            self._remember(path, "converted: " + ",".join(todo))
            self._finish(job_id, "done", f"{describe(info, todo)} in {int(time.time() - t0)} s")
            log.info("Converted %s (%s)", os.path.basename(path), ", ".join(todo))
            self.after(path, json.loads(arr_json) if arr_json else None, o)
        except Exception as e:  # noqa: BLE001
            log.warning("Converting %s failed: %s", os.path.basename(path), e)
            self._finish(job_id, "failed", str(e))
        finally:
            self.current = None

    def convert(self, path, info, todo, o):
        """Run the steps one after another, each writing a hidden copy next to
        the file, checking it, and swapping it in."""
        src = Path(path)
        for step in todo:
            info = probe(src)                       # each step starts from the current file
            self.current["step"] = step
            self.current["progress"] = 0
            prog = lambda pct: self.current.__setitem__("progress", round(pct, 1))  # noqa: E731
            tmp = src.with_name(TMP_PREFIX + src.stem + ".mkv")
            work = src.with_name(TMP_PREFIX + src.stem + ".work")
            need = src.stat().st_size * (2.2 if step == "dv7" else 1.15)
            free = shutil.disk_usage(src.parent).free
            if free < need:
                raise RuntimeError(f"not enough free space ({free / 1e9:.0f} GB free, about {need / 1e9:.0f} GB needed)")
            try:
                if step == "audio":
                    cmd, want = audio_cmd(src, tmp, info, o.get("bitrate") or "1536k", o.get("keep_original", True))
                    run(cmd, "ffmpeg", duration(info), prog)
                    check_output(info, tmp, want, expect_eac3=True)
                elif step == "dv7":
                    work.mkdir(exist_ok=True)
                    convert_dv7(src, tmp, info, work, prog)
                    want = sum(1 for s in info.get("streams", []) if s.get("codec_type") != "data")
                    check_output(info, tmp, want, expect_dovi=8)
                elif step == "dv5":
                    ok, why = gpu_check()
                    if not ok:
                        raise RuntimeError("Dolby Vision 5 needs the graphics: " + why)
                    convert_dv5(src, tmp, info, prog)
                    want = sum(1 for s in info.get("streams", []) if s.get("codec_type") != "data"
                               and not (s.get("codec_type") == "video" and s is not streams(info, "video")[0]))
                    check_output(info, tmp, want, expect_dovi=None)
                st = src.stat()
                os.chmod(tmp, st.st_mode & 0o7777)
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except PermissionError:
                    pass
                final = src.with_suffix(".mkv")
                os.replace(tmp, final)
                if final != src:                    # an .mp4 became an .mkv
                    src.unlink(missing_ok=True)
                    src = final
            finally:
                tmp.unlink(missing_ok=True)
                shutil.rmtree(work, ignore_errors=True)

    def after(self, path, arr, o):
        """Let Plex (and Sonarr/Radarr, when they sent it) notice the new file."""
        url, token = (o.get("plex_url") or "").rstrip("/"), o.get("plex_token") or ""
        if url and token:
            try:
                h = {"X-Plex-Token": token, "Accept": "application/json"}
                secs = requests.get(url + "/library/sections", headers=h, timeout=15).json()["MediaContainer"].get("Directory", [])
                for s in secs:
                    if s.get("type") in ("movie", "show"):
                        requests.get(f"{url}/library/sections/{s['key']}/refresh", headers=h, timeout=15)
            except Exception as e:  # noqa: BLE001
                log.info("Couldn't ask Plex to rescan: %s", e)
        if arr and arr.get("id") and o.get("arr"):
            app = arr.get("app")
            cfg = o["arr"].get(app) or {}
            if cfg.get("url") and cfg.get("api_key"):
                name = "RescanMovie" if app == "radarr" else "RescanSeries"
                key = "movieId" if app == "radarr" else "seriesId"
                try:
                    requests.post(cfg["url"].rstrip("/") + "/api/v3/command", json={"name": name, key: arr["id"]},
                                  headers={"X-Api-Key": cfg["api_key"]}, timeout=15)
                except requests.RequestException:
                    pass

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="converter").start()

    def stop(self):
        self._stop = True
        self.wake.set()
