import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from dtls_bench_receiver import ACK, DATA, DONE, FINISH, HEADER, MISSING, QUERY, START, SUMMARY, Transfer


class DtlsReceiverTest(unittest.TestCase):
    def test_burst_repairs_only_missing_packets_and_ack_loss_is_idempotent(self):
        content = bytes(i % 251 for i in range(33001))
        token = 't' * 32
        with tempfile.TemporaryDirectory() as directory:
            transfer = Transfer(directory, token)
            digest = hashlib.sha256(content).digest()
            start = b'abcdefgh' + struct.pack('!I', len(content)) + digest + token.encode()
            try:
                transfer.feed(START, 1, start)
                for index in reversed(range(33)):
                    if index in (7, 32):
                        continue
                    transfer.feed(DATA, index * 1024, content[index * 1024:(index + 1) * 1024])
                missing = transfer.feed(SUMMARY, len(content), b'')
                self.assertEqual(HEADER.unpack_from(missing)[1], MISSING)
                self.assertEqual(int.from_bytes(missing[HEADER.size:], 'little'), (1 << 7) | (1 << 32))
                self.assertEqual(missing, transfer.feed(SUMMARY, len(content), b''))
                self.assertFalse(transfer.complete)
                for index in (7, 32):
                    transfer.feed(DATA, index * 1024, content[index * 1024:(index + 1) * 1024])
                done = transfer.feed(SUMMARY, len(content), b'')
                self.assertEqual(HEADER.unpack_from(done)[1], DONE)
                self.assertEqual(done[HEADER.size:], digest)
                self.assertEqual(done, transfer.feed(SUMMARY, len(content), b''))
                self.assertEqual((Path(directory) / '6162636465666768.bin').read_bytes(), content)
                self.assertEqual(transfer.queries, 4)
            finally:
                transfer.close()

    def test_reordering_missing_packets_duplicate_ack_and_final_sha(self):
        content = bytes(i % 251 for i in range(33001))
        token = 't' * 32
        with tempfile.TemporaryDirectory() as directory:
            transfer = Transfer(directory, token)
            digest = hashlib.sha256(content).digest()
            start = b'12345678' + struct.pack('!I', len(content)) + digest + token.encode()
            try:
                self.assertEqual(HEADER.unpack_from(transfer.feed(START, 0, start))[1], ACK)
                self.assertEqual(transfer.feed(START, 0, start), transfer.feed(START, 0, start))
                for index in reversed(range(32)):
                    if index == 7:
                        continue
                    offset = index * 1024
                    self.assertIsNone(transfer.feed(DATA, offset, content[offset:offset + 1024]))
                partial = transfer.feed(QUERY, 0, struct.pack('!I', 32))
                self.assertEqual(struct.unpack('!I', partial[HEADER.size:])[0], 0xffffffff ^ (1 << 7))
                self.assertEqual(transfer.base, 0)
                transfer.feed(DATA, 7 * 1024, content[7 * 1024:8 * 1024])
                full = transfer.feed(QUERY, 0, struct.pack('!I', 32))
                self.assertEqual(transfer.base, 32768)
                self.assertEqual(full, transfer.feed(QUERY, 0, struct.pack('!I', 32)))
                transfer.feed(DATA, 0, content[:1024])
                self.assertEqual(transfer.duplicates, 1)
                transfer.feed(DATA, 32768, content[32768:])
                transfer.feed(QUERY, 32768, struct.pack('!I', 1))
                done = transfer.feed(FINISH, len(content), b'')
                self.assertTrue(transfer.complete)
                self.assertEqual(HEADER.unpack_from(done)[1], DONE)
                self.assertEqual(done[HEADER.size:], digest)
                self.assertEqual(done, transfer.feed(FINISH, len(content), b''))
                self.assertEqual((Path(directory) / '3132333435363738.bin').read_bytes(), content)
                self.assertFalse(list(Path(directory).glob('*.partial')))
            finally:
                transfer.close()

    def test_rejects_unauthorized_invalid_window_and_corruption(self):
        token = 't' * 32
        with tempfile.TemporaryDirectory() as directory:
            transfer = Transfer(directory, token)
            good = b'12345678' + struct.pack('!I', 1) + hashlib.sha256(b'x').digest() + token.encode()
            try:
                with self.assertRaisesRegex(ValueError, 'unauthorized'):
                    transfer.feed(START, 0, good[:-32] + b'z' * 32)
                self.assertFalse(list(Path(directory).iterdir()))
                transfer.feed(START, 0, good)
                with self.assertRaises(ValueError):
                    transfer.feed(QUERY, 0, struct.pack('!I', 33))
                with self.assertRaises(ValueError):
                    transfer.feed(FINISH, 1, b'')
                with self.assertRaises(ValueError):
                    transfer.feed(DATA, 1, b'x')
                transfer.feed(DATA, 0, b'y')
                transfer.feed(QUERY, 0, struct.pack('!I', 1))
                with self.assertRaisesRegex(ValueError, 'sha256_mismatch'):
                    transfer.feed(FINISH, 1, b'')
                self.assertFalse(list(Path(directory).glob('*.bin')))
            finally:
                transfer.close()


if __name__ == '__main__':
    unittest.main()
