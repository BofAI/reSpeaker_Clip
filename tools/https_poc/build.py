"""Reproducible app-only HTTPS overlay build in an isolated NCS build directory."""
import argparse
import os
from pathlib import Path
import subprocess

PRESETS = {
    "dtls-optimized": (
        "runtime.conf", "netdiag.conf", "https-connect-budget.conf",
        "https-wifi-no-stbc.conf", "https-arp-backport.conf", "dtls-upload.conf",
        "wifi-auth-log.conf", "dtls-aead-hardware.conf",
        "dtls-upload-optimized.conf", "dtls-profile.conf",
    ),
}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ncs", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--preset", choices=PRESETS,
                        help="named full-application candidate configuration")
    parser.add_argument("--extra-conf", type=Path, action="append", default=[],
                        help="additional app-only config, appended after https.conf")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    ca = args.ca.resolve()
    content = ca.read_text()
    if "BEGIN CERTIFICATE" not in content or "PRIVATE KEY" in content:
        parser.error("--ca must contain only public CA certificate(s)")
    build = args.build.resolve()
    if build.name in ("build-clip-prod", "build-clip-ext-patch"):
        parser.error("refusing to overwrite the baseline build")
    env = dict(os.environ, ZEPHYR_EXTRA_MODULES=str(repo),
               ZEPHYR_SDK_INSTALL_DIR=str(args.sdk.resolve()), CLIP_HTTPS_CA_FILE=str(ca))
    configs = [repo / "applications/clip/https.conf",
               *[repo / "applications/clip" / name for name in PRESETS.get(args.preset, ())],
               *[p.resolve() for p in args.extra_conf]]
    if any(not p.is_file() for p in configs):
        parser.error("each --extra-conf must be an existing file")
    command = ["west", "build", "--cmake", "--sysbuild", "-b", "clip/nrf5340/cpuapp",
               str(repo / "applications/clip"), "-d", str(build), "--",
               "-DSNIPPET=production", "-DSNIPPET_ROOT=" + str(repo / "applications/clip"),
               "-Dclip_EXTRA_CONF_FILE=" + ";".join(map(str, configs))]
    build.parent.mkdir(parents=True, exist_ok=True)
    log = build.parent / (build.name + ".log")
    with log.open("w") as output:
        result = subprocess.run(command, cwd=args.ncs, env=env, stdout=output, stderr=subprocess.STDOUT)
    print("\n".join(log.read_text().splitlines()[-70:]))
    print(f"Full build log: {log}")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
