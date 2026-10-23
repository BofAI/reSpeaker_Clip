"""Bounded DTLS downlink ceiling diagnostic; synthetic offset-checked 4 MiB."""
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

from dtls_bench_receiver import START, DATA, ACK, DONE, QUERY, PAYLOAD, frame
from dtls_down_server import DatagramTLS, make_context, unpack_frame, PULL

CREDIT = 10
SIZE = 4 * 1024 * 1024
WINDOW = 256  # 256 KiB BDP allowance, without a file-sized device buffer.


class Peak:
    def __init__(self, token, rate, transport='DTLS1.2/UDP'):
        if rate not in (0, 256, 512, 1024):
            raise ValueError('invalid_peak_rate')
        self.content = bytes(range(256)) * (SIZE // 256)
        self.digest = hashlib.sha256(self.content).digest()
        self.token = token.encode()
        self.transport = transport
        self.rate = rate
        self.run = None
        self.started = self.finished = None
        self.complete = False
        self.acked = set()
        self.flying = {}
        self.tries = {}
        self.next_index = 0
        self.packets = self.retransmissions = self.credits = 0
        self.progress_at = time.monotonic()

    def feed(self, packet, now):
        kind, off, body = unpack_frame(packet)
        if kind == START:
            if off != 3 or not 76 <= len(body) <= 140 or not hmac.compare_digest(body[44:], self.token):
                raise ValueError('unauthorized_peak_start')
            if struct.unpack('!I', body[8:12])[0] != SIZE or body[12:44] != self.digest:
                raise ValueError('peak_fingerprint_mismatch')
            run = body[:8].hex()
            if self.run and self.run != run:
                raise ValueError('conflicting_start')
            self.run = run
            return [frame(ACK, 0xffffffff, struct.pack('!I', 0))]
        if not self.run:
            raise ValueError('start_required')
        if kind == PULL:
            if off or body:
                raise ValueError('invalid_pull')
            if self.started is None:
                self.started = now
                self.progress_at = now
            return []
        if kind == CREDIT:
            if self.started is None or len(body) != SIZE // PAYLOAD // 8:
                raise ValueError('invalid_credit')
            received = {i for i in range(SIZE // PAYLOAD) if body[i // 8] & (1 << (i % 8))}
            if off != len(received) * PAYLOAD or any(i >= self.next_index for i in received):
                raise ValueError('invalid_credit_accounting')
            if not self.acked <= received:
                # Older snapshots may arrive late; never regress credit.
                received |= self.acked
            if len(received) > len(self.acked):
                self.progress_at = now
            self.acked = received
            for i in list(self.flying):
                if i in self.acked:
                    del self.flying[i]
            self.credits += 1
            return []
        if kind == DONE:
            if self.started is None or off != SIZE or body != self.digest:
                raise ValueError('invalid_done')
            if not self.complete:
                self.complete = True
                self.finished = now
            return [frame(ACK, SIZE, self.digest)]
        raise ValueError('unexpected_control')

    def next_packet(self, now):
        if self.started is None or self.complete:
            return None
        if now - self.progress_at > 15:
            raise TimeoutError('no_receive_progress')
        expired = next((i for i, sent in self.flying.items() if now - sent >= 1), None)
        repair = expired is not None
        if repair:
            index = expired
        elif self.next_index < SIZE // PAYLOAD and len(self.flying) < WINDOW:
            index = self.next_index
            self.next_index += 1
        else:
            return None
        self.tries[index] = self.tries.get(index, 0) + 1
        if self.tries[index] > 12:
            raise TimeoutError('packet_retry_limit')
        self.flying[index] = now
        self.packets += 1
        self.retransmissions += repair
        off = index * PAYLOAD
        return frame(DATA, off, self.content[off:off + PAYLOAD])

    def result(self):
        return dict(run=self.run, transport=self.transport, direction='download-ram', bytes=SIZE,
                    sha256=self.digest.hex(), verification=('DTLS authentication plus ' if self.transport != 'UDP' else '') + 'per-byte offset pattern and unique-packet coverage; no SD file',
                    complete=self.complete, offered_kib_s=self.rate, flight_limit_kib=WINDOW,
                    packets=self.packets, retransmissions=self.retransmissions,
                    credited_bytes=len(self.acked) * PAYLOAD, credit_messages=self.credits,
                    transfer_s=self.finished - self.started if self.finished else None)


def run_peak(channel, state):
    deadline = time.monotonic() + 240
    next_send = time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        if state.complete and now - state.finished > 4:
            return
        readable = channel.connection.pending() or select.select([channel.sock], [], [], 0)[0]
        if readable:
            try:
                request = channel.read(.05)
            except TimeoutError:
                continue
            for response in state.feed(request, time.monotonic()):
                channel.write(response)
            continue
        if now >= next_send:
            packet = state.next_packet(now)
            if packet:
                channel.write(packet)
                next_send = time.monotonic() + (PAYLOAD / (state.rate * 1024) if state.rate else 0)
                continue
        # No 64 KiB/s repair cap: new data and retransmissions share the same
        # configured offered rate, bounded by authenticated receiver credit.
        delay = min(.001, max(0, next_send - time.monotonic())) if now < next_send else .001
        select.select([channel.sock], [], [], delay)
    raise TimeoutError('peak_session_deadline')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--port', type=int, default=9527)
    parser.add_argument('--allow-plain-udp', action='store_true', help='temporary synthetic benchmark, no production credentials on UDP')
    args = parser.parse_args()
    token = (args.root / 'private/token').read_text().strip()
    if not 32 <= len(token) <= 96 or not all(c.isascii() and (c.isalnum() or c in '_-') for c in token):
        parser.error('invalid token')
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    from OpenSSL import SSL
    context = make_context(args.root)
    cookie_key = os.urandom(32)
    from udp_peak_channel import PlainUDP, parse_start, MARKER
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('0.0.0.0', args.port)); sock.setblocking(False)
        print(json.dumps(dict(event='dtls_peak_listening', port=args.port)), flush=True)
        while True:
            if not select.select([sock], [], [], .5)[0]:
                continue
            packet, peer = sock.recvfrom(2048, socket.MSG_PEEK)
            plain = None
            if args.allow_plain_udp:
                try:
                    nonce, start = parse_start(packet)
                    plain = PlainUDP(sock, peer, nonce, cookie_key, start)
                except ValueError:
                    pass
            if plain is None and (len(packet) < 25 or packet[0] != 22 or packet[3:5] != b'\0\0' or packet[13] != 1):
                sock.recvfrom(65535); continue
            channel = plain or DatagramTLS(sock, peer, context)
            state = None; error = None
            try:
                rate = json.loads(args.profile.read_text())['rate_kib']
                if plain:
                    sock.recvfrom(65535)  # Consume only the peeked START.
                    state = Peak(MARKER, rate, transport='UDP')
                    state.feed(start, time.monotonic())
                    plain.challenge()  # No DATA until client echoes the cookie.
                else:
                    channel.handshake()
                    state = Peak(token, rate)
                print(json.dumps(dict(event='peak_session', transport=state.transport, offered_kib_s=state.rate)), flush=True)
                run_peak(channel, state)
            except SSL.ZeroReturnError:
                if not state or not state.complete:
                    error = 'peer_closed_before_completion'
            except (SSL.Error, OSError, ValueError, TimeoutError) as caught:
                error = type(caught).__name__ + ':' + str(caught)
            finally:
                channel.close()
            result = dict(error=error, **(state.result() if state else {}))
            name = (state.run if state and state.run else 'handshake') + '-' + str(time.time_ns()) + '.json'
            (args.directory / name).write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(dict(event='dtls_peak_result', **result)), flush=True)


if __name__ == '__main__':
    main()
