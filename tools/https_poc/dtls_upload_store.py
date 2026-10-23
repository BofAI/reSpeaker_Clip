"""One-owner UDP receive session, using the existing durable checkpoint format.

Received data may be ahead of the published checkpoint. A new session recovers
through ResumeStore._load, truncating that volatile tail. Only fsync followed
by atomic checkpoint publication advances durable progress.
"""
import hashlib
import json
import os
import time

from dtls_upload_protocol import MAX_FILE, PAYLOAD, WINDOW_PACKETS
from resume_store import ResumeStore, ResumeError


class UploadWriter:
    COMMIT_BYTES = 64 * 1024
    COMMIT_SECONDS = .5

    def __init__(self, store, identity, now=None):
        self.store = store
        self.identity = dict(identity)
        self.fd = None
        self.closed = False
        self.future = set()
        self.commits = self.packets = self.duplicates = 0
        self.last_commit = time.monotonic() if now is None else now
        size, digest = identity['size'], identity['sha256']
        if not 0 < size <= MAX_FILE or len(digest) != 64:
            raise ValueError('invalid_identity')
        with store.lock:
            paths, state, stored = store._load(identity['object_id'], size, digest)
            self.paths, self.state, self.stored = paths, state, stored
            self.durable = self.received = state['offset']
            metadata = store.directory / (identity['object_id'] + '.identity')
            if metadata.exists():
                if json.loads(metadata.read_text()) != identity:
                    raise ResumeError(409, 'identity_conflict')
            else:
                store._save(metadata, identity)
            if not stored:
                if not paths[2].exists():
                    store._save(paths[2], state)
                self.fd = os.open(paths[1], os.O_RDWR | os.O_CREAT, 0o600)

    def _check_open(self):
        if self.closed:
            raise ValueError('writer_closed')

    def data(self, offset, data, now=None):
        self._check_open()
        size = self.identity['size']
        if (offset % PAYLOAD or not 0 <= offset < size or
                len(data) != min(PAYLOAD, size - offset) or
                offset >= self.received + WINDOW_PACKETS * PAYLOAD):
            raise ResumeError(400, 'invalid_data_range')
        self.packets += 1
        if self.stored:
            return
        with self.store.lock:
            if offset < self.received or offset in self.future:
                if os.pread(self.fd, len(data), offset) != data:
                    raise ResumeError(409, 'duplicate_content_conflict')
                self.duplicates += 1
                return
            written = 0
            while written < len(data):
                n = os.pwrite(self.fd, data[written:], offset + written)
                if n <= 0:
                    raise OSError('short_data_write')
                written += n
            self.future.add(offset)
            while self.received in self.future:
                self.future.remove(self.received)
                self.received += min(PAYLOAD, size - self.received)
        self.maybe_commit(time.monotonic() if now is None else now)

    def maybe_commit(self, now, force=False):
        self._check_open()
        if self.stored or self.received <= self.durable:
            return False
        if not (force or self.received == self.identity['size'] or
                self.received - self.durable >= self.COMMIT_BYTES or now - self.last_commit >= self.COMMIT_SECONDS):
            return False
        with self.store.lock:
            # No packet is called durable until BOTH operations succeed.
            os.fsync(self.fd)
            new_state = dict(self.state, offset=self.received)
            self.store._save(self.paths[2], new_state)
            if self.received == self.identity['size']:
                os.close(self.fd)
                self.fd = None
                self.store._finish(self.paths, new_state)
                self.stored = True
            self.state = new_state
            self.durable = self.received
            self.commits += 1
            self.last_commit = now
        return True

    def finish(self, now=None):
        if self.received != self.identity['size']:
            raise ResumeError(409, 'missing_data')
        self.maybe_commit(time.monotonic() if now is None else now, force=True)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.closed = True
