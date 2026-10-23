import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from dtls_down_server import Download, PULL, drain_records, unpack_frame
from dtls_bench_receiver import START, DATA, ACK, DONE, SUMMARY, MISSING, frame


class DownServerTest(unittest.TestCase):
    def test_authenticated_source_repair_and_idempotent_controls(self):
        content = bytes(i % 251 for i in range(8301))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixed.bin'
            source.write_bytes(content)
            state = Download(source, 't' * 32, 128)
            start = b'12345678' + struct.pack('!I', len(content)) + hashlib.sha256(content).digest() + b't' * 32
            self.assertEqual(unpack_frame(list(state.feed(frame(START, 2, start)))[0][0])[0], ACK)
            packets = list(state.feed(frame(PULL, 0)))
            data = [unpack_frame(p)[2] for p, repair in packets if p[4] == DATA]
            self.assertEqual(b''.join(data), content)
            self.assertEqual(packets[-1][0][4], SUMMARY)
            self.assertEqual(len(list(state.feed(frame(PULL, 0)))), 1)
            repairs = list(state.feed(frame(MISSING, len(content), b'\x00\x01')))
            self.assertTrue(repairs[0][1])
            self.assertEqual(unpack_frame(repairs[0][0]), (DATA, 8192, content[8192:]))
            with self.assertRaises(ValueError):
                list(state.feed(frame(MISSING, len(content), b'\x00\x80')))
            done = frame(DONE, len(content), hashlib.sha256(content).digest())
            response = list(state.feed(done))
            self.assertEqual(response, list(state.feed(done)))
            self.assertTrue(state.complete)
            self.assertEqual(state.repairs, 1)
            self.assertEqual(source.read_bytes(), content)

    def test_rejects_wrong_token_or_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixed.bin'
            source.write_bytes(b'x')
            state = Download(source, 't' * 32, 64)
            start = b'12345678' + struct.pack('!I', 1) + hashlib.sha256(b'x').digest()
            with self.assertRaisesRegex(ValueError, 'unauthorized'):
                list(state.feed(frame(START, 2, start + b'z' * 32)))
            with self.assertRaisesRegex(ValueError, 'source_mismatch'):
                list(state.feed(frame(START, 2, start[:12] + b'\0' * 32 + b't' * 32)))
            self.assertIsNone(state.run)
            self.assertEqual(state.packets, 0)

    def test_dtls_records_preserve_boundaries_across_memory_bio_chunks(self):
        def record(body):
            return b'\x17\xfe\xfd' + b'\0' * 8 + len(body).to_bytes(2, 'big') + body
        expected = [record(b'a' * 1000), record(b'b' * 30)]
        stream = b''.join(expected)
        pending = bytearray()
        output = []
        for offset in range(0, len(stream), 17):
            pending.extend(stream[offset:offset + 17])
            output.extend(drain_records(pending))
        self.assertEqual(output, expected)
        self.assertFalse(pending)
        with self.assertRaisesRegex(ValueError, 'mtu'):
            list(drain_records(bytearray(record(b'x' * 1200))))


if __name__ == '__main__':
    unittest.main()
