"""Run authorized idle/recording/BLE-download/AP-idle resource experiments.

Creates two local recordings, downloads them with CRC verification, and
temporarily enables the device AP. Does not join the AP or delete recordings.
Requires Python 3.9+ and bleak. Audio and results stay in the output directory.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import time
import zlib

from collect import Connection, TX

FILE_TX = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"


async def run(args):
    from bleak import BleakClient

    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / "measurements.jsonl").open("x", buffering=1) as log:
        phase = "setup"

        def save(kind, value):
            row = {"kind": kind, "phase": phase, "host_monotonic": time.monotonic(),
                   "host_time": datetime.now(timezone.utc).isoformat(), "value": value}
            log.write(json.dumps(row) + "\n")
            if kind != "thread":
                print(json.dumps(row), flush=True)

        async with BleakClient(args.address, timeout=30) as client:
            connection = Connection(client)
            await client.start_notify(TX, connection.receive)

            async def query(command, *, redact=False):
                save("request", {"command": command})
                try:
                    result = await connection.request(
                        command, timeout=60 if command == "AT+WIFI=on" else 20)
                except Exception as error:
                    save("request_failed", {"command": command, "type": type(error).__name__,
                                            "message": str(error)})
                    raise
                if not redact:
                    save(command, result)
                return result

            async def sample(seconds, interval=3):
                await query("AT+RES")
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    await asyncio.sleep(min(interval, max(0, end - time.monotonic())))
                    status = await query("AT+GSTAT")
                    if phase.startswith("recording_"):
                        assert status["data"]["state"] == "RECORDING", status
                        assert status["data"]["recording"] is True, status
                    stats = await query("AT+RES")
                    assert stats["data"]["build"] == "runtime-0.1.2"
                    save("sample", {"status": status["data"], "resources": stats["data"]})

            async def stacks():
                first = await connection.request("AT+RES=0")
                save("thread", first["data"])
                for i in range(1, first["data"]["total"]):
                    save("thread", (await connection.request(f"AT+RES={i}"))["data"])

            state = (await query("AT+GSTAT"))["data"]
            assert state["state"] == "IDLE" and not state["recording"]
            usb = (await query("AT+USB?"))["data"]["status"]
            assert usb == "off", "Disable USB before running recording measurements"
            await query("AT+LOG?")
            await query("AT+VERSION")
            initial_resources = (await query("AT+RES"))["data"]
            assert initial_resources["schema"] == 1
            # Restore modes/AP state even if a test fails. Leave USB and logs as found.
            original_mode = state["mode"]
            wifi = (await query("AT+WIFI?", redact=True))["data"]
            save("initial_wifi", {k: v for k, v in wifi.items() if k not in ("password", "ssid")})
            assert wifi.get("running") is False, "Device AP must initially be off"
            recording = False
            ap_enabled = False
            sessions = []
            try:
                await query("AT+TIME=" + str(int(time.time())))
                phase = "idle_ble_connected"
                save("settle", {"seconds": 46})
                await asyncio.sleep(46)
                await sample(18)
                await stacks()
                modes = ("normal", "enhanced") + (("enhanced",) if args.repeat_enhanced else ())
                for number, mode in enumerate(modes):
                    phase = "recording_" + mode + ("_restart" if number == 2 else "")
                    await query("AT+START=" + mode)
                    recording = True
                    if args.pause_resume and number == 2:
                        await sample(args.record_seconds / 2)
                        await query("AT+PAUSE")
                        paused = (await query("AT+GSTAT"))["data"]
                        assert paused["state"] == "PAUSED", paused
                        await query("AT+RES")
                        await asyncio.sleep(2)
                        await query("AT+RESUME")
                        await sample(args.record_seconds / 2)
                    else:
                        await sample(args.record_seconds)
                    await stacks()
                    stopped = await query("AT+STOP")
                    recording = False
                    stopped_resources = (await query("AT+RES"))["data"]
                    if args.expect_release:
                        # Permit small network bookkeeping retained after the first AP.
                        assert stopped_resources["heap_allocated"] <= initial_resources["heap_allocated"] + 512, stopped_resources
                    sid = stopped["data"]["session"]
                    assert len(sid) == 14 and sid.isdecimal()
                    sessions.append(sid)
                    await query("AT+LIST=" + sid)
                    if args.ap_after_each:
                        phase = "ap_after_" + mode + ("_restart" if number == 2 else "")
                        await query("AT+WIFI=on", redact=True)
                        ap_enabled = True
                        assert (await query("AT+WIFI?", redact=True))["data"]["running"]
                        await query("AT+RES")
                        save("post_record_ap", {"running": True, "session": sid})
                        await query("AT+WIFI=off")
                        ap_enabled = False
                        await query("AT+RES")

                phase = "ble_download"
                frames = asyncio.Queue()
                await client.start_notify(FILE_TX, lambda _c, data: frames.put_nowait(bytes(data)))
                for sid in sessions:
                    async def download():
                        target = args.output / sid
                        target.mkdir()
                        handle = None
                        count = 0
                        verified = False
                        try:
                            save("transfer_begin", {"session": sid})
                            await query("AT+DOWNLOAD=" + sid)
                            while True:
                                frame = await asyncio.wait_for(frames.get(), 30)
                                if frame[0] == 0x10:
                                    assert handle is None
                                    n = frame[1]
                                    assert len(frame) == n + 6
                                    name = frame[2:2+n].decode("ascii")
                                    assert len(name) == 9 and name[:4].isdigit() and name.endswith(".opus")
                                    path = target / name
                                    handle = path.with_suffix(".opus.part").open("xb")
                                    expected = struct.unpack_from("<I", frame, 2+n)[0]
                                    received, sequence, crc = 0, 0, 0
                                    verified = False
                                elif frame[0] == 1:
                                    seq, size = struct.unpack_from("<HH", frame, 1)
                                    assert handle and seq == sequence and len(frame) == size+5
                                    handle.write(frame[5:])
                                    crc = zlib.crc32(frame[5:], crc)
                                    received += size
                                    assert received <= expected
                                    sequence = (sequence+1) & 65535
                                elif frame[0] == 0x11:
                                    assert handle and len(frame) == 5 and received == expected
                                    assert crc & 0xffffffff == struct.unpack_from("<I", frame, 1)[0]
                                    handle.close()
                                    path.with_suffix(".opus.part").rename(path)
                                    handle = None
                                    count += 1
                                    verified = True
                                    save("file_verified", {"session": sid, "file": path.name,
                                                           "bytes": received, "crc32": crc & 0xffffffff})
                                elif frame[0] == 0x12:
                                    n = frame[1]
                                    assert len(frame) == n+6 and frame[2:2+n].decode() == sid
                                    assert verified and count == struct.unpack_from("<I", frame, 2+n)[0]
                                    save("transfer_end", {"session": sid, "files": count})
                                    return
                                else:
                                    raise ValueError(f"Unexpected file frame {frame[0]}")
                        finally:
                            if handle:
                                handle.close()

                    # CPU windows include transfer setup/completion. Preserve timestamps
                    # so windows crossing those boundaries can be excluded in analysis.
                    await query("AT+RES")
                    task = asyncio.create_task(download())
                    try:
                        while not task.done():
                            await asyncio.sleep(1)
                            await query("AT+RES")
                        await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                            if client.is_connected:
                                await query("AT+CANCEL")
                await stacks()
                phase = "wifi_ap_idle_ble_connected"
                await query("AT+WIFI=on", redact=True)
                ap_enabled = True
                save("wifi_ap", {"enabled": True, "host_joined": False, "udp_transfer": False})
                await asyncio.sleep(5)
                await sample(18)
                await stacks()
                await query("AT+WIFI=off")
                ap_enabled = False
                phase = "post_workloads_idle"
                await query("AT+MODE=" + original_mode)
                await sample(6)
                save("complete", {"sessions": sessions})
            finally:
                if client.is_connected:
                    if recording:
                        await query("AT+STOP")
                    if ap_enabled:
                        await query("AT+WIFI=off")
                    await query("AT+MODE=" + original_mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--record-seconds", type=int, default=30)
    parser.add_argument("--repeat-enhanced", action="store_true")
    parser.add_argument("--pause-resume", action="store_true")
    parser.add_argument("--ap-after-each", action="store_true")
    parser.add_argument("--expect-release", action="store_true")
    args = parser.parse_args()
    if args.record_seconds < 10:
        parser.error("record-seconds must be at least 10")
    if args.pause_resume and not args.repeat_enhanced:
        parser.error("pause-resume requires repeat-enhanced")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
