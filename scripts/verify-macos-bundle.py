#!/usr/bin/env python3
"""Reject nonportable dylib dependencies before producing a macOS installer."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

MACHO = {
    bytes.fromhex(v)
    for v in (
        "cffaedfe",
        "feedfacf",
        "cefaedfe",
        "feedface",
        "cafebabe",
        "bebafeca",
        "cafebabf",
        "bfbafeca",
    )
}


def audit(bundle):
    bundle = Path(bundle).resolve()
    report = {"bundle": bundle.name, "mach_o_files": 0, "errors": []}
    if not (bundle / "Contents/MacOS").is_dir():
        report["errors"].append({"error": "Missing app executable directory"})
        return report
    for binary in sorted(bundle.rglob("*")):
        if binary.is_symlink() or not binary.is_file():
            continue
        with binary.open("rb") as source:
            if source.read(4) not in MACHO:
                continue
        report["mach_o_files"] += 1
        result = subprocess.run(
            ["otool", "-arch", "all", "-L", str(binary)],
            capture_output=True,
            text=True,
            check=True,
        )
        for line in result.stdout.splitlines():
            if not line.startswith("\t"):
                continue
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith(("/System/Library/", "/usr/lib/")):
                continue
            candidates = []
            if dependency.startswith("@rpath/"):
                suffix = dependency.removeprefix("@rpath/")
                candidates = [
                    bundle / "Contents/Frameworks" / suffix,
                    binary.parent / suffix,
                ]
            elif dependency.startswith("@loader_path/"):
                candidates = [binary.parent / dependency.removeprefix("@loader_path/")]
            elif dependency.startswith("@executable_path/"):
                candidates = [
                    bundle
                    / "Contents/MacOS"
                    / dependency.removeprefix("@executable_path/")
                ]
            # Absolute developer-machine paths are forbidden even if they exist here.
            if not any(
                p.exists() and p.resolve().is_relative_to(bundle) for p in candidates
            ):
                report["errors"].append(
                    {
                        "binary": str(binary.relative_to(bundle)),
                        "dependency": dependency,
                    }
                )
    if report["mach_o_files"] == 0:
        report["errors"].append({"error": "No Mach-O binaries found"})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    report = audit(args.bundle)
    print(json.dumps(report, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
