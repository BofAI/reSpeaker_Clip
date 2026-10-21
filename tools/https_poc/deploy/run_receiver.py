"""Single-process HTTPS receiver for a controlled Internet pilot.

Uses the existing protocol/storage implementation, with bounded connections.
Certificates and bearer credentials are external to the source checkout.
"""
import argparse
from pathlib import Path
import re
import ssl
import threading

from server import Receiver


class BoundedReceiver(Receiver):
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=9527)
    parser.add_argument('--device', required=True)
    args = parser.parse_args()
    token = (args.root / 'private/token').read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,96}', token):
        parser.error('invalid bearer token file')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', args.device):
        parser.error('invalid device identity')
    server = BoundedReceiver(('0.0.0.0', args.port), args.root / 'data', args.device, token)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.root / 'private/server-chain.pem', args.root / 'private/server.key')
    server.socket = context.wrap_socket(server.socket, server_side=True,
                                      do_handshake_on_connect=False)
    print(f'HTTPS receiver ready on TCP {args.port}; connection limit 16', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
