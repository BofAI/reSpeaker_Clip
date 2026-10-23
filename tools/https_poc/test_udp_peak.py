import hashlib
import os
import socket
import struct
import unittest

from dtls_bench_receiver import START, DATA, ACK, frame
from dtls_down_server import PULL, unpack_frame
from dtls_peak_server import Peak
from udp_peak_channel import PlainUDP, MARKER, SIZE, encode, decode, parse_start


class PlainPeakTest(unittest.TestCase):
    def start(self):
        nonce = b'12345678'
        digest = hashlib.sha256(bytes(range(256)) * (SIZE // 256)).digest()
        wire = encode(frame(START, 4, nonce + struct.pack('!I', SIZE) + digest + MARKER.encode()), nonce[:7])
        return nonce, wire

    def test_frame_session_tag_and_size_are_checked(self):
        tag = b'1234567'
        packet = frame(DATA, 0, bytes(range(256)) * 4)
        self.assertEqual(decode(encode(packet, tag), tag), packet)
        with self.assertRaisesRegex(ValueError, 'session'):
            decode(encode(packet, tag), b'7654321')
        with self.assertRaisesRegex(ValueError, 'size'):
            decode(encode(packet, tag) + b'x', tag)
        nonce, wire = self.start()
        self.assertEqual(parse_start(wire)[0], nonce)
        with self.assertRaises(ValueError):
            parse_start(wire[:-1] + b'x')

    def test_cookie_echo_precedes_data_and_duplicate_start_reissues_challenge(self):
        nonce, wire = self.start()
        _, start = parse_start(wire)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            server.bind(('127.0.0.1', 0)); server.setblocking(False)
            client.bind(('127.0.0.1', 0)); client.connect(server.getsockname()); client.settimeout(.5)
            channel = PlainUDP(server, client.getsockname(), nonce, os.urandom(32), start)
            channel.challenge()
            kind, off, body = unpack_frame(decode(client.recv(2048), nonce[:7]))
            self.assertEqual((kind, off, len(body)), (ACK, 0xffffffff, 36))
            client.send(encode(frame(PULL, 0, b'wrong'), nonce[:7]))
            with self.assertRaises(TimeoutError):
                channel.read(.01)
            self.assertFalse(channel.verified)
            client.send(encode(frame(PULL, 0, body[4:]), nonce[:7]))
            self.assertEqual(channel.read(.1), frame(PULL, 0))
            self.assertTrue(channel.verified)
            client.send(wire)
            with self.assertRaises(TimeoutError):
                channel.read(.01)
            self.assertEqual(decode(client.recv(2048), nonce[:7]), frame(ACK, 0xffffffff, body))
            state = Peak(MARKER, 512, transport='UDP')
            state.feed(start, 0)
            self.assertIsNone(state.next_packet(.1))
            state.feed(frame(PULL, 0), .1)
            self.assertIsNotNone(state.next_packet(.1))
            self.assertEqual(state.result()['transport'], 'UDP')
            self.assertNotIn('authentication', state.result()['verification'])
