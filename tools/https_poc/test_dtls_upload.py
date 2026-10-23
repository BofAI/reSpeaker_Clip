"""Durable upload protocol: selective ACK, crash recovery and lost receipts."""
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from dtls_upload_protocol import (OPEN, DATA, QUERY, FINISH, ACK, COMPLETE,
                                  PAYLOAD, RECEIPT, frame, unpack, open_body, parse_open)
from dtls_upload_session import UploadSession
from dtls_upload_store import UploadWriter
from resume_store import ResumeStore, ResumeError


class DurableDatagramTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ResumeStore(Path(self.tmp.name), threading.Lock())
        self.token = 't' * 32
        self.content = bytes(i % 251 for i in range(70 * 1024 + 117))
        self.digest = hashlib.sha256(self.content).digest()
        self.start = open_body('clip', '20261020080208', 1, len(self.content), self.digest, self.token)
        self.identity, _ = parse_open(self.start)
        self.cid = b'nonce001'
        self.session = UploadSession(self.store, {'clip': self.token})
        self.addCleanup(lambda: self.session.close())

    def send(self, kind, offset=0, body=b'', now=0, session=None, cid=None):
        return (session or self.session).feed(frame(kind, offset, cid or self.cid, body), now)

    def ack(self, packet):
        kind, durable, cid, body = unpack(packet)
        size, digest, received, mask, complete = RECEIPT.unpack(body)
        self.assertEqual((cid, size, digest), (self.cid, len(self.content), self.digest))
        return kind, durable, received, mask, bool(complete)

    def test_receive_credit_is_not_a_durable_checkpoint(self):
        self.assertEqual(self.ack(self.send(OPEN, body=self.start)[0])[:3], (ACK, 0, 0))
        self.send(DATA, 1024, self.content[1024:2048], now=.01)
        # Selective ACK identifies received packet1 while packet0 is absent.
        a = self.ack(self.session.reply())
        self.assertEqual((a[1], a[2], a[3][0]), (0, 0, 2))
        self.send(DATA, 0, self.content[:1024], now=.02)
        self.assertEqual(self.ack(self.session.reply())[:3], (ACK, 0, 2048))
        a = self.ack(self.session.tick(.6)[0])
        self.assertEqual(a[:3], (ACK, 2048, 2048))
        saved = json.loads(self.session.writer.paths[2].read_text())
        self.assertEqual(saved['offset'], 2048)

    def test_recovery_discards_uncommitted_tail_then_finishes_exact_bytes(self):
        self.send(OPEN, body=self.start)
        for off in range(0, 68 * 1024, 1024):
            self.send(DATA, off, self.content[off:off + 1024], now=.1)
        self.assertEqual(self.session.writer.durable, 64 * 1024)
        self.assertEqual(self.session.writer.received, 68 * 1024)
        partial = self.session.writer.paths[1]
        self.session.close()
        self.session = UploadSession(self.store, {'clip': self.token})
        a = self.ack(self.send(OPEN, body=self.start, now=1)[0])
        self.assertEqual(a[:3], (ACK, 65536, 65536))
        self.assertEqual(partial.stat().st_size, 65536)
        responses = []
        for off in range(65536, len(self.content), 1024):
            responses = self.send(DATA, off, self.content[off:off + 1024], now=1.1)
        self.assertEqual(self.ack(responses[0])[:3], (COMPLETE, len(self.content), len(self.content)))
        self.assertEqual(self.session.writer.paths[0].read_bytes(), self.content)
        # COMPLETE lost: another QUERY and an entirely new connection agree.
        self.assertEqual(self.send(QUERY, now=1.2)[0], responses[0])
        self.session.close()
        self.session = UploadSession(self.store, {'clip': self.token})
        self.assertEqual(self.send(OPEN, body=self.start, now=2)[0], responses[0])

    def test_duplicate_open_preserves_live_receive_state_and_stale_frames_are_ignored(self):
        self.send(OPEN, body=self.start)
        self.send(DATA, 0, self.content[:1024], now=.1)
        self.assertEqual(self.ack(self.send(OPEN, body=self.start, now=.2)[0])[:3], (ACK, 0, 1024))
        self.assertEqual(self.send(DATA, 1024, self.content[1024:2048], cid=b'oldnonce'), [])
        self.assertEqual(self.session.writer.received, 1024)
        changed = open_body('clip', '20261020080208', 2, len(self.content), self.digest, self.token)
        with self.assertRaisesRegex(ResumeError, 'transfer_identity_conflict'):
            self.send(OPEN, body=changed)
        with self.assertRaisesRegex(ResumeError, 'incomplete_transfer_active'):
            self.send(OPEN, body=changed, cid=b'newnonce')

    def test_unauthenticated_open_never_creates_an_object(self):
        bad = open_body('clip', '20261020080208', 1, len(self.content), self.digest, 'x' * 32)
        with self.assertRaisesRegex(ResumeError, 'unauthorized'):
            self.send(OPEN, body=bad)
        self.assertEqual(list(self.store.directory.iterdir()), [])
        self.assertIsNone(self.session.writer)

    def test_data_fsync_failure_never_advances_durable_progress(self):
        self.send(OPEN, body=self.start)
        self.send(DATA, 0, self.content[:1024], now=.1)
        with patch('dtls_upload_store.os.fsync', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                self.session.tick(.6)
        self.assertEqual(self.session.writer.durable, 0)
        self.assertEqual(json.loads(self.session.writer.paths[2].read_text())['offset'], 0)
        self.session.close()
        w = UploadWriter(self.store, self.identity, 1)
        self.addCleanup(w.close)
        self.assertEqual((w.durable, w.received, w.paths[1].stat().st_size), (0, 0, 0))

    def test_checkpoint_failure_after_data_sync_is_recoverable(self):
        self.send(OPEN, body=self.start)
        self.send(DATA, 0, self.content[:1024], now=.1)
        with patch.object(self.store, '_save', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                self.session.tick(.6)
        self.assertEqual(self.session.writer.durable, 0)
        self.session.close()
        w = UploadWriter(self.store, self.identity, 1)
        self.addCleanup(w.close)
        self.assertEqual(w.paths[1].stat().st_size, 0)

    def test_hash_failure_cannot_publish_complete_or_ack_full_size(self):
        self.send(OPEN, body=self.start)
        for off in range(0, len(self.content) - 1024, 1024):
            self.send(DATA, off, b'x' * 1024, now=.1)
        last = (len(self.content) - 1) // 1024 * 1024
        with self.assertRaisesRegex(ResumeError, 'hash_mismatch'):
            self.send(DATA, last, b'x' * (len(self.content) - last), now=.1)
        self.assertFalse(self.session.writer.stored)
        self.assertLess(self.session.writer.durable, len(self.content))
        self.assertFalse(self.session.writer.paths[0].exists())
        self.session.close()
        w = UploadWriter(self.store, self.identity, 1)
        self.addCleanup(w.close)
        self.assertEqual(w.durable, 0)

    def test_invalid_range_duplicate_content_and_identity_conflict(self):
        self.send(OPEN, body=self.start)
        for off, body in [(1, b'x' * 1024), (128 * 1024, b'x' * 1024), (0, b'x')]:
            with self.assertRaises(ResumeError):
                self.send(DATA, off, body)
        self.send(DATA, 0, self.content[:1024], now=.1)
        self.send(DATA, 0, self.content[:1024], now=.2)
        self.assertEqual(self.session.writer.duplicates, 1)
        with self.assertRaisesRegex(ResumeError, 'duplicate_content_conflict'):
            self.send(DATA, 0, b'x' * 1024, now=.2)
        with self.assertRaisesRegex(ResumeError, 'missing_data'):
            self.send(FINISH, len(self.content), now=.2)
        self.session.close()
        changed = dict(self.identity, sha256=hashlib.sha256(b'other').hexdigest())
        with self.assertRaisesRegex(ResumeError, 'identity_conflict'):
            UploadWriter(self.store, changed, 1)

    def test_malformed_frame_and_open_fields(self):
        good = frame(OPEN, 0, self.cid, self.start)
        for bad in [good[:20], good + b'x', good[:5] + b'bad' + good[8:]]:
            with self.assertRaises(ValueError):
                unpack(bad)
        for body in [b'', self.start[:-1], bytes([33]) + self.start[1:]]:
            with self.assertRaises(ValueError):
                parse_open(body)
        for session in ['../escape', '2026102008020x']:
            with self.assertRaises(ValueError):
                open_body('clip', session, 1, len(self.content), self.digest, self.token)
