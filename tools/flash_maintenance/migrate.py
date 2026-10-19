"""Prepare and verify a smaller LittleFS plus original SDK patch entirely offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from littlefs import LittleFS, UserContext
from littlefs.errors import LittleFSError
from flash_tool import START, END, load_backup, private_write, sha

BLOCK = 65536
NEW_SIZE = 0x6b0000
PATCH_SHA256 = "6353a2d07319337a5aaa75af03f2303756496eda0e2bea53ef47bc2148f64e7f"
GEOMETRY = dict(block_size=BLOCK, read_size=16, prog_size=16, cache_size=64,
                lookahead_size=32, block_cycles=512, disk_version=0x20001)


class ReadOnly(UserContext):
    def prog(self, *_args): raise RuntimeError("Source filesystem is read-only")
    def erase(self, *_args): raise RuntimeError("Source filesystem is read-only")


def open_image(data, readonly):
    context = (ReadOnly if readonly else UserContext)(buffer=bytearray(data))
    fs = LittleFS(context=context, block_count=len(data)//BLOCK, mount=False, **GEOMETRY)
    fs.mount()  # Never use the constructor's auto-format-on-mount-failure path.
    stat = fs.fs_stat()
    if stat.block_size != BLOCK or stat.block_count != len(data)//BLOCK or stat.disk_version != 0x20001:
        raise ValueError("Unexpected filesystem geometry/version")
    return fs


def inventory(fs):
    entries = []
    for root, directories, files in fs.walk("/"):
        for name, kind in [(name, "directory") for name in directories] + [(name, "file") for name in files]:
            path = (root.rstrip("/")+"/"+name)
            if name in (".", "..") or "/" in name:
                raise ValueError("Unexpected filesystem entry")
            entries.append((path, kind))
    result = []
    for path, kind in [("/", "directory")] + sorted(entries):
        attributes = {}
        for typ in range(256):
            try: attributes[str(typ)] = fs.getattr(path, typ).hex()
            except LittleFSError as error:
                if error.code != -61: raise
        data = b""
        if kind == "file":
            with fs.open(path, "rb") as file: data = file.read()
        result.append({"path": path, "kind": kind, "size": len(data), "sha256": sha(data), "attributes": attributes})
    return result


def migrate(data):
    if len(data) != END-START: raise ValueError("Wrong original image size")
    source = open_image(data, True)
    before = inventory(source)
    # Model erased NOR so unused blocks stay 0xff and need no needless writes.
    target = LittleFS(context=UserContext(buffer=bytearray(b"\xff"*NEW_SIZE)),
                      block_count=NEW_SIZE//BLOCK, mount=False, **GEOMETRY)
    target.format(); target.mount()
    for entry in before:
        path = entry["path"]
        if path != "/" and entry["kind"] == "directory": target.makedirs(path, exist_ok=True)
    for entry in before:
        path = entry["path"]
        if entry["kind"] == "file":
            with source.open(path, "rb") as original, target.open(path, "wb") as output:
                output.write(original.read())
        for typ, value in entry["attributes"].items(): target.setattr(path, int(typ), bytes.fromhex(value))
    target.unmount(); source.unmount()
    image = bytes(target.context.buffer)
    check = open_image(image, True)
    after = inventory(check); check.unmount()
    if before != after: raise ValueError("File/directory/attribute inventory changed")
    return image, before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest, original = load_backup(args.backup)
    patch = args.patch.read_bytes()
    if len(patch) != 87046 or sha(patch) != PATCH_SHA256:
        raise ValueError("Patch is not the validated SDK nrf70.bin")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(args.output, 0o700)
    image, entries = migrate(original)
    combined = image + patch + b"\xff"*(0x20000-len(patch))
    private_write(args.output/"migrated.bin", combined)
    # Inventory contains no file contents, but attributes can be sensitive.
    report = {"device": manifest["device"], "before_sha256": sha(original),
              "after_sha256": sha(combined), "old_blocks": 109, "new_blocks": 107,
              "disk_version": "2.1", "inventory_equal_after_remount": True,
              "patch_sha256": sha(patch), "patch_offset": 0x7e0000, "entries": entries}
    private_write(args.output/"migration.json", (json.dumps(report, indent=2)+"\n").encode())
    print(json.dumps({"migration_verified": True, "entries": len(entries),
                      "old_blocks": 109, "new_blocks": 107, "patch_sha256": sha(patch)}))


if __name__ == "__main__":
    main()
