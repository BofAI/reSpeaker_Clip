"""Plain UDP adapter for the temporary synthetic receive benchmark only."""
import hashlib
import hmac
import select
import socket
import struct
import time
from types import SimpleNamespace

from dtls_bench_receiver import START, ACK, frame
from dtls_down_server import HEADER, PULL, unpack_frame
SIZE = 4 * 1024 * 1024

MARKER = 'clip-udp-diagnostic-not-a-secret-v1'


def encode(packet, tag):
    unpack_frame(packet)
    if len(tag) != 7:
        raise ValueError('invalid_session_tag')
    return tag[:4] + packet[4:5] + tag[4:] + packet[8:]


def decode(packet, tag):
    if len(packet) < HEADER.size or len(packet) > HEADER.size + 1024:
        raise ValueError('invalid_datagram_size')
    if len(tag) != 7 or packet[:4] != tag[:4] or packet[5:8] != tag[4:]:
        raise ValueError('wrong_session')
    normalized = b'CDB1' + packet[4:5] + b'\0' * 3 + packet[8:]
    unpack_frame(normalized)
    return normalized


def parse_start(packet):
    if len(packet) < HEADER.size + 44:
        raise ValueError('short_start')
    nonce = packet[HEADER.size:HEADER.size + 8]
    normalized = decode(packet, nonce[:7])
    kind, offset, body = unpack_frame(normalized)
    if kind != START or offset != 4 or len(body) != 44 + len(MARKER) or body[44:] != MARKER.encode():
        raise ValueError('invalid_plain_start')
    expected = hashlib.sha256(bytes(range(256)) * (SIZE // 256)).digest()
    if struct.unpack('!I', body[8:12])[0] != SIZE or body[12:44] != expected:
        raise ValueError('pattern_mismatch')
    return nonce, frame(START, 3, body)


class PlainUDP:
    def __init__(self, sock, peer, nonce, key, start):
        self.sock, self.peer, self.tag = sock, peer, nonce[:7]
        self.cookie = hmac.new(key, repr(peer).encode() + nonce, 'sha256').digest()
        self.start = start
        self.verified = False
        self.connection = SimpleNamespace(pending=lambda: 0)

    def challenge(self):
        self.write(frame(ACK, 0xffffffff, b'\0' * 4 + self.cookie))

    def read(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([self.sock], [], [], max(0, end - time.monotonic()))[0]:
                break
            packet, peer = self.sock.recvfrom(65535)
            if peer != self.peer:
                continue
            try:
                normalized = decode(packet, self.tag)
                kind, offset, body = unpack_frame(normalized)
            except ValueError:
                continue
            if kind == START:
                if normalized == frame(START, 4, self.start[HEADER.size:]):
                    self.challenge()
                continue
            if kind == PULL:
                if offset or not hmac.compare_digest(body, self.cookie):
                    continue
                self.verified = True
                return frame(PULL, 0)
            if self.verified:
                return normalized
        raise TimeoutError('UDP receive deadline')

    def write(self, packet):
        wire = encode(packet, self.tag)
        end = time.monotonic() + 5
        while True:
            try:
                if self.sock.sendto(wire, self.peer) != len(wire):
                    raise OSError('short_datagram')
                return
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError('UDP send deadline')
                select.select([], [self.sock], [], .01)

    def close(self):
        pass
