# reSpeaker Clip SDK

> Experimental HTTPS profile: `experiment/https-runtime-0.1.2` replaces the AP
> with Station mode and disables the USB disk. Use the dedicated
> [USB HTTPS tools](../tools/https_poc/README.md) for credentials and upload
> control; the SDK does not provide those experimental commands as high-level
> methods. `AT+RES` remains available over BLE for resource measurements when
> enabled, including while an upload is active. Standard AP/UDP workflows below
> apply to the normal firmware profile.

The new Python SDK for the current reSpeaker Clip firmware.  It is intentionally
independent from `applications/clip/tests`: that directory remains the legacy
test/tool collection and is neither imported nor modified by this package.

The SDK provides:

- an asynchronous, typed AT-command API;
- BLE transport (`pip install -e '.[ble]'`) and dependency-free Wi-Fi/UDP transport;
- sequential command dispatch, so concurrent callers cannot consume one another's
  command responses;
- streaming file download to `*.part` files, with length and CRC32 checks before an
  atomic rename; and
- an intentionally small CLI for common inspection and download tasks.

## Install for development

```sh
cd /home/lht/clip/sdk
python -m pip install -e '.[dev,ble]'
pytest
```

BLE is optional.  Wi-Fi/UDP needs only the Python standard library.

Installation registers these commands on `PATH`:

```sh
clip.terminal --transport ble --address AA:BB:CC:DD:EE:FF
clip.sync --transport udp --all --output recordings
clip.record --transport ble --mode enhanced --duration 60
clip.web --transport udp
```

`clip-sdk` remains the compact JSON-oriented CLI; the `clip.*` commands are the
interactive and workflow-oriented tools. `clip.web` is always registered but
requires the `web` extra when run: `pip install -e '.[web]'` (or `.[web,ble]`
when it controls a BLE device).

## Quick start

```python
import asyncio
from pathlib import Path

from clip import ClipClient, BleTransport


async def main() -> None:
    async with ClipClient(BleTransport(name="Clip")) as clip:
        print(await clip.status())
        sessions = await clip.list_all_sessions()
        if sessions:
            result = await clip.download_session(sessions[0].id, Path("recordings"))
            print(result.files)


asyncio.run(main())
```

For Wi-Fi after enabling the Clip AP:

```python
from clip import ClipClient, UdpTransport

async with ClipClient(UdpTransport()) as clip:
    print(await clip.storage())
```

More detail is in [docs/architecture.md](docs/architecture.md) and
[docs/api.md](docs/api.md).  Direct-run utilities are in
[tools/README.md](tools/README.md).
