"""Run the built macOS smoke entry point without exposing test credentials."""
import argparse
import json
import subprocess
import time
from pathlib import Path

base = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--offline', action='store_true')
args = parser.parse_args()
app = base.parent.parent / 'mobile/app/build/macos/Build/Products/Debug/Boa Voice.app/Contents/MacOS/Boa Voice'
root = Path.home() / 'Library/Containers/com.boa.voice.desktop/Data/Documents'
root.mkdir(parents=True, exist_ok=True)
mode = 'offline' if args.offline else 'smoke'
report = root / f'audio-storage-{mode}-report.json'
report.unlink(missing_ok=True)
config = {'offline': True} if args.offline else {
    'url': 'http://127.0.0.1:8765',
    'token': (base / 'artifacts/server-data/token').read_text().strip(),
}
seed = root / 'audio-storage-smoke.json'
seed.write_text(json.dumps(config))
seed.chmod(0o600)
with (base / 'artifacts/macos-native.log').open('w') as log:
    process = subprocess.Popen([str(app)], stdout=log, stderr=log)
    try:
        for _ in range(600):
            if report.exists():
                result = json.loads(report.read_text())
                (base / f'artifacts/macos-{mode}-report.json').write_text(json.dumps(result, indent=2))
                print(json.dumps(result, indent=2))
                raise SystemExit(0 if result.get('passed') else 1)
            if process.poll() is not None:
                raise SystemExit('Native test app exited before report')
            time.sleep(1)
        raise SystemExit('Native test timeout')
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        seed.unlink(missing_ok=True)
