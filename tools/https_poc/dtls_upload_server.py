"""Single-owner durable DTLS recording receiver; data directory is exclusive."""
import argparse
import fcntl
import json
from pathlib import Path
import re
import select
import socket
import struct
import threading
import time

from dtls_server_transport import DatagramTLS, make_context
from dtls_upload_protocol import ERROR, IDENTIFIER, frame, unpack
from dtls_upload_session import UploadSession
from resume_store import ResumeStore, ResumeError


def load_identities(path):
    values = json.loads(path.read_text())
    if not isinstance(values, dict) or not values:
        raise ValueError('empty_device_registry')
    for name, token in values.items():
        if (not isinstance(name, str) or not IDENTIFIER.fullmatch(name) or
                not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,96}', token, re.ASCII)):
            raise ValueError('invalid_device_registry')
    return values


def run_session(channel, session):
    deadline = time.monotonic() + 120
    opened_at = time.monotonic()
    while time.monotonic() < deadline:
        try:
            packet = channel.read(.05)
        except TimeoutError:
            for response in session.tick():
                channel.write(response)
            if session.writer is None and time.monotonic() - opened_at > 10:
                raise TimeoutError('authentication_deadline')
            continue
        _, _, transfer, _ = unpack(packet)
        try:
            responses = session.feed(packet)
        except ResumeError as error:
            channel.write(frame(ERROR, 0, transfer, struct.pack('!H', error.code)))
            raise
        for response in responses:
            channel.write(response)
        if responses or (session.writer and session.transfer == transfer):
            deadline = time.monotonic() + 120
    raise TimeoutError('session_idle_deadline')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='certificate root, private/server-chain.pem and server.key')
    parser.add_argument('--directory', type=Path, required=True, help='exclusive DTLS data directory, separate from HTTP writers')
    parser.add_argument('--identities', type=Path, required=True, help='private JSON device-to-token registry; never logged')
    parser.add_argument('--port', type=int, default=9527)
    args = parser.parse_args()
    identities = load_identities(args.identities)
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    from OpenSSL import SSL
    context = make_context(args.root)
    with (args.directory / '.receiver.lock').open('a') as owner, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = ResumeStore(args.directory, threading.Lock())
        sock.bind(('0.0.0.0', args.port)); sock.setblocking(False)
        print(json.dumps(dict(event='dtls_upload_listening', port=args.port, protocol=3)), flush=True)
        while True:
            if not select.select([sock], [], [], .5)[0]:
                continue
            packet, peer = sock.recvfrom(2048, socket.MSG_PEEK)
            if len(packet) < 25 or packet[0] != 22 or packet[3:5] != b'\0\0' or packet[13] != 1:
                sock.recvfrom(65535); continue
            channel = DatagramTLS(sock, peer, context)
            session = UploadSession(store, identities)
            error = None
            try:
                channel.handshake()
                run_session(channel, session)
            except SSL.ZeroReturnError:
                pass
            except (SSL.Error, OSError, ValueError, TimeoutError, ResumeError) as caught:
                # Never include OPEN bodies, registry values or OpenSSL's
                # potentially data-bearing exception text in service logs.
                error = type(caught).__name__
            finally:
                session.close(); channel.close()
            writer = session.writer
            print(json.dumps(dict(event='dtls_upload_session_end', error=error,
                                  object_id=writer.identity['object_id'] if writer else None,
                                  durable=writer.durable if writer else 0,
                                  received=writer.received if writer else 0,
                                  complete=writer.stored if writer else False)), flush=True)


if __name__ == '__main__':
    main()
