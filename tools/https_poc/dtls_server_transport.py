"""DTLS1.2 socket adapter extracted from the verified diagnostic receiver.

OpenSSL supplies record protection and handshake retransmission. Memory BIO
output is split at complete DTLS record boundaries before UDP transmission.
"""
import hashlib
import hmac
import os
import select
import time

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
    def __init__(self, sock, peer, context, receive_packet=None):
        from OpenSSL import SSL
        self.SSL = SSL
        self.sock, self.peer = sock, peer
        self.receive_packet = receive_packet
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
                if self.receive_packet is not None:
                    packet = self.receive_packet(.02)
                    if packet is not None:
                        self.connection.bio_write(packet)
                elif select.select([self.sock], [], [], .02)[0]:
                    packet, peer = self.sock.recvfrom(65535)
                    if peer == self.peer and len(packet) <= 2048:
                        self.connection.bio_write(packet)
                self.connection.DTLSv1_handle_timeout()
                self.flush(end)

    def handshake(self):
        return self.operation(self.connection.do_handshake, 15)

    def read(self, timeout):
        return self.operation(lambda: self.connection.recv(1200), timeout)

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
