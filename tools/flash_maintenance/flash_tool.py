"""USB maintenance protocol. Backup is read-only; apply requires verified backup.

Never prints flash contents or configuration values. Backups contain secrets;
store the output directory privately and never commit it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import zlib

START = 0x130000
END = 0x800000
ERASE = 0x10000
CHUNK = 0x4000


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_write(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(data)


class Device:
    def __init__(self, port):
        import serial
        self.serial = serial.Serial(port, 115200, timeout=10, write_timeout=10, exclusive=True)
        self.info = self.identify()

    def close(self):
        self.serial.close()

    def line(self):
        value = self.serial.read_until(b"\n", 256)
        if not value.endswith(b"\n"):
            raise TimeoutError("Incomplete maintenance response; reconnect before retrying")
        text = value.decode("ascii").strip()
        if text.startswith("ERR "):
            raise RuntimeError(text)
        return text

    def command(self, text):
        data = (text + "\n").encode("ascii")
        if self.serial.write(data) != len(data):
            raise IOError("Partial command write")
        return self.line()

    def identify(self):
        parts = self.command("INFO").split()
        if len(parts) != 7 or parts[:2] != ["INFO", "1"]:
            raise ValueError("Not the expected flash-maintenance protocol")
        geometry = [int(value, 16) for value in parts[2:6]]
        if geometry != [END, START, ERASE, CHUNK]:
            raise ValueError("Unexpected device flash geometry")
        return {"protocol": 1, "flash_bytes": END, "write_start": START,
                "erase_bytes": ERASE, "chunk_bytes": CHUNK, "device_id": parts[6]}

    def read(self, offset, size):
        header = self.command(f"READ {offset:x} {size:x}").split()
        if len(header) != 3 or header[0] != "DATA" or int(header[1], 16) != size:
            raise ValueError("Invalid read frame")
        data = self.serial.read(size)
        if len(data) != size or zlib.crc32(data) & 0xffffffff != int(header[2], 16):
            raise ValueError("Read length/CRC mismatch; stop before any writes")
        return data

    def region(self):
        data = bytearray()
        for offset in range(START, END, CHUNK):
            data.extend(self.read(offset, min(CHUNK, END-offset)))
            if len(data) % 0x100000 == 0:
                print(json.dumps({"read_bytes": len(data)}), flush=True)
        return bytes(data)

    def write(self, offset, payload):
        crc = zlib.crc32(payload) & 0xffffffff
        if self.command(f"WRITE {offset:x} {len(payload):x} {crc:08x}") != "READY":
            raise ValueError("Write was not accepted")
        if self.serial.write(payload) != len(payload) or self.line() != "OK WRITE":
            raise IOError("Write verification failed")


def load_backup(directory):
    manifest = json.loads((directory / "backup.json").read_text())
    data = (directory / "backup.bin").read_bytes()
    if len(data) != END-START or sha(data) != manifest["sha256"] or not manifest["verified_read_twice"]:
        raise ValueError("Backup integrity/verification missing")
    return manifest, data


def backup(device, directory):
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(directory, 0o700)
    data = device.region()
    private_write(directory / "backup.bin", data)
    second = device.region()
    if second != data:
        raise ValueError("Two independent raw reads disagree")
    manifest = {"device": device.info, "start": START, "size": len(data),
                "sha256": sha(data), "verified_read_twice": True}
    private_write(directory / "backup.json", (json.dumps(manifest, indent=2)+"\n").encode())
    print(json.dumps({"backup_verified": True, "bytes": len(data), "sha256": sha(data)}))


def apply_image(device, backup_dir, image, report_path, *, restore=False, plan_path=None):
    if report_path.exists(): raise FileExistsError(report_path)
    if not report_path.parent.is_dir(): raise ValueError("Report directory must already exist")
    manifest, before = load_backup(backup_dir)
    if device.info != manifest["device"]:
        raise ValueError("Backup belongs to another device/geometry")
    target = before if restore else image.read_bytes()
    if len(target) != END-START:
        raise ValueError("Image must cover exactly the writable LFS/patch region")
    if not restore:
        if plan_path is None: raise ValueError("A verified migration plan is required")
        plan = json.loads(plan_path.read_text())
        if (plan.get("device") != device.info or plan.get("before_sha256") != sha(before)
                or plan.get("after_sha256") != sha(target)
                or plan.get("inventory_equal_after_remount") is not True
                or plan.get("old_blocks") != 109 or plan.get("new_blocks") != 107
                or plan.get("patch_offset") != 0x7e0000):
            raise ValueError("Migration plan does not match backup, device, or image")
    # Re-read before ARM, ensuring the device still matches its backup.
    current = device.region()
    if not restore and current != before:
        raise ValueError("Flash changed since backup; refuse to overwrite")
    if device.command("ARM LFS_PATCH_0x130000_0x800000") != "OK ARM":
        raise ValueError("Cannot arm writes")
    erased, chunks = [], 0
    started = time.monotonic()
    for relative in range(0, len(target), ERASE):
        block = target[relative:relative+ERASE]
        if block == current[relative:relative+ERASE]:
            continue
        offset = START+relative
        if device.command(f"ERASE {offset:x} {ERASE:x}") != "OK ERASE":
            raise IOError("Erase failed")
        erased.append(offset)
        for pos in range(0, ERASE, CHUNK):
            payload = block[pos:pos+CHUNK]
            if payload != b"\xff"*len(payload):
                device.write(offset+pos, payload)
                chunks += 1
        print(json.dumps({"verified_block": hex(offset), "blocks_changed": len(erased)}), flush=True)
    actual = device.region()
    if actual != target:
        raise ValueError("Final full-region readback differs; stay in maintenance and restore backup")
    report = {"device": device.info, "before_sha256": sha(before), "after_sha256": sha(actual),
              "full_readback_matches": True, "erased_offsets": erased, "written_chunks": chunks,
              "elapsed_s": time.monotonic()-started}
    private_write(report_path, (json.dumps(report, indent=2)+"\n").encode())
    print(json.dumps({"full_readback_matches": True, "sha256": sha(actual)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("info")
    b = sub.add_parser("backup")
    b.add_argument("--output", type=Path, required=True)
    a = sub.add_parser("apply")
    a.add_argument("--backup", type=Path, required=True)
    a.add_argument("--image", type=Path, required=True)
    a.add_argument("--plan", type=Path, required=True)
    a.add_argument("--report", type=Path, required=True)
    a.add_argument("--allow-write", action="store_true", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--backup", type=Path, required=True)
    r.add_argument("--report", type=Path, required=True)
    r.add_argument("--allow-write", action="store_true", required=True)
    args = parser.parse_args()
    device = Device(args.port)
    try:
        if args.action == "info": print(json.dumps(device.info))
        elif args.action == "backup": backup(device, args.output)
        elif args.action == "apply": apply_image(device, args.backup, args.image, args.report, plan_path=args.plan)
        else: apply_image(device, args.backup, None, args.report, restore=True)
    finally:
        device.close()


if __name__ == "__main__":
    main()
