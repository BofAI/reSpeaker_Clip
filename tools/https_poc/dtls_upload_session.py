"""Authenticated DTLS file session; volatile receive credit and durable ACKs."""
import hmac
import struct
import time

from dtls_upload_protocol import (OPEN, DATA, QUERY, FINISH, ERROR, frame,
                                  unpack, parse_open, receipt)
from dtls_upload_store import UploadWriter
from resume_store import ResumeError


class UploadSession:
    def __init__(self, store, identities):
        self.store, self.identities = store, identities
        self.writer = None
        self.transfer = None
        self.last_ack = 0
        self.last_received = 0

    def reply(self):
        w = self.writer
        return receipt(self.transfer, w.identity, w.durable, w.received, w.future, w.stored)

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if self.writer is None:
            return []
        committed = self.writer.maybe_commit(now)
        changed = self.writer.received != self.last_received
        if committed or (changed and now - self.last_ack >= .05):
            return self._ack(now)
        return []

    def _ack(self, now):
        response = self.reply()
        self.last_ack = now
        self.last_received = self.writer.received
        return [response]

    def feed(self, packet, now=None):
        now = time.monotonic() if now is None else now
        kind, offset, transfer, body = unpack(packet)
        if kind == OPEN:
            if offset:
                raise ResumeError(400, 'invalid_open_offset')
            identity, supplied = parse_open(body)
            expected = self.identities.get(identity['device'])
            if expected is None or not hmac.compare_digest(supplied, expected):
                raise ResumeError(403, 'unauthorized_device')
            if self.writer and self.transfer == transfer:
                if self.writer.identity != identity:
                    raise ResumeError(409, 'transfer_identity_conflict')
                return self._ack(now)
            if self.writer and not self.writer.stored:
                raise ResumeError(409, 'incomplete_transfer_active')
            if self.writer:
                self.writer.close()
            self.writer = UploadWriter(self.store, identity, now)
            self.transfer = transfer
            return self._ack(now)
        if not self.writer or transfer != self.transfer:
            # Late frames from another file must not alter progress or close
            # the current authenticated transfer.
            return []
        if kind == DATA:
            self.writer.data(offset, body, now)
            if self.writer.stored or self.writer.received - self.last_received >= 16 * 1024:
                return self._ack(now)
            return self.tick(now)
        if kind == QUERY:
            if offset or body:
                raise ResumeError(400, 'invalid_query')
            self.writer.maybe_commit(now, force=True)
            return self._ack(now)
        if kind == FINISH:
            if body or offset != self.writer.identity['size']:
                raise ResumeError(400, 'invalid_finish')
            self.writer.finish(now)
            return self._ack(now)
        raise ResumeError(400, 'unexpected_client_frame')

    def close(self):
        if self.writer:
            self.writer.close()
