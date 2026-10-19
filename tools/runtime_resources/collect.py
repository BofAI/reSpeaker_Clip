"""On-demand BLE resource sampling (Python 3.9+, pip install bleak).

Every output line is durable JSON; Ctrl-C disconnects. No recording, USB,
Wi-Fi, logging or mode setting is changed by this collector.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time

RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"


class Connection:
    def __init__(self, client):
        self.client = client
        self.queue = asyncio.Queue()
        self.buffer = ""
        self.lock = asyncio.Lock()

    def receive(self, _sender, payload):
        self.buffer += bytes(payload).decode("utf-8")
        if len(self.buffer) > 65536:
            raise ValueError("unterminated device JSON")
        while self.buffer.strip():
            self.buffer = self.buffer.lstrip()
            try:
                value, end = json.JSONDecoder().raw_decode(self.buffer)
            except json.JSONDecodeError:
                return
            self.buffer = self.buffer[end:]
            if isinstance(value, dict) and "event" not in value:
                self.queue.put_nowait(value)

    async def request(self, command):
        async with self.lock:
            await self.client.write_gatt_char(RX, command.encode(), response=False)
            try:
                result = await asyncio.wait_for(self.queue.get(), 20)
            except asyncio.TimeoutError:
                # No transaction IDs: late replies cannot be safely reused.
                await self.client.disconnect()
                raise
            if result.get("ok") is not True:
                raise RuntimeError(result)
            return result


async def collect(args):
    from bleak import BleakClient

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite earlier measurements.
    with args.output.open("x", buffering=1) as output:
        def save(kind, value):
            row = {"kind": kind, "label": args.label,
                   "host_time": datetime.now(timezone.utc).isoformat(), "value": value}
            output.write(json.dumps(row) + "\n")
            print(json.dumps(row), flush=True)

        async with BleakClient(args.address, timeout=30) as client:
            connection = Connection(client)
            await client.start_notify(TX, connection.receive)
            for command in ("AT+VERSION", "AT+GSTAT", "AT+USB?", "AT+LOG?"):
                save(command, await connection.request(command))
            save("prime", await connection.request("AT+RES"))
            deadline = time.monotonic()
            for _ in range(args.samples):
                deadline += args.interval
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                save("sample", await connection.request("AT+RES"))
            # Stack scans run after CPU sampling to avoid contaminating its window.
            if args.stacks:
                first = await connection.request("AT+RES=0")
                save("thread", first)
                for index in range(1, first["data"]["total"]):
                    save("thread", await connection.request(f"AT+RES={index}"))
            save("final_status", await connection.request("AT+GSTAT"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="unspecified")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--stacks", action="store_true")
    args = parser.parse_args()
    if args.samples < 1 or args.interval < 1:
        parser.error("samples must be positive and interval at least one second")
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()
