"""Single-peer DTLS 1.2 benchmark, using the installed OpenSSL transport.

Application frames are length-prefixed, so pipe reads need not match datagrams.
Each ACK fits in one pipe write. OpenSSL owns DTLS encryption, replay protection,
cookies and handshake retransmission. This is not a production upload service.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import selectors
import struct
import subprocess
import tempfile
import time

HEADER = struct.Struct('!4sB3xII')
START, DATA, QUERY, ACK, FINISH, DONE = range(1, 7)
SUMMARY, MISSING = 7, 8
PAYLOAD = 1024
MAX_FILE = 8 * 1024 * 1024


def frame(kind, offset, body=b''):
    return HEADER.pack(b'CDB1', kind, offset, len(body)) + body


class Transfer:
    def __init__(self, directory, token):
        self.directory = Path(directory)
        self.token = token.encode('ascii')
        self.file = None
        self.partial = None
        self.run = None
        self.size = self.base = self.mask = 0
        self.last_ack = None
        self.digest = None
        self.complete = False
        self.burst = False
        self.received_mask = 0
        self.packets = self.duplicates = self.queries = 0
        self.started_ns = self.finished_ns = None

    def feed(self, kind, offset, body):
        if kind == START:
            if offset not in (0, 1) or not 76 <= len(body) <= 140 or not hmac.compare_digest(body[44:], self.token):
                raise ValueError('unauthorized_start')
            run = body[:8].hex()
            size = struct.unpack('!I', body[8:12])[0]
            digest = body[12:44]
            if not 0 < size <= MAX_FILE:
                raise ValueError('invalid_size')
            if self.run:
                if (run, size, digest, bool(offset)) != (self.run, self.size, self.digest, self.burst):
                    raise ValueError('conflicting_start')
            else:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                if (self.directory / (run + '.bin')).exists():
                    raise ValueError('existing_run')
                self.file = tempfile.NamedTemporaryFile(mode='w+b', prefix=run + '-',
                                                        suffix='.partial', dir=self.directory, delete=False)
                self.partial = Path(self.file.name)
                self.run, self.size, self.digest = run, size, digest
                self.burst = bool(offset)
            return frame(ACK, 0xffffffff, struct.pack('!I', 0))
        if not self.file:
            raise ValueError('start_required')
        if kind == DATA:
            if offset >= self.size or offset % PAYLOAD or len(body) != min(PAYLOAD, self.size - offset):
                raise ValueError('invalid_data')
            if self.complete:
                raise ValueError('already_complete')
            self.packets += 1
            if self.burst:
                index = offset // PAYLOAD
                if self.received_mask & (1 << index):
                    self.duplicates += 1
                    return None
                if self.started_ns is None:
                    self.started_ns = time.perf_counter_ns()
                self.file.seek(offset)
                if self.file.write(body) != len(body):
                    raise OSError('short_write')
                self.received_mask |= 1 << index
                return None
            if offset < self.base:
                self.duplicates += 1
                return None
            index = (offset - self.base) // PAYLOAD
            if index >= 32:
                raise ValueError('window_exceeded')
            if self.started_ns is None:
                self.started_ns = time.perf_counter_ns()
            if self.mask & (1 << index):
                self.duplicates += 1
                return None
            self.file.seek(offset)
            if self.file.write(body) != len(body):
                raise OSError('short_write')
            self.mask |= 1 << index
            return None
        if kind == QUERY:
            if self.burst:
                raise ValueError('burst_requires_summary')
            if len(body) != 4:
                raise ValueError('invalid_query')
            count = struct.unpack('!I', body)[0]
            if not 1 <= count <= 32:
                raise ValueError('invalid_count')
            self.queries += 1
            if self.last_ack and offset == self.last_ack[0]:
                if count != self.last_ack[1]:
                    raise ValueError('conflicting_query')
                return frame(ACK, offset, struct.pack('!I', self.last_ack[2]))
            if offset != self.base or self.complete or self.base >= self.size:
                raise ValueError('invalid_offset')
            length = min(count * PAYLOAD, self.size - self.base)
            if (length + PAYLOAD - 1) // PAYLOAD != count:
                raise ValueError('invalid_count')
            all_bits = (1 << count) - 1
            if self.mask & ~all_bits:
                raise ValueError('data_outside_batch')
            response = frame(ACK, offset, struct.pack('!I', self.mask))
            if self.mask == all_bits:
                self.last_ack = (self.base, count, self.mask)
                self.base += length
                self.mask = 0
            return response
        if kind == SUMMARY:
            if not self.burst or body or offset != self.size:
                raise ValueError('invalid_summary')
            self.queries += 1
            if self.complete:
                return frame(DONE, self.size, self.digest)
            count = (self.size + PAYLOAD - 1) // PAYLOAD
            missing = ((1 << count) - 1) ^ self.received_mask
            if missing:
                return frame(MISSING, self.size, missing.to_bytes((count + 7) // 8, 'little'))
            self.base = self.size
            return self.feed(FINISH, offset, b'')
        if kind == FINISH:
            if body or offset != self.size or self.base != self.size:
                raise ValueError('incomplete_file')
            if not self.complete:
                self.file.flush()
                os.fsync(self.file.fileno())
                self.file.seek(0)
                digest = hashlib.file_digest(self.file, 'sha256').digest()
                if not hmac.compare_digest(digest, self.digest):
                    raise ValueError('sha256_mismatch')
                self.file.close()
                os.replace(self.partial, self.directory / (self.run + '.bin'))
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.complete = True
                self.finished_ns = time.perf_counter_ns()
            return frame(DONE, self.size, self.digest)
        raise ValueError('unknown_frame')

    def close(self):
        if self.file and not self.file.closed:
            self.file.close()

    def result(self):
        elapsed = ((self.finished_ns - self.started_ns) / 1e9
                   if self.finished_ns and self.started_ns else None)
        return dict(run=self.run, size=self.size, confirmed=self.base, complete=self.complete,
                    burst=self.burst,
                    sha256=self.digest.hex() if self.digest else None, packets=self.packets,
                    duplicate_packets=self.duplicates, queries=self.queries, transfer_s=elapsed)


def serve_one(args, token, index):
    directory = args.directory
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = Transfer(directory, token)
    # s_server -timeout installs a 250 ms socket timeout. In its first
    # post-cookie SSL_accept path a retry can be treated as fatal on WANs.
    # Keep OpenSSL's DTLS flight timers and the outer bounded session deadline.
    command = ['openssl', 's_server', '-dtls1_2', '-listen', '-quiet', '-state',
               '-naccept', '1', '-accept', str(args.port), '-mtu', '1200',
               '-cert', str(args.root / 'private/server-chain.pem'),
               '-key', str(args.root / 'private/server.key'),
               '-cipher', 'ECDHE-ECDSA-AES128-GCM-SHA256']
    error_path = directory / ('openssl-' + str(index) + '.log')
    fd = os.open(error_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    error = None
    with os.fdopen(fd, 'wb') as errors, subprocess.Popen(command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, bufsize=0) as child:
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        buffered = bytearray()
        end = time.monotonic() + 240
        done_at = None
        print(json.dumps(dict(event='dtls_listening', session=index, port=args.port)), flush=True)
        try:
            while time.monotonic() < end:
                if child.poll() is not None:
                    break
                if done_at and time.monotonic() - done_at > 4:
                    break
                if not selector.select(.1):
                    continue
                data = os.read(child.stdout.fileno(), 65536)
                if not data:
                    break
                buffered.extend(data)
                while len(buffered) >= HEADER.size:
                    magic, kind, offset, size = HEADER.unpack_from(buffered)
                    if magic != b'CDB1' or buffered[5:8] != b'\x00\x00\x00' or size > PAYLOAD:
                        if state.complete:
                            buffered.clear()
                            break
                        raise ValueError('invalid_frame')
                    if len(buffered) < HEADER.size + size:
                        break
                    body = bytes(buffered[HEADER.size:HEADER.size + size])
                    del buffered[:HEADER.size + size]
                    response = state.feed(kind, offset, body)
                    if response:
                        written = os.write(child.stdin.fileno(), response)
                        if written != len(response):
                            raise OSError('short_ack_write')
                    if state.complete and done_at is None:
                        done_at = time.monotonic()
        except (OSError, ValueError) as caught:
            error = type(caught).__name__ + ':' + str(caught)
        finally:
            selector.close()
            state.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
    result = dict(event='dtls_result', session=index, error=error, **state.result())
    (directory / ('result-' + str(index) + '.json')).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', type=int, default=9527)
    parser.add_argument('--sessions', type=int, default=12)
    args = parser.parse_args()
    token = (args.root / 'private/token').read_text().strip()
    if not 32 <= len(token) <= 96 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in token):
        parser.error('invalid token')
    # Restarts retain every previous session's evidence.
    previous = [int(p.stem.split('-')[1]) for p in args.directory.glob('openssl-*.log')
                if p.stem.split('-')[1].isdigit()]
    first = max(previous, default=-1) + 1
    for index in range(first, first + args.sessions):
        serve_one(args, token, index)


if __name__ == '__main__':
    main()
