"""Single-owner durable DTLS recording receiver; data directory is exclusive."""
import argparse
import fcntl
import json
from pathlib import Path
import re
import select
import queue
from contextlib import nullcontext
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


def run_session(channel, session, guard=None, stopped=None):
    guard = guard if guard is not None else nullcontext()
    stopped = stopped if stopped is not None else threading.Event()
    deadline = time.monotonic() + 120
    opened_at = time.monotonic()
    while time.monotonic() < deadline:
        if stopped.is_set():
            raise OSError("session_superseded")
        try:
            packet = channel.read(.05)
        except TimeoutError:
            with guard:
                if stopped.is_set():
                    raise OSError("session_superseded")
                responses = session.tick()
            for response in responses:
                channel.write(response)
            if session.writer is None and time.monotonic() - opened_at > 10:
                raise TimeoutError('authentication_deadline')
            continue
        _, _, transfer, _ = unpack(packet)
        try:
            with guard:
                if stopped.is_set():
                    raise OSError("session_superseded")
                responses = session.feed(packet)
        except ResumeError as error:
            channel.write(frame(ERROR, 0, transfer, struct.pack('!H', error.code)))
            raise
        for response in responses:
            channel.write(response)
        if responses or (session.writer and session.transfer == transfer):
            deadline = time.monotonic() + 120
    raise TimeoutError('session_idle_deadline')


def is_client_hello(packet):
    return (25 <= len(packet) <= 2048 and packet[0] == 22 and
            packet[1:3] in (b'\xfe\xfd', b'\xfe\xff') and
            packet[3:5] == b'\0\0' and packet[13] == 1 and
            13 + int.from_bytes(packet[11:13], 'big') <= len(packet))


class Peer:
    def __init__(self, receiver, peer, previous=None):
        self.receiver, self.peer, self.previous = receiver, peer, previous
        self.inbox = queue.Queue(maxsize=128)
        self.stopped = threading.Event()
        self.established = False
        self.device = None
        self.session = UploadSession(receiver.store, receiver.identities,
                                     lambda device: receiver.claim(self, device))
        self.thread = threading.Thread(target=receiver.serve, args=(self,), daemon=True)

    def receive(self, timeout):
        if self.stopped.is_set():
            raise OSError('session_superseded')
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None


class Receiver:
    """One UDP reader, bounded TLS workers, one authenticated owner per device.

    Session state and writer handover share a lock. A new authenticated OPEN
    closes the old writer before loading its durable checkpoint; untrusted
    handshakes cannot evict an authenticated writer.
    """
    def __init__(self, sock, context, store, identities, max_peers=8):
        self.sock, self.context, self.store, self.identities = sock, context, store, identities
        self.max_peers = max_peers
        self.guard = threading.RLock()
        self.routes, self.workers, self.owners = {}, set(), {}

    def dispatch(self, packet, peer):
        if len(packet) > 2048:
            return
        with self.guard:
            client = self.routes.get(peer)
            # A new DTLS association may reuse the UDP endpoint. Keep the old
            # writer until the candidate proves its device token in OPEN.
            if client is None or (client.established and is_client_hello(packet)):
                if not is_client_hello(packet) or len(self.workers) >= self.max_peers:
                    return
                client = Peer(self, peer, client)
                self.workers.add(client)
                self.routes[peer] = client
                client.inbox.put_nowait(packet)
                client.thread.start()
                return
            try:
                client.inbox.put_nowait(packet)
            except queue.Full:
                pass  # Bounded UDP loss; DTLS/application retransmission handles it.

    def claim(self, client, device):
        # Called by feed() under guard, after checking the device token and
        # before opening or recovering any file.
        with self.guard:
            if client.stopped.is_set():
                raise OSError('session_superseded')
            if client.device is not None and client.device != device:
                raise ResumeError(409, 'connection_device_conflict')
            old = self.owners.get(device)
            if old is not None and old is not client:
                old.stopped.set()
                old.session.close()
            self.owners[device] = client
            client.device = device

    def serve(self, client):
        from OpenSSL import SSL
        channel = None
        error = None
        try:
            channel = DatagramTLS(self.sock, client.peer, self.context, client.receive)
            channel.handshake()
            with self.guard:
                client.established = True
            run_session(channel, client.session, self.guard, client.stopped)
        except SSL.ZeroReturnError:
            pass
        except (SSL.Error, OSError, ValueError, TimeoutError, ResumeError) as caught:
            error = type(caught).__name__
        finally:
            with self.guard:
                client.stopped.set()
                client.session.close()
            if channel is not None:
                channel.close()
            with self.guard:
                if self.routes.get(client.peer) is client:
                    previous = client.previous
                    if previous in self.workers and not previous.stopped.is_set():
                        self.routes[client.peer] = previous
                    else:
                        self.routes.pop(client.peer, None)
                self.workers.discard(client)
                for worker in self.workers:
                    if worker.previous is client:
                        worker.previous = None
                client.previous = None
                for device, owner in list(self.owners.items()):
                    if owner is client:
                        del self.owners[device]
            writer = client.session.writer
            # Never log OPEN bodies, tokens or exception text.
            print(json.dumps(dict(event='dtls_upload_session_end', error=error,
                                  object_id=writer.identity['object_id'] if writer else None,
                                  durable=writer.durable if writer else 0,
                                  received=writer.received if writer else 0,
                                  complete=writer.stored if writer else False)), flush=True)

    def close(self):
        with self.guard:
            clients = list(self.workers)
            for client in clients:
                client.stopped.set()
        for client in clients:
            client.thread.join(timeout=6)


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
        receiver = Receiver(sock, context, store, identities)
        try:
            while True:
                if select.select([sock], [], [], .5)[0]:
                    packet, peer = sock.recvfrom(65535)
                    receiver.dispatch(packet, peer)
        finally:
            receiver.close()


if __name__ == '__main__':
    main()
