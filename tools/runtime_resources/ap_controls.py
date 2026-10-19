"""Isolate AP startup after normal/enhanced recording on an instrumented Clip.

Each arm starts with a normal reboot. Creates short recordings; does not
download or delete files. Requires USB, SD logging and AP initially off.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from collect import Connection, TX


async def run(args):
    from bleak import BleakClient

    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / "controls.jsonl").open("x", buffering=1) as output:
        arm = "preflight"

        def save(kind, value):
            row = {"arm": arm, "kind": kind, "host_time": datetime.now(timezone.utc).isoformat(),
                   "host_monotonic": time.monotonic(), "value": value}
            output.write(json.dumps(row) + "\n")
            print(json.dumps(row), flush=True)

        async def connect():
            client = BleakClient(args.address, timeout=30)
            await client.connect()
            connection = Connection(client)
            await client.start_notify(TX, connection.receive)
            return client, connection

        client, connection = await connect()

        async def query(command):
            save("request", command)
            started = time.monotonic()
            try:
                result = await connection.request(
                    command, timeout=60 if command == "AT+WIFI=on" else 20)
            except Exception as error:
                save("error", {"command": command, "type": type(error).__name__,
                               "message": str(error), "elapsed_s": time.monotonic()-started})
                raise
            if isinstance(result.get("data"), dict):
                result["data"].pop("password", None)
            save(command, {"response": result, "elapsed_s": time.monotonic()-started})
            return result.get("data", result)

        async def snapshot(label, expected_state=None):
            status = await query("AT+GSTAT")
            if expected_state is not None:
                assert status["state"] == expected_state, status
                assert status["recording"] is (expected_state == "RECORDING"), status
            stats = await query("AT+RES")
            assert stats["build"] == "runtime-0.1.2"
            save("snapshot", {"label": label, "status": status, "resources": stats})
            return stats

        async def reboot():
            nonlocal client, connection
            status = await query("AT+GSTAT")
            assert status["state"] == "IDLE" and not status["recording"]
            await query("AT+REBOOT")
            await client.disconnect()
            await asyncio.sleep(4)
            client, connection = await connect()
            assert (await query("AT+USB?"))["status"] == "off"
            assert (await query("AT+LOG?"))["log"] == "off"
            assert (await query("AT+WIFI?"))["running"] is False
            await query("AT+TIME=" + str(int(time.time())))

        async def record(mode, seconds):
            started = await query("AT+START=" + mode)
            try:
                await asyncio.sleep(seconds / 2)
                await snapshot(mode + "_recording", "RECORDING")
                await asyncio.sleep(seconds / 2)
            finally:
                if client.is_connected:
                    await query("AT+STOP")
            await snapshot(mode + "_stopped", "IDLE")
            save("recording_session", {"mode": mode, "session": started["session"]})

        try:
            initial = await query("AT+GSTAT")
            assert initial["state"] == "IDLE" and not initial["recording"]
            original_mode = initial["mode"]
            assert (await query("AT+USB?"))["status"] == "off"
            assert (await query("AT+LOG?"))["log"] == "off"
            assert (await query("AT+WIFI?"))["running"] is False
            for name in ("A_fresh_boot", "B_normal", "C_enhanced", "D_enhanced_then_normal"):
                arm = name
                await reboot()
                await snapshot("boot")
                if name == "A_fresh_boot":
                    await asyncio.sleep(args.record_seconds)
                elif name == "B_normal":
                    await record("normal", args.record_seconds)
                else:
                    await record("enhanced", args.record_seconds)
                    if name == "D_enhanced_then_normal":
                        await record("normal", 5)
                await query("AT+MODE=" + original_mode)
                await asyncio.sleep(2)
                await snapshot("before_ap")
                try:
                    await query("AT+WIFI=on")
                except RuntimeError:
                    # Device rejection is an experimental outcome, not a script failure.
                    pass
                await snapshot("after_ap_attempt")
                running = (await query("AT+WIFI?"))["running"]
                save("arm_result", {"ap_running": running})
                if running:
                    await query("AT+WIFI=off")
                await snapshot("after_ap_cleanup")
            arm = "final"
            await reboot()
            await query("AT+MODE=" + original_mode)
            await snapshot("final_idle")
            save("complete", True)
        finally:
            if client.is_connected:
                # A timeout disconnects BLE; if that happens, stop and inspect state
                # with a new connection instead of risking stale command responses.
                state = await query("AT+GSTAT")
                if state["recording"]:
                    await query("AT+STOP")
                if (await query("AT+WIFI?"))["running"]:
                    await query("AT+WIFI=off")
                await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--record-seconds", type=int, default=15)
    args = parser.parse_args()
    if args.record_seconds < 10:
        parser.error("record-seconds must be at least 10")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
