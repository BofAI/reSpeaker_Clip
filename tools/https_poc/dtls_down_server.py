"""DTLS1.2 paced downlink diagnostic using the installed pyOpenSSL API."""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import select
import socket
import struct
import time

from dtls_bench_receiver import HEADER, START, DATA, ACK, DONE, SUMMARY, MISSING, frame, MAX_FILE, PAYLOAD

PULL = 9


def unpack_frame(packet):
    if len(packet) < HEADER.size:
        raise ValueError('short_frame')
    magic, kind, offset, size = HEADER.unpack_from(packet)
    if magic != b'CDB1' or packet[5:8] != b'\0\0\0' or size > PAYLOAD or len(packet) != HEADER.size + size:
        raise ValueError('invalid_frame')
    return kind, offset, packet[HEADER.size:]


class Download:
    def __init__(self, source, token, rate_kib):
        self.source = Path(source)
        self.size = self.source.stat().st_size
        if not 0 < self.size <= MAX_FILE or rate_kib not in (32, 64, 128, 256, 512):
            raise ValueError('invalid_source_or_rate')
        with self.source.open('rb') as data:
            self.digest = hashlib.file_digest(data, 'sha256').digest()
        self.token = token.encode()
        self.rate_kib = rate_kib
        self.run = None
        self.started = self.finished = None
        self.complete = False
        self.packets = self.repairs = self.requests = 0

    def summary(self):
        return frame(SUMMARY, self.size, struct.pack('!II', self.packets, self.repairs))

    def packets_at(self, offsets, repair):
        with self.source.open('rb') as data:
            for offset in offsets:
                data.seek(offset)
                body = data.read(min(PAYLOAD, self.size - offset))
                if len(body) != min(PAYLOAD, self.size - offset):
                    raise OSError('source_changed')
                self.packets += 1
                self.repairs += bool(repair)
                yield frame(DATA, offset, body), repair
        yield self.summary(), False

    def feed(self, packet):
        kind, offset, body = unpack_frame(packet)
        if kind == START:
            if offset != 2 or not 76 <= len(body) <= 140 or not hmac.compare_digest(body[44:], self.token):
                raise ValueError('unauthorized_start')
            run = body[:8].hex()
            if struct.unpack('!I', body[8:12])[0] != self.size or body[12:44] != self.digest:
                raise ValueError('source_mismatch')
            if self.run and self.run != run:
                raise ValueError('conflicting_start')
            self.run = run
            yield frame(ACK, 0xffffffff, struct.pack('!I', 0)), False
            return
        if not self.run:
            raise ValueError('start_required')
        if kind == PULL:
            if offset or body:
                raise ValueError('invalid_pull')
            if self.started is None:
                self.started = time.perf_counter_ns()
                yield from self.packets_at(range(0, self.size, PAYLOAD), False)
            else:
                yield self.summary(), False
            return
        if kind == MISSING:
            count = (self.size + PAYLOAD - 1) // PAYLOAD
            length = (count + 7) // 8
            if self.started is None or self.complete or offset != self.size or len(body) != length:
                raise ValueError('invalid_missing')
            if count % 8 and body[-1] >> (count % 8):
                raise ValueError('invalid_bitmap_tail')
            self.requests += 1
            if self.requests > 12:
                raise ValueError('repair_limit')
            offsets = (i * PAYLOAD for i in range(count) if body[i // 8] & (1 << (i % 8)))
            yield from self.packets_at(offsets, True)
            return
        if kind == DONE:
            if self.started is None or offset != self.size or body != self.digest:
                raise ValueError('invalid_completion')
            if not self.complete:
                self.complete = True
                self.finished = time.perf_counter_ns()
            yield frame(ACK, self.size, self.digest), False
            return
        raise ValueError('unknown_frame')

    def result(self):
        return dict(run=self.run, direction='download', size=self.size, sha256=self.digest.hex(),
                    rate_kib=self.rate_kib, repair_rate_kib=min(64, self.rate_kib),
                    packets=self.packets, retransmissions=self.repairs, repair_requests=self.requests,
                    complete=self.complete,
                    transfer_s=(self.finished - self.started) / 1e9 if self.finished else None)


def drain_records(pending, mtu=1200):
    """Separate complete DTLS1.2 records; a record is one valid UDP payload."""
    while len(pending) >= 13:
        if pending[0] not in (20, 21, 22, 23) or pending[1:3] not in (b'\xfe\xfd', b'\xfe\xff'):
            raise ValueError('unexpected_dtls_record')
        size = 13 + int.from_bytes(pending[11:13], 'big')
        if size > mtu:
            raise ValueError('record_exceeds_mtu')
        if len(pending) < size:
            return
        record = bytes(pending[:size])
        del pending[:size]
        yield record


class DatagramTLS:
    """Public pyOpenSSL memory BIO API; never coalesce plaintext writes.

    OpenSSL supplies the DTLS record header and encryption. The adapter only
    sends complete encrypted records and feeds received UDP datagrams back.
    """
    def __init__(self, sock, peer, context):
        from OpenSSL import SSL
        self.SSL = SSL
        self.sock, self.peer = sock, peer
        self.connection = SSL.Connection(context, None)
        self.connection.set_app_data(peer)
        self.connection.set_accept_state()
        self.connection.set_ciphertext_mtu(1200)
        self.pending = bytearray()

    def flush(self, end):
        while True:
            try:
                self.pending.extend(self.connection.bio_read(65536))
            except self.SSL.WantReadError:
                break
        for packet in drain_records(self.pending):
            while True:
                if time.monotonic() >= end:
                    raise TimeoutError('DTLS send deadline')
                try:
                    if self.sock.sendto(packet, self.peer) != len(packet):
                        raise OSError('short_datagram')
                    break
                except BlockingIOError:
                    select.select([], [self.sock], [], .02)

    def operation(self, call, timeout):
        end = time.monotonic() + timeout
        while True:
            try:
                result = call()
                self.flush(end)
                return result
            except (self.SSL.WantReadError, self.SSL.WantWriteError):
                self.flush(end)
                if time.monotonic() >= end:
                    raise TimeoutError('DTLS operation deadline')
                if select.select([self.sock], [], [], .02)[0]:
                    packet, peer = self.sock.recvfrom(65535)
                    if peer == self.peer and len(packet) <= 2048:
                        self.connection.bio_write(packet)
                self.connection.DTLSv1_handle_timeout()
                self.flush(end)

    def handshake(self):
        return self.operation(self.connection.do_handshake, 15)

    def read(self, timeout):
        return self.operation(lambda: self.connection.recv(HEADER.size + PAYLOAD), timeout)

    def write(self, packet):
        n = self.operation(lambda: self.connection.send(packet), 5)
        if n != len(packet):
            raise OSError('short_dtls_write')

    def close(self):
        try:
            self.connection.shutdown()
        except self.SSL.Error:
            pass
        try:
            self.flush(time.monotonic() + .1)
        except (OSError, TimeoutError):
            pass


def make_context(root):
    from OpenSSL import SSL
    context = SSL.Context(SSL.DTLS_SERVER_METHOD)
    context.set_min_proto_version(0xfefd)
    context.set_max_proto_version(0xfefd)
    context.set_cipher_list(b'ECDHE-ECDSA-AES128-GCM-SHA256')
    context.use_certificate_chain_file(str(root / 'private/server-chain.pem'))
    context.use_privatekey_file(str(root / 'private/server.key'))
    context.check_privatekey()
    # OpenSSL ssl.h: OP_NO_QUERY_MTU bit12, OP_COOKIE_EXCHANGE bit13.
    context.set_options((1 << 12) | (1 << 13))
    key = os.urandom(32)

    def cookie(connection):
        return hmac.new(key, repr(connection.get_app_data()).encode(), 'sha256').digest()

    context.set_cookie_generate_callback(cookie)
    context.set_cookie_verify_callback(lambda connection, value: hmac.compare_digest(cookie(connection), value))
    return context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--port', type=int, default=9527)
    args = parser.parse_args()
    token = (args.root / 'private/token').read_text().strip()
    if not 32 <= len(token) <= 96 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in token):
        parser.error('invalid token')
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    context = make_context(args.root)
    from OpenSSL import SSL
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('0.0.0.0', args.port))
        sock.setblocking(False)
        print(json.dumps(dict(event='dtls_down_listening', port=args.port)), flush=True)
        while True:
            if not select.select([sock], [], [], .5)[0]:
                continue
            packet, peer = sock.recvfrom(2048, socket.MSG_PEEK)
            if len(packet) < 25 or packet[0] != 22 or packet[3:5] != b'\0\0' or packet[13] != 1:
                sock.recvfrom(65535)
                continue
            transfer = None
            channel = DatagramTLS(sock, peer, context)
            error = None
            try:
                channel.handshake()
                profile = json.loads(args.profile.read_text())
                transfer = Download(args.source, token, profile['rate_kib'])
                print(json.dumps(dict(event='dtls_down_handshake', cipher=channel.connection.get_cipher_name(),
                                      rate_kib=transfer.rate_kib)), flush=True)
                deadline = time.monotonic() + 240
                done_at = None
                next_send = time.monotonic()
                while time.monotonic() < deadline:
                    if done_at and time.monotonic() - done_at > 4:
                        break
                    try:
                        request = channel.read(1)
                    except TimeoutError:
                        continue
                    kind, offset, body = unpack_frame(request)
                    print(json.dumps(dict(event='dtls_down_control', kind=kind, offset=offset,
                                          length=len(body))), flush=True)
                    for response, repair in transfer.feed(request):
                        if response[4] == DATA:
                            now = time.monotonic()
                            if next_send > now:
                                time.sleep(next_send - now)
                            channel.write(response)
                            rate = min(64, transfer.rate_kib) if repair else transfer.rate_kib
                            next_send = max(next_send, time.monotonic()) + (len(response) - HEADER.size) / (rate * 1024)
                        else:
                            channel.write(response)
                    if transfer.complete and done_at is None:
                        done_at = time.monotonic()
            except (SSL.Error, OSError, ValueError, TimeoutError) as caught:
                error = type(caught).__name__ + ':' + str(caught)
            finally:
                channel.close()
            result = dict(event='dtls_down_result', error=error, **(transfer.result() if transfer else {}))
            name = (transfer.run if transfer and transfer.run else 'handshake') + '-' + str(time.time_ns()) + '.json'
            (args.directory / name).write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
