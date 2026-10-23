"""DTLS reconnects must not wait for an abandoned session's idle deadline."""
import hashlib
import importlib.util
import queue
from pathlib import Path
import select
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from dtls_upload_server import Receiver, is_client_hello
from dtls_server_transport import DatagramTLS, make_context
from dtls_upload_protocol import OPEN, DATA, QUERY, ACK, COMPLETE, ERROR, frame, unpack, open_body, RECEIPT
from make_credentials import generate
from resume_store import ResumeStore


def hello():
    return bytes([22, 254, 253, 0, 0]) + bytes(6) + b'\0\x0c' + b'\x01' + bytes(11)


class DispatchTest(unittest.TestCase):
    def test_foreign_peer_gets_its_own_bounded_queue(self):
        receiver = Receiver(None, None, None, {}, max_peers=2)
        release = threading.Event()
        def serve(client):
            release.wait(2)
        with patch.object(receiver, 'serve', side_effect=serve):
            try:
                self.assertTrue(is_client_hello(hello()))
                receiver.dispatch(b'invalid', ('127.0.0.1', 1))
                self.assertFalse(receiver.workers)
                receiver.dispatch(hello(), ('127.0.0.1', 1))
                receiver.dispatch(hello(), ('127.0.0.1', 2))
                a, b = [receiver.routes[('127.0.0.1', n)] for n in (1, 2)]
                self.assertEqual(a.inbox.get_nowait(), hello())
                self.assertEqual(b.inbox.get_nowait(), hello())
                receiver.dispatch(b'new-peer-data', b.peer)
                self.assertEqual(b.inbox.get_nowait(), b'new-peer-data')
                self.assertTrue(a.inbox.empty())
                receiver.dispatch(hello(), ('127.0.0.1', 3))
                self.assertEqual(len(receiver.workers), 2)
                for _ in range(150):
                    receiver.dispatch(b'bounded', b.peer)
                self.assertEqual(b.inbox.qsize(), 128)
                receiver.dispatch(bytes(2049), a.peer)
                self.assertTrue(a.inbox.empty())
            finally:
                release.set()
                receiver.close()


@unittest.skipUnless(importlib.util.find_spec('OpenSSL'), 'pyOpenSSL DTLS is tested on receiver host')
class RealDTLSReconnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cert, key = generate(self.root / 'private')
        (self.root / 'private/server-chain.pem').write_bytes(cert.read_bytes())
        self.cert = cert
        self.token = 'x' * 32
        self.body = bytes(n % 251 for n in range(8192 + 13))
        self.sha = hashlib.sha256(self.body).digest()
        self.open = open_body('clip', '20261023000000', 1, len(self.body), self.sha, self.token)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.setblocking(False)
        (self.root / 'data').mkdir()
        self.receiver = Receiver(self.sock, make_context(self.root),
                                 ResumeStore(self.root / 'data', threading.Lock()), {'clip': self.token})
        self.done = threading.Event()
        self.clients = []
        self.thread = threading.Thread(target=self.dispatch, daemon=True)
        self.thread.start()

    def dispatch(self):
        while not self.done.is_set():
            if select.select([self.sock], [], [], .02)[0]:
                packet, peer = self.sock.recvfrom(65535)
                self.receiver.dispatch(packet, peer)

    def tearDown(self):
        self.done.set()
        self.thread.join(2)
        self.receiver.close()
        for channel in self.clients:
            channel.sock.close()  # Intentionally no DTLS close notification.
        self.sock.close()
        self.tmp.cleanup()

    def client(self, sock=None):
        from OpenSSL import SSL
        context = SSL.Context(SSL.DTLS_CLIENT_METHOD)
        context.set_min_proto_version(0xfefd)
        context.set_max_proto_version(0xfefd)
        context.set_cipher_list(b'ECDHE-ECDSA-AES128-GCM-SHA256')
        context.load_verify_locations(str(self.cert))
        context.set_verify(SSL.VERIFY_PEER, lambda conn, cert, errno, depth, ok: ok)
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('127.0.0.1', 0))
            sock.setblocking(False)
        channel = DatagramTLS(sock, self.sock.getsockname(), context)
        channel.connection.set_connect_state()
        channel.handshake()
        self.clients.append(channel)
        return channel

    def request(self, channel, kind, offset=0, body=b'', cid=b'request1'):
        channel.write(frame(kind, offset, cid, body))
        packet = channel.read(3)
        return unpack(packet)

    def test_abandoned_partial_session_can_resume_without_waiting_120_seconds(self):
        a = self.client()
        self.request(a, OPEN, body=self.open)
        a.write(frame(DATA, 0, b'request1', self.body[:1024]))
        # QUERY forces a durable checkpoint. A stays alive and sends no close.
        reply = self.request(a, QUERY)
        while reply[1] < 1024:
            reply = unpack(a.read(3))
        start = time.monotonic()
        b = self.client()
        resumed = self.request(b, OPEN, body=self.open, cid=b'request2')
        self.assertEqual(resumed[1], 1024)
        self.assertLess(time.monotonic() - start, 5)
        for offset in range(1024, len(self.body), 1024):
            b.write(frame(DATA, offset, b'request2', self.body[offset:offset + 1024]))
        end = time.monotonic() + 5
        while True:
            reply = unpack(b.read(3))
            if reply[0] == COMPLETE:
                break
            self.assertLess(time.monotonic(), end)
        files = list((self.root / 'data').glob('*.bin'))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), self.body)

    def test_unauthorized_connection_does_not_evict_the_old_writer(self):
        a = self.client()
        self.request(a, OPEN, body=self.open)
        b = self.client()
        bad = open_body('clip', '20261023000000', 1, len(self.body), self.sha, 'z' * 32)
        self.assertEqual(self.request(b, OPEN, body=bad)[0], ERROR)
        self.assertEqual(self.request(a, QUERY)[0], ACK)
        with self.receiver.guard:
            owner = self.receiver.owners['clip']
            self.assertFalse(owner.stopped.is_set())

    def test_reconnect_can_reuse_the_same_udp_endpoint(self):
        a = self.client()
        self.request(a, OPEN, body=self.open)
        b = self.client(a.sock)
        self.assertEqual(self.request(b, OPEN, body=self.open, cid=b'request2')[0], ACK)
        self.assertEqual(self.request(b, QUERY, cid=b'request2')[0], ACK)
        end = time.monotonic() + 2
        while time.monotonic() < end:
            with self.receiver.guard:
                if len(self.receiver.workers) == 1:
                    self.assertIsNone(self.receiver.owners['clip'].previous)
                    break
            time.sleep(.01)
        else:
            self.fail('Superseded TLS worker was retained')


if __name__ == '__main__':
    unittest.main()
