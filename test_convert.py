"""Converter tests with real files (needs ffmpeg/ffprobe). python3 test_convert.py"""
import json, os, shutil, subprocess, tempfile, time

import convert

T = tempfile.mkdtemp()
MEDIA = os.path.join(T, "media")
os.makedirs(os.path.join(MEDIA, "Movies", "Sample (2020)"))


def make(path, audio, seconds=12):
    """audio: [(codec, channels, language)]"""
    srt = os.path.join(T, "s.srt")
    with open(srt, "w") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\nHello\n")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=24:duration={seconds}"]
    for codec, ch, _ in audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000"]
    cmd += ["-i", srt, "-map", "0:v"]
    for i in range(len(audio)):
        cmd += ["-map", f"{i + 1}:a"]
    cmd += ["-map", f"{len(audio) + 1}:s", "-c:v", "libx264", "-preset", "ultrafast", "-c:s", "srt"]
    for i, (codec, ch, lang) in enumerate(audio):
        cmd += [f"-c:a:{i}", codec, f"-ac:a:{i}", str(ch), f"-metadata:s:a:{i}", f"language={lang}"]
        if codec == "dca":
            cmd += ["-strict", "-2"]
    cmd += [path]
    subprocess.run(cmd, check=True)
    old = time.time() - 600
    os.utime(path, (old, old))


def codecs(path):
    info = convert.probe(path)
    return [(s["codec_type"], s.get("codec_name"), convert.lang(s), (s.get("disposition") or {}).get("default", 0))
            for s in info["streams"]], convert.duration(info)


opts = {"enabled": True, "dirs": MEDIA, "audio": True, "bitrate": "640k", "keep_original": True,
        "dv7": False, "dv5": False, "hours": "", "scan_hour": 3, "path_map": "/data/media=" + MEDIA,
        "plex_url": "", "plex_token": "", "arr": {}}
c = convert.Converter(os.path.join(T, "data"), lambda: opts)

# --- planning
movie = os.path.join(MEDIA, "Movies", "Sample (2020)", "Sample (2020).mkv")
make(movie, [("dca", 6, "eng"), ("aac", 2, "fre")])
info = convert.probe(movie)
assert convert.plan(info, opts) == ["audio"], convert.plan(info, opts)
assert "DTS" in convert.describe(info, ["audio"])
both = os.path.join(MEDIA, "Movies", "Both.mkv")
make(both, [("dca", 6, "eng"), ("eac3", 6, "eng")])
assert convert.plan(convert.probe(both), opts) == [], "a playable English track already exists"
print("planning OK")

# --- webhook from Radarr (path as Radarr sees it, mapped)
n = c.hook({"eventType": "Download", "movie": {"id": 7, "folderPath": "/data/media/Movies/Sample (2020)"},
            "movieFile": {"relativePath": "Sample (2020).mkv"}})
assert n == 1, n
assert c.jobs()[0]["path"] == movie and c.jobs()[0]["source"] == "radarr"
assert c.hook({"eventType": "Download", "movie": {"id": 7, "folderPath": "/data/media/Movies/Sample (2020)"},
               "movieFile": {"relativePath": "Sample (2020).mkv"}}) == 0, "already queued"
assert c.hook({"eventType": "Test"}) == 0 and c.hook({"eventType": "Grab", "movie": {}}) == 0
print("webhook OK")

# --- converting: E-AC3 first and default, DTS kept, everything else intact
before, d0 = codecs(movie)
job = c._next()
c.process(*job)
st = c.status()
assert st["jobs"][0]["status"] == "done", st["jobs"][0]
after, d1 = codecs(movie)
print(" before:", before)
print(" after: ", after)
assert [a[1] for a in after if a[0] == "audio"] == ["eac3", "dts", "aac"], after
assert next(a for a in after if a[1] == "eac3")[2:] == ("eng", 1), "E-AC3 is English and default"
assert next(a for a in after if a[1] == "dts")[3] == 0 and next(a for a in after if a[1] == "aac")[3] == 0
assert [a for a in after if a[0] == "subtitle"] and abs(d0 - d1) < 1
assert not [f for f in os.listdir(os.path.dirname(movie)) if f.startswith(".orama-tmp")], "temp files cleaned"
assert convert.plan(convert.probe(movie), opts) == [], "done once, not again"
print("convert (keep original) OK")

# --- without keeping the original; .mp4 input becomes .mkv
opts["keep_original"] = False
mp4 = os.path.join(MEDIA, "Movies", "Clip.mkv")
make(mp4, [("dca", 6, "ger")])
c.enqueue(mp4, "manual")
c.process(*c._next())
after, _ = codecs(mp4)
assert [a[1] for a in after if a[0] == "audio"] == ["eac3"], after
print("convert (replace) OK")

# --- a failing conversion leaves the file alone
bad = os.path.join(MEDIA, "Movies", "Bad.mkv")
make(bad, [("dca", 6, "eng")])
size0 = os.path.getsize(bad)
real = convert.FFMPEG
convert.FFMPEG = "/bin/false"
c.enqueue(bad, "manual")
c.process(*c._next())
convert.FFMPEG = real
assert c.jobs()[0]["status"] == "failed" and os.path.getsize(bad) == size0
assert not [f for f in os.listdir(os.path.dirname(bad)) if f.startswith(".orama-tmp")]
c.retry(c.jobs()[0]["id"])
c.process(*c._next())
assert c.jobs()[0]["status"] == "done", c.jobs()[0]
print("failure safety + retry OK")

# --- scan: finds new DTS files, remembers the rest, skips files it already checked
opts["keep_original"] = True
fresh = os.path.join(MEDIA, "Movies", "Scan Me.mkv")
make(fresh, [("dca", 2, "eng")])
c.clear_finished()
c.scan()
queued = [j["name"] for j in c.jobs() if j["status"] == "queued"]
assert queued == ["Scan Me.mkv"], queued
c.process(*c._next())
c.scan()
assert not [j for j in c.jobs() if j["status"] == "queued"], "nothing new the second time"
assert c.status()["scanned"] >= 4
print("scan OK")

# --- hours window
opts["hours"] = "1-7"
assert c.in_hours(time.struct_time((2026, 1, 1, 3, 0, 0, 0, 1, 0))) and not c.in_hours(time.struct_time((2026, 1, 1, 12, 0, 0, 0, 1, 0)))
opts["hours"] = "22-6"
assert c.in_hours(time.struct_time((2026, 1, 1, 23, 0, 0, 0, 1, 0))) and not c.in_hours(time.struct_time((2026, 1, 1, 12, 0, 0, 0, 1, 0)))
print("hours OK")

# --- Dolby Vision detection from ffprobe's side data
fake = {"streams": [{"codec_type": "video", "side_data_list": [{"side_data_type": "DOVI configuration record",
                                                                "dv_profile": 5, "dv_bl_signal_compatibility_id": 0}]}]}
assert convert.dovi(fake) == (5, 0)
assert convert.plan(fake, dict(opts, dv5=True)) == ["dv5"] and convert.plan(fake, opts) == []
fake["streams"][0]["side_data_list"][0]["dv_profile"] = 7
assert convert.plan(fake, dict(opts, dv7=True)) == ["dv7"]
fake["streams"][0]["side_data_list"][0]["dv_profile"] = 8
assert convert.plan(fake, dict(opts, dv7=True, dv5=True)) == [], "8.1 plays fine"
print("Dolby Vision planning OK")

shutil.rmtree(T)
print("ALL CONVERTER TESTS OK")
