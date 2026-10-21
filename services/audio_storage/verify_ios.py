"""Seed and launch the installed development smoke runner; never print credentials."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument("--udid", required=True)
p.add_argument("--offline", action="store_true")
p.add_argument("--url", default="http://127.0.0.1:8765")
p.add_argument("--data-dir", type=Path, default=BASE / "artifacts/server-data")
a = p.parse_args()
os.environ["DEVELOPER_DIR"] = "/Applications/Xcode.app/Contents/Developer"
root = Path(
    subprocess.check_output(
        ["xcrun", "simctl", "get_app_container", a.udid, "cc.seeed.voice", "data"], text=True
    ).strip()
)
name = "offline" if a.offline else "smoke"
report = root / f"Documents/audio-storage-{name}-report.json"
report.unlink(missing_ok=True)
config = (
    {"offline": True}
    if a.offline
    else {"url": a.url, "token": (a.data_dir / "token").read_text().strip()}
)
f = root / "Documents/audio-storage-smoke.json"
f.write_text(json.dumps(config))
f.chmod(0o600)
subprocess.run(
    ["xcrun", "simctl", "terminate", a.udid, "cc.seeed.voice"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
subprocess.run(["xcrun", "simctl", "launch", a.udid, "cc.seeed.voice"], check=True)
for _ in range(600):
    if report.exists():
        result = json.loads(report.read_text())
        (BASE / f"artifacts/ios-{name}-report.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get("passed") else 1)
    time.sleep(1)
raise SystemExit("Native playback report timed out")
