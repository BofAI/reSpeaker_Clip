"""Query or atomically set a RAM-only transport profile on a diagnostic Clip."""
import argparse
import json

from device import exchange


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='USB serial port; otherwise select the sole Clip')
    parser.add_argument('--chunk-kib', type=int, choices=(16, 32, 64, 128, 8192),
                        help='8192 requires the upload-only whole-request experiment')
    parser.add_argument('--write-kib', type=int, choices=(2, 4))
    parser.add_argument('--send-bytes', type=int,
                        choices=(4096, 5461, 8192, 10240, 12288, 16384, 32768, 49152, 65535),
                        help='values above 16384 require the upload-only image')
    parser.add_argument('--nodelay', type=int, choices=(0, 1))
    args = parser.parse_args()
    profile = (args.chunk_kib, args.write_kib, args.send_bytes, args.nodelay)
    if any(value is not None for value in profile) and any(value is None for value in profile):
        parser.error('set all four profile options together, or omit all to query')
    import serial
    from serial.tools import list_ports

    port = args.serial
    if not port:
        ports = [p.device for p in list_ports.comports() if p.vid == 0x2886 and p.pid == 0x0069]
        if len(ports) != 1:
            parser.error('connect one Clip in normal mode or specify --serial')
        port = ports[0]
    command = 'AT+UPLOADTUNE?'
    if args.chunk_kib is not None:
        command = f'AT+UPLOADTUNE={args.chunk_kib * 1024},{args.write_kib * 1024},{args.send_bytes},{args.nodelay}'
    with serial.Serial(port, 115200, timeout=10, exclusive=True) as connection:
        response = exchange(connection.fileno(), command, allow_rejected=True)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    if not response.get('ok'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
