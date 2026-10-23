"""Versioned, bounded DTLS recording upload frames (application protocol v3)."""
import hashlib
import re
import struct

MAGIC = b'CU01'
HEADER = struct.Struct('!4sB3xII8s')
OPEN, DATA, QUERY, ACK, FINISH, COMPLETE, ERROR = range(1, 8)
PAYLOAD = 1024
WINDOW_PACKETS = 128
MAX_FILE = 8 * 1024 * 1024
RECEIPT = struct.Struct('!I32sI16sB')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,32}\Z', re.ASCII)
SESSION = re.compile(r'[0-9]{14}\Z', re.ASCII)


def frame(kind, offset, transfer, body=b''):
    if len(transfer) != 8 or not 0 <= offset <= MAX_FILE or len(body) > PAYLOAD:
        raise ValueError('invalid_frame_fields')
    return HEADER.pack(MAGIC, kind, offset, len(body), transfer) + body


def unpack(packet):
    if len(packet) < HEADER.size:
        raise ValueError('short_frame')
    magic, kind, offset, length, transfer = HEADER.unpack_from(packet)
    if magic != MAGIC or packet[5:8] != b'\0' * 3 or len(packet) != HEADER.size + length or length > PAYLOAD or offset > MAX_FILE:
        raise ValueError('invalid_frame')
    if kind not in range(OPEN, ERROR + 1):
        raise ValueError('unknown_frame')
    return kind, offset, transfer, packet[HEADER.size:]


def open_body(device, session, segment, size, digest, token):
    if not IDENTIFIER.fullmatch(device) or not SESSION.fullmatch(session) or not 1 <= segment <= 999999999:
        raise ValueError('invalid_identity')
    if not 0 < size <= MAX_FILE or len(digest) != 32 or not 32 <= len(token) <= 96 or not re.fullmatch(r'[A-Za-z0-9_-]+', token, re.ASCII):
        raise ValueError('invalid_open')
    return (bytes([len(device)]) + device.encode('ascii') + session.encode('ascii') +
            struct.pack('!II', segment, size) + digest + bytes([len(token)]) + token.encode('ascii'))


def parse_open(body):
    if not body or not 1 <= body[0] <= 32:
        raise ValueError('invalid_device_length')
    n = body[0]
    fixed = 1 + n + 14 + 8 + 32
    if len(body) < fixed + 1 or len(body) != fixed + 1 + body[fixed]:
        raise ValueError('invalid_open_length')
    device = body[1:1 + n].decode('ascii')
    session = body[1 + n:1 + n + 14].decode('ascii')
    segment, size = struct.unpack_from('!II', body, 1 + n + 14)
    digest = body[1 + n + 22:fixed]
    token = body[fixed + 1:].decode('ascii')
    if open_body(device, session, segment, size, digest, token) != body:
        raise ValueError('invalid_open')
    object_id = hashlib.sha256(f'{device}/{session}/{segment}'.encode()).hexdigest()
    return dict(device=device, session=session, segment=segment, size=size,
                sha256=digest.hex(), object_id=object_id), token


def receipt(transfer, identity, durable, received, future=(), stored=False):
    size = identity['size']
    if not 0 <= durable <= received <= size or (durable == size) != stored:
        raise ValueError('invalid_receipt_progress')
    if received != size and received % PAYLOAD:
        raise ValueError('invalid_received_alignment')
    mask = bytearray(16)
    for offset in future:
        index = (offset - received) // PAYLOAD
        if offset % PAYLOAD or not 0 <= index < WINDOW_PACKETS:
            raise ValueError('invalid_selective_ack')
        mask[index // 8] |= 1 << (index % 8)
    body = RECEIPT.pack(size, bytes.fromhex(identity['sha256']), received, mask, int(stored))
    return frame(COMPLETE if stored else ACK, durable, transfer, body)
