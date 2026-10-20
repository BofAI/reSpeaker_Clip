"""Upload a known recording and collect resources; never log credentials.

Requires pyserial. Does not flash, delete recordings, or alter filesystem layout.
Each attempt creates a new device upload job; verify TLS resumption separately
before describing repeated jobs as cold handshakes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from device import configure_upload, exchange, load_config


def run(args):
    import serial

    config = load_config(args.config)
    if not args.session.isascii() or not args.session.isdecimal() or len(args.session) != 14:
        raise ValueError("Session must contain 14 decimal digits")
    reference = args.reference.read_bytes()
    expected = hashlib.sha256(reference).hexdigest()
    object_id = hashlib.sha256(f"{config['device']}/{args.session}/1".encode()).hexdigest()
    target = args.received / (object_id + ".bin")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with os.fdopen(os.open(args.output / "measurements.jsonl",
                          os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as log:
        phase = "startup"

        def save(kind, value):
            row = {"phase": phase, "kind": kind, "monotonic": time.monotonic(), "value": value}
            log.write(json.dumps(row) + "\n")
            log.flush()
            if kind != "thread":
                print(json.dumps(row), flush=True)

        with serial.Serial(args.port, 115200, timeout=10, write_timeout=10, exclusive=True) as device:
            fd = device.fileno()

            def query(command):
                # Arguments may contain a password or bearer token.
                label = command.split("=", 1)[0]
                save("request", label)
                response = exchange(fd, command)
                save(label, response)
                return response

            def stacks():
                first = exchange(fd, "AT+RES=0")["data"]
                save("thread", first)
                for index in range(1, first["total"]):
                    save("thread", exchange(fd, f"AT+RES={index}")["data"])

            query("AT+VERSION")
            state = query("AT+GSTAT")["data"]
            if state["state"] != "IDLE" or state["recording"]:
                raise RuntimeError("Device must be idle")
            query("AT+RES")
            query("AT+LIST=" + args.session)
            configure_upload(query, config)
            stacks()
            results = []
            active = False
            try:
                for attempt in range(args.repeat):
                    phase = f"upload_{attempt+1}"
                    started = time.monotonic()
                    query(f"AT+UPLOAD={args.session}:1")
                    active = True
                    while True:
                        resource = query("AT+RES")["data"]
                        status = query("AT+UPLOAD?")
                        if not status["active"]:
                            active = False
                            # The sample before UPLOAD? may precede cleanup by
                            # a few milliseconds. Measure again after inactive.
                            resource = query("AT+RES")["data"]
                            break
                        if time.monotonic() - started > 330:
                            raise TimeoutError("Upload exceeded its device deadline")
                        time.sleep(.4)
                    result = {"attempt": attempt+1, "status": status,
                              "elapsed_s": time.monotonic()-started,
                              "after_resources": resource}
                    for option, command, count in (("netdiag", "NETDIAG", 4),
                                                   ("nettrace", "NETTRACE", 4),
                                                   ("nettx", "NETTX", 3)):
                        if getattr(args, option, False):
                            result[option] = [query(f"AT+{command}={index}")["data"]
                                              for index in range(count)]
                    if status["stage"] == "succeeded":
                        actual = target.read_bytes()
                        if actual != reference:
                            raise ValueError("Received recording differs from known reference")
                        result.update(received_bytes=len(actual), sha256=expected,
                                      byte_for_byte_match=True)
                    results.append(result)
                    save("attempt_result", result)
                    stacks()
                    if status["stage"] != "succeeded":
                        break
                phase = "final"
                query("AT+GSTAT")
                query("AT+RES")
                summary = {"session": args.session, "reference_sha256": expected,
                           "results": results, "all_succeeded": len(results) == args.repeat and
                           all(r["status"]["stage"] == "succeeded" for r in results)}
                with os.fdopen(os.open(args.output / "summary.json",
                                      os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                    json.dump(summary, file, indent=2)
                    file.write("\n")
                if not summary["all_succeeded"]:
                    raise RuntimeError("Device upload failed; inspect saved status/resources")
            finally:
                if active:
                    query("AT+UPLOADCANCEL")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--received", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--netdiag", action="store_true",
                        help="collect cached network snapshots from a netdiag image")
    parser.add_argument("--nettrace", action="store_true",
                        help="collect cached ARP/driver snapshots from an instrumented image")
    parser.add_argument("--nettx", action="store_true",
                        help="collect the first three cached ARP firmware-command records")
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100:
        parser.error("--repeat must be 1..100")
    run(args)


if __name__ == "__main__":
    main()
