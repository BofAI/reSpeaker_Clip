"""Real TCP HTTP verification with generated audio, restart and RSS measurement."""

import hashlib
import json
import math
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import httpx
import imageio_ffmpeg
import psutil

BASE = Path(__file__).resolve().parent
OUT = BASE / "artifacts"
OUT.mkdir(exist_ok=True)
DATA = OUT / "verification-data"
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def wav(path, byte_count):
    frames = byte_count // 4
    block = b"".join(
        struct.pack(
            "<hh",
            int(1500 * math.sin(2 * math.pi * 440 * i / 48000)),
            int(1500 * math.sin(2 * math.pi * 440 * i / 48000)),
        )
        for i in range(48000)
    )
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(48000)
        while frames:
            n = min(frames, 48000)
            f.writeframesraw(block[: n * 4])
            frames -= n


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
url = f"http://127.0.0.1:{port}"
log = (OUT / "live-server.log").open("w")
process = None


def start():
    global process
    process = subprocess.Popen(
        [sys.executable, "run.py", "--data-dir", str(DATA), "--port", str(port)],
        cwd=BASE,
        stdout=log,
        stderr=log,
    )
    for _ in range(100):
        try:
            r = httpx.get(url + "/api/audio/recordings", timeout=1)
            if r.status_code == 401:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Server failed to start")


def stop(kill=False):
    if process and process.poll() is None:
        process.kill() if kill else process.terminate()
        process.wait(timeout=15)


start()
token = (DATA / "token").read_text().strip()
c = httpx.Client(
    base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=120
)
report = {"cases": [], "checks": {}, "server_peak_rss_mib": 0}
monitor_done = threading.Event()


def monitor():
    while not monitor_done.wait(0.02):
        try:
            rss = psutil.Process(process.pid).memory_info().rss / 1024**2
            report["server_peak_rss_mib"] = max(report["server_peak_rss_mib"], rss)
        except psutil.Error:
            pass


threading.Thread(target=monitor, daemon=True).start()


def request(method, path, **kwargs):
    r = c.request(method, path, **kwargs)
    r.raise_for_status()
    return r


try:
    small = OUT / "tone.wav"
    wav(small, 48000 * 4 * 4)
    files = [small]
    for ext, codec in [("m4a", "aac"), ("mp3", "libmp3lame"), ("ogg", "libopus")]:
        dest = OUT / f"tone.{ext}"
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-i", str(small), "-c:a", codec, str(dest)],
            check=True,
        )
        files.append(dest)
    for size in [101 * 1024**2, 1024**3]:
        f = OUT / f"tone-{size // 1024**2}MiB.wav"
        wav(f, size)
        files.append(f)
    for source in files:
        began = time.monotonic()
        sha = digest(source)
        mime = {
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
        }[source.suffix]
        info = request(
            "POST",
            "/api/audio/uploads",
            json={
                "filename": source.name,
                "size": source.stat().st_size,
                "sha256": sha,
                "mime": mime,
            },
        ).json()
        uid, chunk = info["id"], info["chunk_size"]
        with source.open("rb") as f:
            i = 0
            while block := f.read(chunk):
                path = f"/api/audio/uploads/{uid}/parts/{i}"
                headers = {"X-Content-SHA256": hashlib.sha256(block).hexdigest()}
                request("PUT", path, content=block, headers=headers)
                if i == 0:
                    request("PUT", path, content=block, headers=headers)
                if i == 1 and source.stat().st_size > 100 * 1024**2:
                    stop(kill=True)
                    start()
                    resume = request("GET", f"/api/audio/uploads/{uid}").json()
                    assert len(resume["parts"]) == 2
                    report["checks"]["process_kill_upload_resume"] = True
                i += 1
        request("POST", f"/api/audio/uploads/{uid}/complete")
        for _ in range(600):
            info = request("GET", f"/api/audio/uploads/{uid}").json()
            if info["state"] == "complete":
                break
            assert info["state"] != "failed", info
            time.sleep(0.05)
        assert info["state"] == "complete"
        path = f"/api/audio/recordings/{uid}/content"
        h = request("HEAD", path)
        assert int(h.headers["content-length"]) == source.stat().st_size
        target = OUT / f"download-{source.name}"
        # Save a prefix, close the connection, then request the remaining bytes.
        cut = min(10001, source.stat().st_size // 2)
        first = request("GET", path, headers={"Range": f"bytes=0-{cut - 1}"})
        assert first.status_code == 206
        target.write_bytes(first.content)
        with c.stream(
            "GET",
            path,
            headers={"Range": f"bytes={cut}-", "If-Range": h.headers["etag"]},
        ) as r:
            assert r.status_code == 206
            with target.open("ab") as out:
                for block in r.iter_bytes(256 * 1024):
                    out.write(block)
        assert digest(target) == sha
        subprocess.run(
            [FFMPEG, "-v", "error", "-i", str(target), "-f", "null", "-"], check=True
        )
        # Decode the tail using HTTP seeking / Range and authenticated requests.
        subprocess.run(
            [
                FFMPEG,
                "-v",
                "error",
                "-headers",
                f"Authorization: Bearer {token}\r\n",
                "-sseof",
                "-1",
                "-i",
                url + path,
                "-f",
                "null",
                "-",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        report["cases"].append(
            {
                "file": source.name,
                "bytes": source.stat().st_size,
                "sha256": sha,
                "upload_download_hash_match": True,
                "decode_full": True,
                "http_seek_decode": True,
                "seconds": round(time.monotonic() - began, 2),
                "id": uid,
            }
        )
        print(json.dumps(report["cases"][-1]), flush=True)
    stop()
    start()
    records = request("GET", "/api/audio/recordings", params={"limit": 100}).json()[
        "items"
    ]
    assert set(x["id"] for x in report["cases"]) <= set(x["id"] for x in records)
    report["checks"]["persistent_list_after_restart"] = True
    report["checks"]["download_resume"] = True
    report["checks"]["duplicate_part_idempotent"] = True
    report["passed"] = True
finally:
    monitor_done.set()
    stop()
    c.close()
    log.close()
    (OUT / "live-report.json").write_text(json.dumps(report, indent=2))
    print("Report:", OUT / "live-report.json", flush=True)
