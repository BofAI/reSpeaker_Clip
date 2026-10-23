"""USB AT test client for macOS/Linux. Does not flash firmware or print secrets."""
import argparse
import json
import os
from pathlib import Path
import re
import select
import termios
import time
import tty


def load_config(path):
    """Read private configuration and validate it before sending any AT commands."""
    if path.stat().st_mode & 0o077:
        raise ValueError("Configuration must be private (mode 0600)")
    config = json.loads(path.read_text())
    keys = ("ssid", "password", "host", "device", "token")
    if not isinstance(config, dict) or any(
            not isinstance(config.get(key), str) or
            any(c in config[key] for c in "\r\n\0") for key in keys):
        raise ValueError("Invalid configuration value")
    if (not 1 <= len(config["ssid"].encode()) <= 32 or "," in config["ssid"] or
            not 8 <= len(config["password"].encode()) <= 63 or
            not re.fullmatch(r"[A-Za-z0-9.-]{1,95}", config["host"]) or
            not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", config["device"]) or
            not re.fullmatch(r"[A-Za-z0-9_-]{32,96}", config["token"])):
        raise ValueError("Configuration does not fit the device's AT interface")
    port = config.get("port")
    if (isinstance(port, bool) or not isinstance(port, (int, str)) or
            not re.fullmatch(r"[0-9]{1,5}", str(port)) or not 1 <= int(port) <= 65535):
        raise ValueError("Port must be 1..65535")
    config["port"] = int(port)
    return config


def configure_upload(query, config):
    """Configure the upload through the caller's normal command/logging path."""
    query(f"AT+NETCFG={config['ssid']},{config['password']}")
    query(f"AT+UPLOADCFG={config['host']},{config['port']},{config['device']},{config['token']}")
    query(f"AT+UPTIME={int(time.time())}")


def exchange(fd, command, *, allow_rejected=False):
    payload = (command + "\r\n").encode()
    if len(payload) > 255:
        raise ValueError("command exceeds firmware limit")
    offset = 0
    while offset < len(payload):
        offset += os.write(fd, payload[offset:])
    end = time.monotonic() + 10
    response = b""
    while time.monotonic() < end:
        if not select.select([fd], [], [], 0.2)[0]:
            continue
        data = os.read(fd, 1024)
        if not data:
            raise RuntimeError("USB disconnected")
        response += data
        if len(response) > 4096:
            raise RuntimeError("response too large")
        while b"\n" in response:
            line, response = response.split(b"\n", 1)
            try:
                result = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if (not isinstance(result, dict) or "event" in result
                    or type(result.get("ok")) is not bool):
                continue
            if not result["ok"] and not allow_rejected:
                raise RuntimeError(f"device rejected command: {result}")
            return result
    raise TimeoutError("AT response timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True)
    parser.add_argument("--config", type=Path, help="private JSON: ssid,password,host,port,device,token")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--upload", help="session_id:positive_segment_number")
    mode.add_argument("--cancel", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config) if args.config else None
    fd = os.open(args.port, os.O_RDWR | os.O_NOCTTY)
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        options = termios.tcgetattr(fd)
        options[4] = options[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, options)
        termios.tcflush(fd, termios.TCIFLUSH)
        if config is not None:
            configure_upload(lambda command: exchange(fd, command), config)
        command = "AT+UPLOAD?"
        if args.upload:
            if any(c in args.upload for c in "\r\n\0"):
                raise ValueError("invalid upload selector")
            command = "AT+UPLOAD=" + args.upload
        elif args.cancel:
            command = "AT+UPLOADCANCEL"
        print(json.dumps(exchange(fd, command), ensure_ascii=False))
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)
        os.close(fd)


if __name__ == "__main__":
    main()
