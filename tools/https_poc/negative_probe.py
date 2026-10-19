"""Test certificate-time rejection, early cancellation and recovery on hardware.

Run hardware_probe.py once first to configure Wi-Fi and the trusted receiver.
Requires pyserial. Does not record, flash or delete files. Restores wall time.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from device import exchange


def run(args):
    import serial

    if not args.session.isascii() or not args.session.isdecimal() or len(args.session) != 14:
        raise ValueError("Session must contain 14 decimal digits")
    if args.config.stat().st_mode & 0o077:
        raise ValueError("Configuration must be private (mode 0600)")
    config = json.loads(args.config.read_text())
    reference = args.reference.read_bytes()
    object_id = hashlib.sha256(f"{config['device']}/{args.session}/1".encode()).hexdigest()
    target = args.received / (object_id + ".bin")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = []
    with os.fdopen(os.open(args.output / "measurements.jsonl",
                          os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as log:
        with serial.Serial(args.port, 115200, timeout=10, write_timeout=10,
                           exclusive=True) as device:
            def query(command):
                value = exchange(device.fileno(), command)
                # Never persist command arguments, which can contain credentials.
                log.write(json.dumps({"command": command.split("=", 1)[0],
                                      "value": value}) + "\n")
                log.flush()
                return value

            def finish(label):
                end = time.monotonic() + 90
                while True:
                    status = query("AT+UPLOAD?")
                    if not status["active"]:
                        resource = query("AT+RES")["data"]
                        result = {"test": label, "status": status, "resources": resource}
                        results.append(result)
                        print(json.dumps(result), flush=True)
                        return status
                    if time.monotonic() >= end:
                        raise TimeoutError(label)
                    time.sleep(.2)

            def start():
                query(f"AT+UPLOAD={args.session}:1")

            state = query("AT+GSTAT")["data"]
            if state["state"] != "IDLE" or state["recording"]:
                raise RuntimeError("Device must be idle")
            try:
                query("AT+UPTIME=1700000000")
                start()
                status = finish("certificate_not_yet_valid")
                if status["stage"] != "failed" or status["sent"] != 0:
                    raise RuntimeError("Invalid certificate time was not rejected")
                query("AT+UPTIME=" + str(int(time.time())))
                for attempt in range(args.cancel_repeat):
                    start()
                    query("AT+UPLOADCANCEL")
                    status = finish(f"early_cancel_{attempt + 1}")
                    if status["stage"] != "cancelled":
                        raise RuntimeError("Cancellation did not finish")
                start()
                status = finish("trusted_upload_after_failure_and_cancel")
                if status["stage"] != "succeeded" or status["sent"] != len(reference):
                    raise RuntimeError("Upload did not recover")
                if target.read_bytes() != reference:
                    raise RuntimeError("Received file differs from reference")
                results[-1]["byte_for_byte_match"] = True
                state = query("AT+GSTAT")["data"]
                if state["state"] != "IDLE" or state["recording"]:
                    raise RuntimeError("Device did not return to idle")
            finally:
                # Preserve collected evidence even if the device stops responding.
                try:
                    if query("AT+UPLOAD?")["active"]:
                        query("AT+UPLOADCANCEL")
                        finish("final_cleanup")
                    query("AT+UPTIME=" + str(int(time.time())))
                finally:
                    with os.fdopen(os.open(args.output / "summary.json",
                                           os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as summary:
                        json.dump(results, summary, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--received", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cancel-repeat", type=int, default=10, choices=range(1, 101))
    run(parser.parse_args())


if __name__ == "__main__":
    main()
