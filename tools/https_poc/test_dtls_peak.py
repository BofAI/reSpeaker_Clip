import hashlib
import struct
import unittest

from dtls_bench_receiver import START, DATA, ACK, DONE, frame
from dtls_down_server import PULL, unpack_frame
from dtls_peak_server import Peak, CREDIT, SIZE, WINDOW


class PeakTest(unittest.TestCase):
    def start(self):
        p = Peak('t' * 32, 512)
        body = b'12345678' + struct.pack('!I', SIZE) + p.digest + b't' * 32
        self.assertEqual(unpack_frame(p.feed(frame(START, 3, body), 0)[0])[0], ACK)
        p.feed(frame(PULL, 0), 0)
        return p

    def test_flight_limit_credit_reorder_repair_and_completion(self):
        p = self.start()
        for index in range(WINDOW):
            kind, off, data = unpack_frame(p.next_packet(.01))
            self.assertEqual((kind, off, data), (DATA, index * 1024, bytes(range(256)) * 4))
        self.assertIsNone(p.next_packet(.5))
        kind, off, _ = unpack_frame(p.next_packet(1.1))
        self.assertEqual((kind, off), (DATA, 0))
        self.assertEqual(p.retransmissions, 1)
        bits = bytearray(SIZE // 1024 // 8)
        bits[:32] = b'\xff' * 32
        p.feed(frame(CREDIT, WINDOW * 1024, bits), 1.2)
        self.assertFalse(p.flying)
        # A stale bitmap cannot revoke acknowledged credit.
        p.feed(frame(CREDIT, 0, bytes(len(bits))), 1.3)
        self.assertEqual(len(p.acked), WINDOW)
        self.assertEqual(unpack_frame(p.next_packet(1.4))[1], WINDOW * 1024)
        with self.assertRaisesRegex(ValueError, 'accounting'):
            p.feed(frame(CREDIT, 0, bits), 1.5)
        for index in range(WINDOW + 1, SIZE // 1024):
            # Complete the rest in bounded batches with authentic credits.
            p.next_packet(1.5)
            if (index + 1) % 32 == 0:
                bits[:(index + 1) // 8] = b'\xff' * ((index + 1) // 8)
                p.feed(frame(CREDIT, (index + 1) * 1024, bits), 1.5)
        self.assertEqual(p.digest, hashlib.sha256(bytes(range(256)) * (SIZE // 256)).digest())
        reply = p.feed(frame(DONE, SIZE, p.digest), 2)
        self.assertEqual(unpack_frame(reply[0]), (ACK, SIZE, p.digest))
        self.assertEqual(reply, p.feed(frame(DONE, SIZE, p.digest), 3))
        self.assertTrue(p.result()['complete'])
        self.assertEqual(p.result()['credited_bytes'], SIZE)

    def test_reject_bad_fingerprint_and_timeout_without_progress(self):
        p = Peak('t' * 32, 0)
        with self.assertRaises(ValueError):
            p.feed(frame(START, 3, b'12345678' + struct.pack('!I', SIZE) + b'x' * 32 + b't' * 32), 0)
        p = self.start()
        with self.assertRaisesRegex(TimeoutError, 'progress'):
            p.next_packet(16)
